"""Queue database repository.

This module provides :class:`QueueRepository` which encapsulates all
interaction with the SQLite queue database used by the application.  It is
responsible for creating the connection, ensuring thread safety through a
lock and exposing a small CRUD style API for manipulating queued paths.

The repository can also be used as a context manager so that connections are
closed cleanly when leaving a ``with`` block.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Iterable, List


class QueueRepository:
    """Simple repository wrapper around the SQLite queue database.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.

    The repository lazily manages a single connection which is safe to use
    across multiple threads thanks to an internal :class:`threading.Lock`.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.lock = threading.Lock()
        # ``check_same_thread=False`` allows the connection to be shared across
        # worker threads.  Access is still serialised via ``self.lock``.
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS queue (path TEXT PRIMARY KEY)")
        self.conn.commit()

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------
    def __enter__(self) -> "QueueRepository":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # pragma: no cover - trivial
        self.close()

    def close(self) -> None:
        """Close the underlying database connection."""

        if getattr(self, "conn", None):
            self.conn.close()
            self.conn = None

    # ------------------------------------------------------------------
    # CRUD operations
    # ------------------------------------------------------------------
    def add(self, path: Path) -> bool:
        """Insert ``path`` into the queue if not already present.

        Returns ``True`` if the path was inserted, ``False`` if it was already
        queued.
        """

        with self.lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO queue(path) VALUES (?)", (str(path),)
            )
            self.conn.commit()
            return cur.rowcount > 0

    def remove(self, path: Path) -> None:
        """Remove ``path`` from the queue."""

        with self.lock:
            self.conn.execute("DELETE FROM queue WHERE path = ?", (str(path),))
            self.conn.commit()

    def all(self) -> List[Path]:
        """Return a list of all queued paths."""

        with self.lock:
            rows = self.conn.execute("SELECT path FROM queue").fetchall()
        return [Path(p) for (p,) in rows]


__all__ = ["QueueRepository"]

