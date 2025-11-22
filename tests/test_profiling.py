from pathlib import Path
from typing import Any, cast

from babelarr.mkv import MkvSubtitleExtractor
from babelarr.mkv_probe_cache import MkvProbeCache
from babelarr.profiling import WorkloadProfiler


class DummyExtractor:
    def list_streams(self, path: Path):
        return []


def test_mkv_probe_cache_db_info(tmp_path):
    db_path = tmp_path / "cache.db"
    profiler = WorkloadProfiler(enabled=True)
    cache = MkvProbeCache(
        cast(MkvSubtitleExtractor, DummyExtractor()),
        db_path=db_path,
        profiler=profiler,
    )

    info: dict[str, Any] = cache.db_info()
    assert info["enabled"] is True
    assert info["path"] == str(db_path)
    assert info["probe_entries"] == 0
    assert info["cache_entries"] == 0
    assert info["max_entries"] == 2048
    assert info["size_bytes"] >= 0
    assert info["db_reads"]["count"] == 0
    assert info["db_writes"]["count"] == 0

    cache._save_to_db("movie.mkv", 123, [])
    cache.update_entry(tmp_path / "movie.mkv", 456, languages={"en"})
    cache.get_entry(tmp_path / "movie.mkv")
    info = cache.db_info()
    assert info["probe_entries"] == 1
    assert info["cache_entries"] == 1
    assert info["db_reads"]["count"] == 1
    assert info["db_writes"]["count"] >= 2
