from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import ANY

import pytest

from babelarr.mkv import SubtitleStream
from babelarr.mkv_probe_cache import MkvProbeCache
from babelarr.mkv_scan import MkvScanner
from babelarr.translator import Translator


class _StubExtractor:
    def __init__(self, streams: list[SubtitleStream], temp_dir: Path) -> None:
        self._streams = streams
        self._temp_dir = temp_dir
        self._counter = 0

    def list_streams(self, path: Path) -> list[SubtitleStream]:
        return self._streams

    def create_temp_path(self, suffix: str) -> Path:
        self._counter += 1
        return self._temp_dir / f"temp-{self._counter}{suffix}"

    def extract_stream(self, path: Path, stream: SubtitleStream, dest: Path) -> None:
        dest.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")


class _StubTagger:
    def __init__(self, extractor: _StubExtractor) -> None:
        self.extractor = extractor
        self._tagged: list[SubtitleStream] = []

    def detect_and_tag(self, path: Path, stream: SubtitleStream) -> None:
        self._tagged.append(stream)
        return None


@dataclass
class _RecordingTranslator(Translator):
    calls: list[tuple[Path, str, str | None]]

    def translate(self, path: Path, lang: str, *, src_lang: str | None = None) -> bytes:
        self.calls.append((path, lang, src_lang))
        return b"1\n00:00:00,000 --> 00:00:01,000\nHola\n"

    def close(self) -> None:  # pragma: no cover - unused
        return None

    def wait_until_available(self) -> None:  # pragma: no cover - unused
        return None

    def supports_translation(self, src_lang: str, target_lang: str) -> bool:
        return True

    def is_target_supported(self, target_lang: str) -> bool:
        return True


@pytest.fixture
def stub_stream() -> SubtitleStream:
    return SubtitleStream(
        ffprobe_index=0,
        subtitle_index=0,
        codec="srt",
        language="en",
        title=None,
        forced=False,
        default=False,
        duration=1.0,
        char_count=5,
        cue_count=1,
    )


def test_scanner_translates_and_caches(
    tmp_path: Path, stub_stream: SubtitleStream
) -> None:
    mkv_file = tmp_path / "movie.mkv"
    mkv_file.write_bytes(b"video")
    extractor = _StubExtractor([stub_stream], temp_dir=tmp_path)
    tagger = _StubTagger(extractor)
    translator = _RecordingTranslator(calls=[])
    probe_cache = MkvProbeCache(extractor, db_path=tmp_path / "cache.db")
    scanner = MkvScanner(
        directories=[str(tmp_path)],
        tagger=tagger,
        translator=translator,
        ensure_langs=["es"],
        probe_cache=probe_cache,
        cache_enabled=True,
        preferred_source="en",
    )

    total_files, tasks, recent = scanner.scan()
    assert total_files == 1
    assert tasks == [(mkv_file, 0)]
    assert recent == [mkv_file]

    result = scanner.process_file(mkv_file, position=1, total_paths=1)

    assert result.translated == 1
    output = mkv_file.with_suffix(".es.srt")
    assert output.read_text(encoding="utf-8").strip().endswith("Hola")
    assert translator.calls == [(ANY, "es", "en")]

    # Second scan skips because sidecar is up-to-date.
    total_files, tasks, _ = scanner.scan()
    assert total_files == 1
    assert tasks == []
