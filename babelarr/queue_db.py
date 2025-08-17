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
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS queue (
                path TEXT,
                lang TEXT,
                priority INTEGER DEFAULT 0,
                PRIMARY KEY (path, lang)
            )
            """
        )
        # ``CREATE TABLE IF NOT EXISTS`` will not modify an existing table, so
        # we need to ensure the ``priority`` column exists for databases created
        # before this field was added.
        cols = {
            row[1] for row in self.conn.execute("PRAGMA table_info(queue)").fetchall()
        }
        if "priority" not in cols:
            self.conn.execute("ALTER TABLE queue ADD COLUMN priority INTEGER DEFAULT 0")
        self.conn.commit()

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------
    def __enter__(self) -> QueueRepository:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # pragma: no cover - trivial
        self.close()

    def close(self) -> None:
        """Close the underlying database connection."""

        if getattr(self, "conn", None):
            self.conn.close()

    # ------------------------------------------------------------------
    # CRUD operations
    # ------------------------------------------------------------------
    def add(self, path: Path, lang: str, priority: int = 0) -> bool:
        """Insert ``path``/``lang`` with ``priority`` if not already present.

        Returns ``True`` if the entry was inserted, ``False`` if it was already
        queued.
        """

        with self.lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO queue(path, lang, priority) VALUES (?, ?, ?)",
                (str(path), lang, priority),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def remove(self, path: Path, lang: str | None = None) -> None:
        """Remove entries for ``path``.

        If ``lang`` is provided only that language is removed, otherwise all
        queued translations for the path are deleted.
        """

        with self.lock:
            if lang is None:
                self.conn.execute("DELETE FROM queue WHERE path = ?", (str(path),))
            else:
                self.conn.execute(
                    "DELETE FROM queue WHERE path = ? AND lang = ?",
                    (str(path), lang),
                )
            self.conn.commit()

    def all(self) -> list[tuple[Path, str, int]]:
        """Return a list of all queued path/language/priority tuples."""

        with self.lock:
            rows = self.conn.execute(
                "SELECT path, lang, priority FROM queue"
            ).fetchall()
        return [(Path(p), lang, int(priority)) for (p, lang, priority) in rows]

    def count(self) -> int:
        """Return the number of queued path/language pairs."""

        with self.lock:
            row = self.conn.execute("SELECT COUNT(*) FROM queue").fetchone()
        return int(row[0]) if row else 0

    def __len__(self) -> int:  # pragma: no cover - simple delegation
        return self.count()


__all__ = ["QueueRepository"]
