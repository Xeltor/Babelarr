from __future__ import annotations

from contextlib import nullcontext
import json
import logging
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Iterable

from .mkv import MkvSubtitleExtractor, SubtitleStream
from .profiling import WorkloadProfiler


logger = logging.getLogger(__name__)

_DB_READ_METRICS = (
    "mkv.cache.db.probe_load",
    "mkv.cache.db.cache_load",
)

_DB_WRITE_METRICS = (
    "mkv.cache.db.probe_save",
    "mkv.cache.db.cache_save",
    "mkv.cache.db.cache_delete",
    "mkv.cache.db.probe_delete",
    "mkv.cache.db.prune",
)


class MkvProbeCache:
    """Keep ffprobe results and MKV cache metadata in memory and on disk."""

    def __init__(
        self,
        extractor: MkvSubtitleExtractor,
        *,
        db_path: str | Path | None = None,
        max_entries: int | None = 2048,
        profiler: WorkloadProfiler | None = None,
    ) -> None:
        self.extractor = extractor
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, tuple[int, list[dict[str, object]]]] = OrderedDict()
        self._max_entries = max_entries
        self._conn: sqlite3.Connection | None = None
        self._db_path = Path(db_path) if db_path else None
        self._profiler = profiler
        if self._db_path:
            try:
                self._db_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning(
                    "probe_cache_dir_unavailable path=%s error=%s",
                    self._db_path.parent,
                    exc,
                )
                self._db_path = None
            else:
                try:
                    self._conn = sqlite3.connect(
                        str(self._db_path), check_same_thread=False, isolation_level=None
                    )
                    self._conn.execute(
                        "CREATE TABLE IF NOT EXISTS probe_cache "
                        "(path TEXT PRIMARY KEY, mtime_ns INTEGER, streams TEXT)"
                    )
                    self._conn.execute(
                        "CREATE TABLE IF NOT EXISTS cache_entries "
                        "(path TEXT PRIMARY KEY, mtime_ns INTEGER, languages TEXT)"
                    )
                except sqlite3.DatabaseError as exc:
                    logger.warning(
                        "probe_cache_db_error path=%s error=%s",
                        self._db_path,
                        exc,
                    )
                    self._conn = None
                    self._db_path = None

    def _profile(self, name: str):
        if not self._profiler:
            return nullcontext()
        return self._profiler.track(name)

    def list_streams(self, path: Path) -> list[SubtitleStream]:
        """Return cached subtitle streams or refresh them via ffprobe."""

        key = str(path)
        try:
            mtime_ns = path.stat().st_mtime_ns
        except FileNotFoundError:
            self.invalidate_path(path)
            raise

        with self._lock:
            entry = self._entries.get(key)
        if entry and entry[0] == mtime_ns:
            return [SubtitleStream.from_cache_dict(record) for record in entry[1]]

        if self._conn:
            serialized = self._load_from_db(key, mtime_ns)
            if serialized:
                return [SubtitleStream.from_cache_dict(record) for record in serialized]

        streams = self.extractor.list_streams(path)
        serialized = [stream.to_cache_dict() for stream in streams]
        with self._lock:
            self._entries[key] = (mtime_ns, serialized)
            self._trim()
        self._save_to_db(key, mtime_ns, serialized)
        return streams

    def invalidate_path(self, path: Path | str) -> None:
        """Drop any cached streams and metadata for *path*."""

        key = str(path)
        with self._lock:
            self._entries.pop(key, None)
        self._delete_db_entry(key)
        self._delete_entry(key)

    def _trim(self) -> None:
        if self._max_entries is None:
            return
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def _load_from_db(
        self, key: str, mtime_ns: int
    ) -> list[dict[str, object]] | None:
        if not self._conn:
            return None
        with self._profile("mkv.cache.db.probe_load"):
            with self._lock:
                cursor = self._conn.execute(
                    "SELECT mtime_ns, streams FROM probe_cache WHERE path = ?", (key,)
                )
                row = cursor.fetchone()
        if not row:
            return None
        stored_mtime, payload = row
        try:
            stored_mtime = int(stored_mtime)
        except (TypeError, ValueError):
            with self._lock:
                self._conn.execute("DELETE FROM probe_cache WHERE path = ?", (key,))
            return None
        if stored_mtime != mtime_ns:
            return None
        if not payload:
            return None
        try:
            streams = json.loads(payload)
        except ValueError:
            logger.warning("decode_probe_cache_failed path=%s data=%s", key, payload)
            return None
        if not isinstance(streams, list):
            return None
        valid: list[dict[str, object]] = []
        for entry in streams:
            if isinstance(entry, dict):
                valid.append(entry)
        if not valid:
            return None
        with self._lock:
            self._entries[key] = (mtime_ns, valid)
            self._trim()
        return valid

    def _save_to_db(
        self, key: str, mtime_ns: int, streams: Iterable[dict[str, object]]
    ) -> None:
        if not self._conn:
            return
        try:
            payload = json.dumps(list(streams))
        except TypeError:
            return
        with self._profile("mkv.cache.db.probe_save"):
            with self._lock:
                self._conn.execute(
                    "INSERT INTO probe_cache (path, mtime_ns, streams) VALUES (?, ?, ?) "
                    "ON CONFLICT(path) DO UPDATE SET mtime_ns=excluded.mtime_ns, streams=excluded.streams",
                    (key, mtime_ns, payload),
                )

    def _delete_db_entry(self, key: str) -> None:
        if not self._conn:
            return
        with self._profile("mkv.cache.db.probe_delete"):
            with self._lock:
                self._conn.execute("DELETE FROM probe_cache WHERE path = ?", (key,))

    def _encode_languages(self, languages: Iterable[str] | None) -> str | None:
        if not languages:
            return None
        normalized = sorted({lang for lang in languages if lang})
        if not normalized:
            return None
        return json.dumps(normalized)

    def _decode_languages(self, payload: str | None) -> set[str]:
        if not payload:
            return set()
        try:
            langs = json.loads(payload)
        except ValueError:
            logger.warning("decode_languages_failed data=%s", payload)
            return set()
        if not isinstance(langs, list):
            return set()
        return {str(entry) for entry in langs if isinstance(entry, str)}

    def get_entry(self, path: Path) -> tuple[int | None, set[str] | None]:
        if not self._conn:
            return None, None
        key = str(path)
        with self._profile("mkv.cache.db.cache_load"):
            with self._lock:
                cursor = self._conn.execute(
                    "SELECT mtime_ns, languages FROM cache_entries WHERE path = ?", (key,)
                )
                row = cursor.fetchone()
        if not row:
            return None, None
        stored_mtime = row[0]
        try:
            stored_mtime = int(stored_mtime) if stored_mtime is not None else None
        except (TypeError, ValueError):
            logger.warning("invalidate_entry path=%s value=%s", key, row[0])
            self._delete_entry(key)
            return None, None
        languages = self._decode_languages(row[1])
        return stored_mtime, languages

    def update_entry(
        self,
        path: Path,
        mtime_ns: int,
        *,
        languages: Iterable[str] | None = None,
    ) -> None:
        if not self._conn:
            return
        payload = self._encode_languages(languages)
        key = str(path)
        with self._profile("mkv.cache.db.cache_save"):
            with self._lock:
                self._conn.execute(
                    "INSERT INTO cache_entries (path, mtime_ns, languages) VALUES (?, ?, ?) "
                    "ON CONFLICT(path) DO UPDATE SET mtime_ns=excluded.mtime_ns, languages=excluded.languages",
                    (key, int(mtime_ns), payload),
                )

    def delete_entry(self, path: Path | str) -> None:
        self._delete_entry(str(path))

    def _delete_entry(self, key: str) -> None:
        if not self._conn:
            return
        with self._profile("mkv.cache.db.cache_delete"):
            with self._lock:
                self._conn.execute("DELETE FROM cache_entries WHERE path = ?", (key,))

    def prune_entries(self, valid_paths: Iterable[str]) -> None:
        if not self._conn:
            return
        with self._profile("mkv.cache.db.prune"):
            valid_tuple = tuple(valid_paths)
            if not valid_tuple:
                delete_sql = "DELETE FROM cache_entries"
                params: tuple[str, ...] = ()
            else:
                placeholders = ",".join("?" for _ in valid_tuple)
                delete_sql = f"DELETE FROM cache_entries WHERE path NOT IN ({placeholders})"
                params = valid_tuple
            with self._lock:
                cursor = self._conn.execute(delete_sql, params)
                removed = cursor.rowcount or 0
                if not removed:
                    return
                cursor = self._conn.execute("SELECT COUNT(*) FROM cache_entries")
                row = cursor.fetchone()
                remaining = int(row[0]) if row and row[0] is not None else 0
        logger.debug(
            "prune_entries removed=%d remaining=%d",
            removed,
            remaining,
        )

    def _summarize_db_metrics(
        self, metrics: dict[str, dict[str, float]], names: Iterable[str]
    ) -> dict[str, float]:
        summary = {"count": 0, "total": 0.0}
        for name in names:
            stat = metrics.get(name)
            if not stat:
                continue
            summary["count"] += int(stat.get("count", 0))
            summary["total"] += float(stat.get("total", 0.0))
        summary["average"] = summary["total"] / summary["count"] if summary["count"] else 0.0
        return summary

    def db_info(self) -> dict[str, object]:
        info: dict[str, object] = {
            "path": str(self._db_path) if self._db_path else None,
            "enabled": bool(self._conn),
        }
        if not self._conn:
            return info
        try:
            with self._profile("mkv.cache.db.stats"):
                with self._lock:
                    cursor = self._conn.execute("SELECT COUNT(*) FROM probe_cache")
                    probe_row = cursor.fetchone()
                    cursor = self._conn.execute("SELECT COUNT(*) FROM cache_entries")
                    cache_row = cursor.fetchone()
        except sqlite3.DatabaseError as exc:
            logger.warning(
                "probe_cache_db_info_failed path=%s error=%s",
                self._db_path,
                exc,
            )
            return info
        probe_count = int(probe_row[0]) if probe_row and probe_row[0] is not None else 0
        cache_count = int(cache_row[0]) if cache_row and cache_row[0] is not None else 0
        info["probe_entries"] = probe_count
        info["cache_entries"] = cache_count
        if self._max_entries is not None:
            info["max_entries"] = self._max_entries
        if self._db_path:
            try:
                info["size_bytes"] = self._db_path.stat().st_size
            except OSError:
                pass
        if self._profiler:
            metrics = self._profiler.metrics()
            info["db_reads"] = self._summarize_db_metrics(metrics, _DB_READ_METRICS)
            info["db_writes"] = self._summarize_db_metrics(metrics, _DB_WRITE_METRICS)
        return info
