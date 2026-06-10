import hashlib
import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional


class FileStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"


@dataclass
class DriveFile:
    file_id: str
    name: str
    path: str
    size: int
    mime_type: str
    remote_key: str
    owner: str = ""
    source: str = "drive"       # "drive" | "local"
    local_path: str = ""        # absolute path on disk (local source only)
    status: FileStatus = FileStatus.PENDING
    error: Optional[str] = None
    attempts: int = 0


class StateManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=10000")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self):
        conn = self._conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                file_id    TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                path       TEXT NOT NULL,
                size       INTEGER NOT NULL DEFAULT 0,
                mime_type  TEXT NOT NULL,
                remote_key TEXT NOT NULL,
                owner      TEXT NOT NULL DEFAULT '',
                source     TEXT NOT NULL DEFAULT 'drive',
                local_path TEXT NOT NULL DEFAULT '',
                status     TEXT NOT NULL DEFAULT 'pending',
                error      TEXT,
                attempts   INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON files(status)")
        # Migrations for older DBs
        existing = {r[1] for r in conn.execute("PRAGMA table_info(files)").fetchall()}
        for col, ddl in [
            ("owner",      "ALTER TABLE files ADD COLUMN owner TEXT NOT NULL DEFAULT ''"),
            ("source",     "ALTER TABLE files ADD COLUMN source TEXT NOT NULL DEFAULT 'drive'"),
            ("local_path", "ALTER TABLE files ADD COLUMN local_path TEXT NOT NULL DEFAULT ''"),
        ]:
            if col not in existing:
                conn.execute(ddl)
        conn.commit()

    def upsert_file(self, f: DriveFile):
        now = time.time()
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO files (file_id, name, path, size, mime_type, remote_key, owner, source, local_path, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            ON CONFLICT(file_id) DO UPDATE SET owner = excluded.owner
            WHERE files.owner = '' AND excluded.owner != ''
            """,
            (f.file_id, f.name, f.path, f.size, f.mime_type, f.remote_key,
             f.owner, f.source, f.local_path, now, now),
        )
        conn.commit()

    def get_all_pending(self, shard: int = None, total_shards: int = None) -> List[DriveFile]:
        rows = self._conn().execute(
            """
            SELECT * FROM files WHERE status = 'pending'
            ORDER BY
              CASE WHEN mime_type LIKE 'application/vnd.google-apps.%' THEN 1 ELSE 0 END ASC,
              size ASC
            """
        ).fetchall()
        files = [self._to_file(r) for r in rows]
        if shard is not None and total_shards is not None:
            files = [
                f for f in files
                if int(hashlib.md5(f.file_id.encode()).hexdigest(), 16) % total_shards == shard
            ]
        return files

    def claim_file(self, file_id: str) -> bool:
        """Atomically mark a pending file as in_progress. Returns True if claimed."""
        conn = self._conn()
        cursor = conn.execute(
            "UPDATE files SET status = 'in_progress', updated_at = ? WHERE file_id = ? AND status = 'pending'",
            (time.time(), file_id),
        )
        conn.commit()
        return cursor.rowcount > 0

    def update_remote_key(self, file_id: str, remote_key: str):
        conn = self._conn()
        conn.execute(
            "UPDATE files SET remote_key = ?, updated_at = ? WHERE file_id = ?",
            (remote_key, time.time(), file_id),
        )
        conn.commit()

    def mark_done(self, file_id: str):
        conn = self._conn()
        conn.execute(
            "UPDATE files SET status = 'done', error = NULL, updated_at = ? WHERE file_id = ?",
            (time.time(), file_id),
        )
        conn.commit()

    def mark_failed(self, file_id: str, error: str):
        conn = self._conn()
        conn.execute(
            "UPDATE files SET status = 'failed', error = ?, attempts = attempts + 1, updated_at = ? WHERE file_id = ?",
            (error[:1000], time.time(), file_id),
        )
        conn.commit()

    def reset_in_progress(self):
        """On startup, reset any in_progress files (from a crashed previous run) back to pending."""
        conn = self._conn()
        n = conn.execute(
            "UPDATE files SET status = 'pending', updated_at = ? WHERE status = 'in_progress'",
            (time.time(),),
        ).rowcount
        conn.commit()
        return n

    def retry_failed(self) -> int:
        conn = self._conn()
        n = conn.execute(
            "UPDATE files SET status = 'pending', updated_at = ? WHERE status = 'failed'",
            (time.time(),),
        ).rowcount
        conn.commit()
        return n

    def get_failed(self) -> List[dict]:
        rows = self._conn().execute(
            "SELECT file_id, name, path, size, error, attempts FROM files WHERE status = 'failed' ORDER BY path, name"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stats_by_owner(self) -> dict:
        """Return {owner: {status: {count, bytes}}} grouped by owner."""
        rows = self._conn().execute(
            "SELECT owner, status, COUNT(*) as cnt, COALESCE(SUM(size), 0) as bytes "
            "FROM files GROUP BY owner, status"
        ).fetchall()
        result: dict = {}
        for row in rows:
            owner = row["owner"] or "(no owner)"
            if owner not in result:
                result[owner] = {s: {"count": 0, "bytes": 0} for s in ("pending", "in_progress", "done", "failed")}
            st = row["status"]
            if st in result[owner]:
                result[owner][st] = {"count": row["cnt"], "bytes": row["bytes"]}
        return result

    def get_stats(self) -> dict:
        rows = self._conn().execute(
            "SELECT status, COUNT(*) as count, COALESCE(SUM(size), 0) as bytes FROM files GROUP BY status"
        ).fetchall()
        stats = {s: {"count": 0, "bytes": 0} for s in ("pending", "in_progress", "done", "failed")}
        for row in rows:
            stats[row["status"]] = {"count": row["count"], "bytes": row["bytes"]}
        return stats

    def _to_file(self, row) -> DriveFile:
        return DriveFile(
            file_id=row["file_id"],
            name=row["name"],
            path=row["path"],
            size=row["size"],
            mime_type=row["mime_type"],
            remote_key=row["remote_key"],
            owner=row["owner"] if row["owner"] else "",
            source=row["source"] if row["source"] else "drive",
            local_path=row["local_path"] if row["local_path"] else "",
            status=FileStatus(row["status"]),
            error=row["error"],
            attempts=row["attempts"],
        )
