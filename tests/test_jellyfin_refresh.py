from pathlib import Path

from babelarr.mkv import SubtitleMetrics, SubtitleStream
from babelarr.mkv_scan import MkvScanner


class DummyProbeCache:
    def __init__(self, streams: list[SubtitleStream]) -> None:
        self._streams = streams

    def list_streams(self, path: Path) -> list[SubtitleStream]:
        return self._streams

    def invalidate_path(self, path: Path | str) -> None:
        pass

    def delete_entry(self, path: Path) -> None:
        pass


class DummyTagger:
    extractor = None


class NotifyingScanner(MkvScanner):
    def _translate_missing(
        self,
        path: Path,
        candidates: dict[str, tuple[SubtitleStream, SubtitleMetrics, bool]],
        mtime_ns: int,
        existing_langs: set[str] | None,
    ) -> tuple[int, bool, bool]:
        return 1, False, False


class SilentScanner(MkvScanner):
    def _translate_missing(
        self,
        path: Path,
        candidates: dict[str, tuple[SubtitleStream, SubtitleMetrics, bool]],
        mtime_ns: int,
        existing_langs: set[str] | None,
    ) -> tuple[int, bool, bool]:
        return 0, False, False


class FakeJellyfin:
    def __init__(self) -> None:
        self.paths: list[Path] = []

    def refresh_path(self, path: Path) -> None:
        self.paths.append(path)


def test_notifies_jellyfin_after_translation(tmp_path: Path) -> None:
    source = tmp_path / "movie.mkv"
    source.write_text("dummy")
    stream = SubtitleStream(
        ffprobe_index=0,
        subtitle_index=1,
        codec="subrip",
        language="eng",
        title="English",
        forced=False,
        default=False,
    )
    cache = DummyProbeCache([stream])
    jellyfin = FakeJellyfin()

    scanner = NotifyingScanner(
        directories=[str(tmp_path)],
        tagger=DummyTagger(),
        translator=object(),
        ensure_langs=["eng", "nl"],
        probe_cache=cache,
        cache_enabled=False,
        jellyfin_client=jellyfin,
    )

    result = scanner.process_file(source)

    assert result.translated == 1
    assert jellyfin.paths == [source]


def test_does_not_notify_when_no_changes(tmp_path: Path) -> None:
    source = tmp_path / "movie.mkv"
    source.write_text("dummy")
    stream = SubtitleStream(
        ffprobe_index=0,
        subtitle_index=1,
        codec="subrip",
        language="eng",
        title="English",
        forced=False,
        default=False,
    )
    cache = DummyProbeCache([stream])
    jellyfin = FakeJellyfin()

    scanner = SilentScanner(
        directories=[str(tmp_path)],
        tagger=DummyTagger(),
        translator=object(),
        ensure_langs=["eng", "nl"],
        probe_cache=cache,
        cache_enabled=False,
        jellyfin_client=jellyfin,
    )

    result = scanner.process_file(source)

    assert result.translated == 0
    assert jellyfin.paths == []
