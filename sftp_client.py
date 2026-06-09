import logging
import os
import socket
import stat
import threading
import time

import paramiko

logger = logging.getLogger(__name__)


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


class HetznerStorageBoxClient:
    """
    SFTP client for Hetzner Storage Box.
    Each thread gets its own SSH/SFTP connection via thread-local storage.
    """

    def __init__(self, host: str, port: int, username: str, password: str, base_path: str = "/"):
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._base_path = base_path.strip("/")  # Hetzner rejects absolute paths
        self._local = threading.local()
        # Shared across threads — directories that are confirmed to exist on the remote.
        # Avoids stat RPC per path component for every file in a shared subtree.
        self._known_dirs: set[str] = set()
        self._dir_lock = threading.Lock()
        # Serialize SSH connection/auth attempts — Hetzner rate-limits simultaneous auths.
        self._connect_lock = threading.Lock()

    def _sftp(self) -> paramiko.SFTPClient:
        """Return this thread's SFTP connection, creating it if needed."""
        local = self._local
        transport_ok = (
            hasattr(local, "transport")
            and local.transport is not None
            and local.transport.is_active()
        )
        if not transport_ok:
            with self._connect_lock:  # one auth at a time — Hetzner rate-limits simultaneous auths
                sock = socket.create_connection((self._host, self._port), timeout=60)
                # OS-level TCP keepalives so the kernel detects a silently dead connection
                # and raises an error instead of blocking put() forever.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 10)
                sock.settimeout(None)  # no per-op timeout — uploads can take many minutes
                transport = paramiko.Transport(sock)
                transport.set_keepalive(30)  # SSH-level keepalive as a secondary guard
                transport.connect(username=self._username, password=self._password)
                local.transport = transport
                local.sftp = paramiko.SFTPClient.from_transport(transport)
                logger.debug(f"SFTP connection opened (thread {threading.current_thread().name})")
        return local.sftp

    def close(self):
        """Close this thread's connection."""
        if hasattr(self._local, "transport") and self._local.transport:
            self._local.transport.close()

    def key_exists(self, remote_key: str) -> bool:
        """Return True if the remote file already exists (used to skip re-uploads)."""
        remote_path = self._remote_path(remote_key)
        try:
            self._sftp().stat(remote_path)
            return True
        except FileNotFoundError:
            return False

    def upload_file(self, local_path: str, remote_key: str, _chunk_size: int = 0):
        """Upload local_path to the storage box at remote_key."""
        remote_path = self._remote_path(remote_key)
        self._makedirs(os.path.dirname(remote_path))

        size = os.path.getsize(local_path)
        logger.debug(f"SFTP put {remote_path} ({_fmt_size(size)})")
        self._sftp().put(local_path, remote_path, confirm=True)

    def _remote_path(self, key: str) -> str:
        key = key.strip("/")
        return f"{self._base_path}/{key}" if self._base_path else key

    def _makedirs(self, remote_dir: str):
        """Recursively create remote directories if they don't exist."""
        if not remote_dir:
            return
        sftp = self._sftp()
        # Build incrementally using relative paths (absolute paths are
        # permission-denied on Hetzner Storage Box).
        parts = [p for p in remote_dir.split("/") if p]
        path = ""
        for part in parts:
            path = f"{path}/{part}" if path else part  # stays relative
            with self._dir_lock:
                if path in self._known_dirs:
                    continue
            try:
                sftp.stat(path)
            except OSError:
                try:
                    sftp.mkdir(path)
                except OSError as mkdir_err:
                    err_lower = str(mkdir_err).lower()
                    # Permanent failures — disk full, quota, permissions — don't retry.
                    if any(k in err_lower for k in ("quota", "no space", "permission denied", "read-only")):
                        raise RuntimeError(
                            f"Cannot create remote directory '{path}': {mkdir_err}\n"
                            "  Check storage quota and permissions on the remote server."
                        ) from mkdir_err
                    # Transient: another thread may have just created it — verify with stat.
                    for _ in range(3):
                        time.sleep(0.2)
                        try:
                            sftp.stat(path)
                            break
                        except OSError:
                            pass
                    else:
                        raise RuntimeError(
                            f"Could not create remote directory '{path}': {mkdir_err}"
                        ) from mkdir_err
            with self._dir_lock:
                self._known_dirs.add(path)
