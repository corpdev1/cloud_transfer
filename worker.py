import logging
import os
from pathlib import Path

from config import Config
from gdrive_client import GoogleDriveClient
from sftp_client import HetznerStorageBoxClient, _fmt_size
from state import DriveFile, StateManager

logger = logging.getLogger(__name__)


class TransferWorker:
    def __init__(self, config: Config, drive: GoogleDriveClient, sftp: HetznerStorageBoxClient, state: StateManager):
        self.config = config
        self.drive = drive
        self.sftp = sftp
        self.state = state
        Path(config.temp_dir).mkdir(parents=True, exist_ok=True)

    def transfer(self, file: DriveFile) -> bool:
        if file.source == "local":
            return self._transfer_local(file)
        return self._transfer_drive(file)

    def _transfer_local(self, file: DriveFile) -> bool:
        """Upload a local file directly to Hetzner — no Drive download needed."""
        display = file.local_path
        if not os.path.exists(file.local_path):
            self.state.mark_failed(file.file_id, "local file not found")
            logger.error(f"Local file not found: {file.local_path}")
            return False

        for attempt in range(1, self.config.max_retries + 1):
            try:
                size = os.path.getsize(file.local_path)
                logger.info(f"[{attempt}] Uploading local file: {display} ({_fmt_size(size)})")
                self.sftp.upload_file(file.local_path, file.remote_key)
                self.state.mark_done(file.file_id)
                logger.info(f"Done: {display}")
                return True
            except Exception as e:
                logger.warning(f"Attempt {attempt} failed for {display}: {e}")
                if attempt == self.config.max_retries:
                    self.state.mark_failed(file.file_id, str(e))
                    logger.error(f"Giving up on: {display}")
                    return False
        return False

    def _transfer_drive(self, file: DriveFile) -> bool:
        """Download from Drive to temp file, upload to Storage Box via SFTP, clean up."""
        display = f"{file.path}/{file.name}" if file.path else file.name
        temp_path = os.path.join(self.config.temp_dir, f"{file.file_id}.tmp")
        remote_key = file.remote_key
        downloaded = False

        try:
            for attempt in range(1, self.config.max_retries + 1):
                try:
                    if not downloaded:
                        logger.info(f"[{attempt}/{self.config.max_retries}] Downloading: {display} ({_fmt_size(file.size)})")
                        actual_ext = self.drive.download_file(
                            file.file_id, file.mime_type, temp_path, self.config.chunk_size_bytes,
                        )
                        if actual_ext is not None:
                            original_ext = Path(file.remote_key).suffix
                            if actual_ext != original_ext:
                                # -len("") == 0, and s[:0] == "" — guard against extensionless files
                                base = file.remote_key[:-len(original_ext)] if original_ext else file.remote_key
                                remote_key = base + actual_ext
                                self.state.update_remote_key(file.file_id, remote_key)
                                logger.info(f"Switched export format: {original_ext or '(none)'} → {actual_ext} for {display}")
                        downloaded = True
                    else:
                        logger.info(f"[{attempt}/{self.config.max_retries}] Retrying upload (skipping re-download): {display}")

                    actual_size = os.path.getsize(temp_path)
                    logger.info(f"Uploading via SFTP: {remote_key} ({_fmt_size(actual_size)})")
                    self.sftp.upload_file(temp_path, remote_key)

                    self.state.mark_done(file.file_id)
                    logger.info(f"Done: {display}")
                    return True

                except Exception as e:
                    err_str = str(e)
                    phase = "upload" if downloaded else "download"
                    logger.warning(f"Attempt {attempt} failed ({phase}) for {display}: {e}")
                    if "nodename nor servname" in err_str or "cannotDownloadAbusiveFile" in err_str or "cannotDownloadFile" in err_str or "fileNotDownloadable" in err_str:
                        self.state.mark_failed(file.file_id, err_str)
                        logger.error(f"Giving up on: {display}")
                        return False
                    if attempt == self.config.max_retries:
                        self.state.mark_failed(file.file_id, err_str)
                        logger.error(f"Giving up on: {display}")
                        return False
        finally:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass

        return False
