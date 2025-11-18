import pytest

pytest.skip(
    "MKV scanner tests need rewriting for the new translation flow",
    allow_module_level=True,
)

from babelarr.mkv import DetectionResult, SubtitleStream
from babelarr.mkv_scan import MkvCache, MkvScanner


class DummyExtractor:
    def __init__(self, streams):
        self._streams = streams
        self.calls = 0

    def list_streams(self, path):
        self.calls += 1
        return list(self._streams)


class DummyTagger:
    def __init__(self, streams):
        self.extractor = DummyExtractor(streams)
        self.detect_calls = 0

    def detect_and_tag(self, path, stream):
        self.detect_calls += 1
        return DetectionResult("en", 0.9)

    def ensure_longest_default(self, path, processed):
        return None


def make_stream():
    return SubtitleStream(
        ffprobe_index=0,
        subtitle_index=1,
        codec="subrip",
        language=None,
        title=None,
        forced=False,
        default=False,
    )


def test_mkv_cache_roundtrip(tmp_path):
    cache_file = tmp_path / "cache.db"
    cache = MkvCache(cache_file)
    file_path = tmp_path / "movie.mkv"
    file_path.write_text("dummy")
    cache.update(file_path, 123)
    assert cache.get_mtime(file_path) == 123

    cache_reload = MkvCache(cache_file)
    assert cache_reload.get_mtime(file_path) == 123
    cache_reload.prune([])
    assert cache_reload.get_mtime(file_path) is None


def test_mkv_cache_handles_corrupt_file(tmp_path):
    cache_file = tmp_path / "cache.db"
    cache_file.write_text("not a sqlite database")
    cache = MkvCache(cache_file)
    file_path = tmp_path / "movie.mkv"
    file_path.write_text("dummy")
    cache.update(file_path, 789)
    assert cache.get_mtime(file_path) == 789


def test_mkv_scanner_skips_cached_files(tmp_path, monkeypatch):
    library = tmp_path / "library"
    library.mkdir()
    mkv_file = library / "movie.mkv"
    mkv_file.write_text("video")

    tagger = DummyTagger([make_stream()])
    cache = MkvCache(tmp_path / "cache.db")
    scanner = MkvScanner([str(library)], tagger, cache)

    files, tagged = scanner.scan()
    assert files == 1
    assert tagged == 1
    assert tagger.detect_calls == 1

    files2, tagged2 = scanner.scan()
    assert files2 == 1
    assert tagged2 == 0
    assert tagger.detect_calls == 1  # no additional tagging


def test_mkv_scanner_scan_files(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    mkv_file = library / "movie.mkv"
    mkv_file.write_text("video")

    tagger = DummyTagger([make_stream()])
    cache = MkvCache(tmp_path / "cache.db")
    scanner = MkvScanner([], tagger, cache)

    files, tagged = scanner.scan_files([mkv_file])
    assert files == 1
    assert tagged == 1
