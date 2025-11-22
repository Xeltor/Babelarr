from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from babelarr.mkv import SubtitleStream
from babelarr.mkv_probe_cache import MkvProbeCache
from babelarr.profiling import WorkloadProfiler


class _StubExtractor:
    def __init__(self, streams: list[SubtitleStream]) -> None:
        self.streams = streams
        self.calls = 0

    def list_streams(self, path: Path) -> list[SubtitleStream]:
        self.calls += 1
        return self.streams


def _sample_stream() -> SubtitleStream:
    return SubtitleStream(
        ffprobe_index=1,
        subtitle_index=1,
        codec="srt",
        language="en",
        title=None,
        forced=False,
        default=False,
    )


def test_probe_cache_persists_and_prunes(tmp_path: Path) -> None:
    mkv = tmp_path / "movie.mkv"
    mkv.write_text("video")
    extractor = _StubExtractor([_sample_stream()])
    profiler = WorkloadProfiler(enabled=True)
    cache = MkvProbeCache(
        extractor, db_path=tmp_path / "cache.db", max_entries=1, profiler=profiler
    )

    # First call populates cache and DB.
    first = cache.list_streams(mkv)
    assert extractor.calls == 1
    assert first[0].language == "en"

    # Second call hits in-memory cache.
    second = cache.list_streams(mkv)
    assert extractor.calls == 1
    assert second[0].language == "en"

    # Cache entry is stored and can be read back via db_info.
    info = cache.db_info()
    assert info["enabled"] is True
    assert info["probe_entries"] == 1
    assert info["cache_entries"] == 0

    # Populate and prune language cache table.
    mtime_ns = mkv.stat().st_mtime_ns
    cache.update_entry(mkv, mtime_ns, languages=["en", "es"])
    stored_mtime, langs = cache.get_entry(mkv)
    assert stored_mtime == mtime_ns
    assert langs == {"en", "es"}

    cache.prune_entries([])
    assert cache.get_entry(mkv) == (None, None)

    metrics = profiler.metrics()
    assert metrics.get("mkv.cache.db.cache_save")


def test_probe_cache_handles_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing.mkv"
    extractor = _StubExtractor([_sample_stream()])
    cache = MkvProbeCache(extractor, db_path=tmp_path / "cache.db")

    try:
        cache.list_streams(missing)
    except FileNotFoundError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("expected FileNotFoundError")

    # Entries are cleaned when invalidated.
    cache.invalidate_path(missing)
    assert cache.get_entry(missing) == (None, None)


def test_probe_cache_recovers_from_corrupt_rows(tmp_path: Path) -> None:
    mkv = tmp_path / "movie.mkv"
    mkv.write_text("video")
    extractor = _StubExtractor([_sample_stream()])
    cache = MkvProbeCache(extractor, db_path=tmp_path / "cache.db")
    assert cache._conn is not None
    conn = cache._conn
    conn.execute(
        "INSERT INTO probe_cache (path, mtime_ns, streams) VALUES (?, ?, ?)",
        (str(mkv), "bad", "not-json"),
    )
    conn.execute(
        "INSERT INTO cache_entries (path, mtime_ns, languages) VALUES (?, ?, ?)",
        (str(mkv), "oops", "not-json"),
    )

    assert cache._load_from_db(str(mkv), mkv.stat().st_mtime_ns) is None
    assert cache.get_entry(mkv) == (None, None)


def test_probe_cache_handles_unwritable_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_mkdir(self: Path, parents: bool = False, exist_ok: bool = False) -> None:
        raise OSError("nope")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    cache = MkvProbeCache(
        _StubExtractor([_sample_stream()]), db_path=tmp_path / "nested" / "cache.db"
    )
    assert cache._db_path is None
    assert cache._conn is None


def test_probe_cache_handles_db_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_connect(*args: object, **kwargs: object) -> None:
        raise sqlite3.DatabaseError("fail")

    monkeypatch.setattr("babelarr.mkv_probe_cache.sqlite3.connect", fail_connect)
    cache = MkvProbeCache(
        _StubExtractor([_sample_stream()]), db_path=tmp_path / "cache.db"
    )
    assert cache._conn is None
