from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)


class MkvWorkIndex:
    """Persist MKV translation tasks so restarts can resume work."""

    def __init__(self, db_path: str | Path | None):
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._db_path = Path(db_path) if db_path else None
        if not self._db_path:
            return
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "work_index_dir_unavailable path=%s error=%s",
                self._db_path.parent,
                exc,
            )
            self._db_path = None
            return
        try:
            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                isolation_level=None,
            )
            with self._lock:
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS mkv_work ("
                    "path TEXT PRIMARY KEY, "
                    "mtime_ns INTEGER, "
                    "size_bytes INTEGER, "
                    "status TEXT, "
                    "priority INTEGER DEFAULT 1"
                    ")"
                )
        except sqlite3.DatabaseError as exc:
            logger.warning(
                "work_index_db_error path=%s error=%s",
                self._db_path,
                exc,
            )
            self._conn = None
            self._db_path = None

    def record_pending(
        self,
        path: Path,
        *,
        mtime_ns: int | None,
        size_bytes: int | None,
        priority: int,
    ) -> None:
        if not self._conn or mtime_ns is None or size_bytes is None:
            return
        key = str(path)
        normalized_priority = priority if priority in (0, 1) else 1
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT mtime_ns, size_bytes, status, priority FROM mkv_work WHERE path = ?",
                    (key,),
                ).fetchone()
                if row:
                    stored_mtime, stored_size, status, stored_priority = row
                    current_priority = min(
                        stored_priority or normalized_priority, normalized_priority
                    )
                    if (
                        stored_mtime == mtime_ns
                        and stored_size == size_bytes
                        and status == "pending"
                        and stored_priority == current_priority
                    ):
                        return
                self._conn.execute(
                    "INSERT INTO mkv_work (path, mtime_ns, size_bytes, status, priority) "
                    "VALUES (?, ?, ?, 'pending', ?) "
                    "ON CONFLICT(path) DO UPDATE SET "
                    "mtime_ns=excluded.mtime_ns, "
                    "size_bytes=excluded.size_bytes, "
                    "status='pending', "
                    "priority=min(excluded.priority, mkv_work.priority)",
                    (key, int(mtime_ns), int(size_bytes), normalized_priority),
                )
            except sqlite3.DatabaseError as exc:
                logger.warning("work_index_record_failed path=%s error=%s", path, exc)

    def recover_pending(self) -> list[tuple[Path, int]]:
        if not self._conn:
            return []
        recovered: list[tuple[Path, int]] = []
        try:
            with self._lock:
                cursor = self._conn.execute(
                    "SELECT path, priority FROM mkv_work WHERE status IN ('pending', 'in_progress')"
                )
                rows = cursor.fetchall()
                self._conn.execute(
                    "UPDATE mkv_work SET status='pending' WHERE status='in_progress'"
                )
        except sqlite3.DatabaseError as exc:
            logger.warning("work_index_recover_failed error=%s", exc)
            return recovered
        for raw_path, priority in rows:
            path = Path(raw_path)
            if not path.exists():
                self.delete(path)
                continue
            recovered.append((path, priority if priority in (0, 1) else 1))
        return recovered

    def mark_in_progress(self, path: Path) -> None:
        if not self._conn:
            return
        key = str(path)
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE mkv_work SET status='in_progress' WHERE path = ?", (key,)
                )
            except sqlite3.DatabaseError as exc:
                logger.warning(
                    "work_index_mark_in_progress_failed path=%s error=%s", path, exc
                )

    def mark_finished(
        self,
        path: Path,
        *,
        mtime_ns: int | None,
        size_bytes: int | None,
        pending: bool,
        missing: bool,
    ) -> None:
        if not self._conn:
            return
        if missing:
            self.delete(path)
            return
        key = str(path)
        with self._lock:
            try:
                if pending:
                    if mtime_ns is None or size_bytes is None:
                        self._conn.execute(
                            "UPDATE mkv_work SET status='pending' WHERE path = ?",
                            (key,),
                        )
                    else:
                        self._conn.execute(
                            "UPDATE mkv_work SET mtime_ns=?, size_bytes=?, status='pending' WHERE path=?",
                            (int(mtime_ns), int(size_bytes), key),
                        )
                else:
                    self._conn.execute("DELETE FROM mkv_work WHERE path = ?", (key,))
            except sqlite3.DatabaseError as exc:
                logger.warning(
                    "work_index_mark_finished_failed path=%s error=%s", path, exc
                )

    def prune_missing(self, valid_paths: Iterable[str]) -> None:
        if not self._conn:
            return
        known = tuple(valid_paths)
        try:
            with self._lock:
                if not known:
                    self._conn.execute("DELETE FROM mkv_work")
                    return
                placeholders = ",".join("?" for _ in known)
                self._conn.execute(
                    f"DELETE FROM mkv_work WHERE path NOT IN ({placeholders})",
                    known,
                )
        except sqlite3.DatabaseError as exc:
            logger.warning("work_index_prune_failed error=%s", exc)

    def delete(self, path: Path) -> None:
        if not self._conn:
            return
        key = str(path)
        with self._lock:
            try:
                self._conn.execute("DELETE FROM mkv_work WHERE path = ?", (key,))
            except sqlite3.DatabaseError as exc:
                logger.warning("work_index_delete_failed path=%s error=%s", path, exc)
