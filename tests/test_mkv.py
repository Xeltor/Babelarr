from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from babelarr.mkv import (
    DetectionResult,
    MkvSubtitleExtractor,
    MkvSubtitleTagger,
    SubtitleMetrics,
    SubtitleStream,
    normalize_language_code,
)
from babelarr.mkv_probe_cache import MkvProbeCache
from babelarr.mkv_scan import MkvScanner
from babelarr.translator import LibreTranslateClient

if TYPE_CHECKING:
    from pytest import MonkeyPatch


class DummyCompletedProcess:
    def __init__(self, stdout: str = "", bytes_stdout: bytes | None = None) -> None:
        self.stdout = bytes_stdout if bytes_stdout is not None else stdout
        self.stderr = ""
        self.returncode = 0
        self.bytes_stdout = bytes_stdout


def test_list_streams_parses_ffprobe(monkeypatch: MonkeyPatch) -> None:
    payload = {
        "streams": [
            {
                "index": 5,
                "codec_name": "subrip",
                "tags": {"language": "EN", "title": "English"},
                "disposition": {"forced": 0, "default": 1},
            },
            {
                "index": 6,
                "codec_name": "ass",
                "tags": {"language": "es"},
                "disposition": {"forced": 1, "default": 0},
            },
        ]
    }

    def fake_run(cmd: list[str], **kwargs: object) -> DummyCompletedProcess:
        assert cmd[0] == "ffprobe"
        return DummyCompletedProcess(stdout=json.dumps(payload))

    monkeypatch.setattr("subprocess.run", fake_run)

    extractor = MkvSubtitleExtractor()
    streams = extractor.list_streams(Path("movie.mkv"))

    assert len(streams) == 2
    assert streams[0].ffprobe_index == 5
    assert streams[0].subtitle_index == 1
    assert streams[0].language == "en"
    assert streams[0].default is True
    assert streams[1].forced is True
    assert streams[1].track_selector == "track:s2"


def test_extract_sample_invokes_ffmpeg(monkeypatch: MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> DummyCompletedProcess:
        captured["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"sample data")
        return DummyCompletedProcess(bytes_stdout=b"sample data")

    monkeypatch.setattr("subprocess.run", fake_run)
    # Ensure the temp output exists before replacement
    monkeypatch.setattr(
        Path,
        "replace",
        lambda self, target: target.write_bytes(b"sample data") or target,
    )

    extractor = MkvSubtitleExtractor()
    extractor._has_mkvextract = False  # force ffmpeg path for consistent command
    stream = SubtitleStream(
        ffprobe_index=7,
        subtitle_index=2,
        codec="subrip",
        language=None,
        title=None,
        forced=False,
        default=False,
    )
    sample = extractor.extract_sample(Path("movie.mkv"), stream)

    assert sample == b"sample data"
    assert "-map" in captured["cmd"]
    assert "0:s:1" in captured["cmd"]
    assert "-c" in captured["cmd"]
    assert "copy" in captured["cmd"]


def test_extract_sample_transcodes_ass(monkeypatch: MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> DummyCompletedProcess:
        captured["cmd"] = cmd
        Path(cmd[-1]).write_text(
            "[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\nFormat: Name\nStyle: Default\n\n[Events]\nFormat: Text\nDialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,Hello",
            encoding="utf-8",
        )
        return DummyCompletedProcess(bytes_stdout=b"sample data")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(
        Path,
        "replace",
        lambda self, target: target.write_bytes(b"sample data") or target,
    )

    extractor = MkvSubtitleExtractor()
    extractor._has_mkvextract = False
    stream = SubtitleStream(
        ffprobe_index=7,
        subtitle_index=2,
        codec="ass",
        language=None,
        title=None,
        forced=False,
        default=False,
    )
    sample = extractor.extract_sample(Path("movie.mkv"), stream)

    assert b"Hello" in sample
    assert "-c:s" in captured["cmd"] or "-c" in captured["cmd"]
    assert "srt" in captured["cmd"]


def test_extract_stream_writes_srt(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    dest = tmp_path / "movie.en.srt"

    def fake_run(cmd: list[str], **kwargs: object) -> DummyCompletedProcess:
        captured["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"raw")
        return DummyCompletedProcess()

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(
        Path, "replace", lambda self, target: target.write_bytes(b"raw") or target
    )

    extractor = MkvSubtitleExtractor()
    extractor._has_mkvextract = False
    stream = SubtitleStream(
        ffprobe_index=7,
        subtitle_index=2,
        codec="subrip",
        language="eng",
        title=None,
        forced=False,
        default=False,
    )
    source_file = tmp_path / "movie.mkv"
    source_file.write_text("dummy")
    extractor.extract_stream(source_file, stream, dest)
    assert "-map" in captured["cmd"]
    assert "0:s:1" in captured["cmd"]
    assert "-c:s" in captured["cmd"] or "-c" in captured["cmd"]
    assert "srt" in captured["cmd"]


def test_normalize_language_code_handles_two_letter() -> None:
    assert normalize_language_code("EN") == "eng"
    assert normalize_language_code("eng") == "eng"
    assert normalize_language_code(None) is None


def test_detect_and_tag_applies_new_language(monkeypatch: MonkeyPatch) -> None:
    sample_calls = []

    class FakeExtractor(MkvSubtitleExtractor):
        def __init__(self) -> None:
            pass

        def extract_sample(self, path: Path, stream: SubtitleStream) -> bytes:
            sample_calls.append((path, stream))
            return b"hello"

    class FakeTranslator:
        def detect_language(
            self, sample: bytes, *, min_confidence: float = 0.0
        ) -> DetectionResult | None:
            assert sample == b"hello"
            assert min_confidence == pytest.approx(0.9)
            return DetectionResult("en", 0.93)

    mkv_calls = []

    def fake_run(cmd: list[str], **kwargs: object) -> DummyCompletedProcess:
        mkv_calls.append(cmd)
        return DummyCompletedProcess(bytes_stdout=b"")

    monkeypatch.setattr("subprocess.run", fake_run)

    tagger = MkvSubtitleTagger(
        extractor=cast(MkvSubtitleExtractor, FakeExtractor()),
        translator=cast(LibreTranslateClient, FakeTranslator()),
        mkvpropedit_path="mkvpropedit",
        min_confidence=0.9,
    )
    stream = SubtitleStream(
        ffprobe_index=0,
        subtitle_index=1,
        codec="subrip",
        language=None,
        title=None,
        forced=False,
        default=False,
    )
    detection = tagger.detect_and_tag(Path("movie.mkv"), stream)

    assert detection is not None
    assert mkv_calls
    assert mkv_calls[0][0] == "mkvpropedit"


def test_detect_and_tag_skips_existing_language(monkeypatch: MonkeyPatch) -> None:
    class FakeExtractor(MkvSubtitleExtractor):
        def __init__(self) -> None:
            pass

        def extract_sample(self, path: Path, stream: SubtitleStream) -> bytes:
            return b"ignored"

    class FakeTranslator:
        def detect_language(
            self, sample: bytes, *, min_confidence: float = 0.0
        ) -> DetectionResult:
            return DetectionResult("en", 0.95)

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("should not run")),
    )

    tagger = MkvSubtitleTagger(
        extractor=cast(MkvSubtitleExtractor, FakeExtractor()),
        translator=cast(LibreTranslateClient, FakeTranslator()),
    )
    stream = SubtitleStream(
        ffprobe_index=0,
        subtitle_index=1,
        codec="subrip",
        language="eng",
        title=None,
        forced=False,
        default=False,
    )
    detection = tagger.detect_and_tag(Path("movie.mkv"), stream)
    assert detection is None


class _FallbackTranslator:
    def translate(self, path: Path, lang: str, *, src_lang: str | None = None) -> bytes:
        return b""

    def close(self) -> None:
        return None

    def wait_until_available(self) -> None:
        return None

    def supports_translation(self, src_lang: str, target_lang: str) -> bool:
        return True

    def is_target_supported(self, target_lang: str) -> bool:
        return True


class _DummyTagger:
    extractor = None


def test_pick_source_stream_uses_other_languages() -> None:
    scanner = MkvScanner(
        [],
        cast(MkvSubtitleTagger, _DummyTagger()),
        _FallbackTranslator(),
        ensure_langs=["eng", "nl"],
        preferred_source="eng",
    )
    stream = SubtitleStream(
        ffprobe_index=0,
        subtitle_index=1,
        codec="subrip",
        language="spa",
        title="Spanish",
        forced=False,
        default=False,
    )
    candidates = {"spa": (stream, SubtitleMetrics.from_stream(stream), False)}
    source, selected = scanner._pick_source_stream(Path("movie.mkv"), candidates, "eng")
    assert source == "spa"
    assert selected is stream


def test_pick_source_prefers_english_when_available() -> None:
    translator = _FallbackTranslator()
    scanner = MkvScanner(
        [],
        cast(MkvSubtitleTagger, _DummyTagger()),
        translator,
        ensure_langs=["bos"],
        preferred_source="bos",
    )
    english = SubtitleStream(
        ffprobe_index=0,
        subtitle_index=1,
        codec="subrip",
        language="eng",
        title="English",
        forced=False,
        default=False,
    )
    spanish = SubtitleStream(
        ffprobe_index=1,
        subtitle_index=2,
        codec="subrip",
        language="spa",
        title="Spanish",
        forced=False,
        default=False,
    )
    candidates = {
        "en": (english, SubtitleMetrics.from_stream(english), False),
        "es": (spanish, SubtitleMetrics.from_stream(spanish), False),
    }
    source, selected = scanner._pick_source_stream(Path("movie.mkv"), candidates, "bs")
    assert source == "en"
    assert selected is english


def test_map_streams_prefers_non_specialized_tracks() -> None:
    scanner = MkvScanner(
        [],
        cast(MkvSubtitleTagger, _DummyTagger()),
        _FallbackTranslator(),
        ensure_langs=["eng"],
    )
    general = SubtitleStream(
        ffprobe_index=0,
        subtitle_index=1,
        codec="subrip",
        language="eng",
        title="English",
        forced=False,
        default=False,
    )
    general.char_count = 50
    general.cue_count = 5
    specialized = SubtitleStream(
        ffprobe_index=1,
        subtitle_index=2,
        codec="subrip",
        language="eng",
        title="English SDH",
        forced=True,
        default=False,
    )
    specialized.char_count = 400
    specialized.cue_count = 40
    path = Path("movie.mkv")
    candidates = scanner._map_streams_to_languages(path, [specialized, general])
    assert "en" in candidates
    assert candidates["en"][0] is general


def test_ensure_tagged_streams_marks_language() -> None:
    tagged: list[SubtitleStream] = []

    class DummyTagger:
        extractor = None

        def detect_and_tag(self, path: Path, stream: SubtitleStream) -> DetectionResult:
            tagged.append(stream)
            stream.language = "eng"
            return DetectionResult("eng", 0.6)

    scanner = MkvScanner(
        [],
        cast(MkvSubtitleTagger, DummyTagger()),
        _FallbackTranslator(),
        ensure_langs=["eng"],
        preferred_source="eng",
    )
    stream = SubtitleStream(
        ffprobe_index=1,
        subtitle_index=1,
        codec="srt",
        language=None,
        title=None,
        forced=False,
        default=False,
    )
    scanner._ensure_tagged_streams(Path("video.mkv"), [stream])
    assert stream.language == "eng"
    assert tagged == [stream]


def test_detect_and_tag_uses_title_hint_when_metadata_wrong(
    monkeypatch: MonkeyPatch,
) -> None:
    class FakeExtractor(MkvSubtitleExtractor):
        def __init__(self) -> None:
            pass

        def extract_sample(self, path: Path, stream: SubtitleStream) -> bytes:
            return b"ignored"

    class FakeTranslator:
        def detect_language(
            self, sample: bytes, *, min_confidence: float = 0.0
        ) -> DetectionResult:
            return DetectionResult("en", 0.95)

    applied: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> DummyCompletedProcess:
        applied["cmd"] = cmd
        return DummyCompletedProcess(bytes_stdout=b"")

    monkeypatch.setattr("subprocess.run", fake_run)

    tagger = MkvSubtitleTagger(
        extractor=cast(MkvSubtitleExtractor, FakeExtractor()),
        translator=cast(LibreTranslateClient, FakeTranslator()),
    )
    stream = SubtitleStream(
        ffprobe_index=0,
        subtitle_index=1,
        codec="subrip",
        language="eng",
        title="German",
        forced=False,
        default=False,
    )
    detection = tagger.detect_and_tag(Path("movie.mkv"), stream)

    assert detection is not None
    assert detection.language == "deu"
    assert applied["cmd"][0] == "mkvpropedit"


def test_detect_and_tag_skips_unsupported_codec() -> None:
    class FakeExtractor(MkvSubtitleExtractor):
        def __init__(self) -> None:
            pass

        def extract_sample(self, path: Path, stream: SubtitleStream) -> bytes:
            raise AssertionError("should not sample unsupported codecs")

    class FakeTranslator:
        def detect_language(
            self, sample: bytes, *, min_confidence: float = 0.0
        ) -> DetectionResult:
            raise AssertionError("should not detect unsupported codecs")

    tagger = MkvSubtitleTagger(
        extractor=cast(MkvSubtitleExtractor, FakeExtractor()),
        translator=cast(LibreTranslateClient, FakeTranslator()),
    )
    stream = SubtitleStream(
        ffprobe_index=0,
        subtitle_index=1,
        codec="hdmv_pgs_subtitle",
        language=None,
        title=None,
        forced=False,
        default=False,
    )
    detection = tagger.detect_and_tag(Path("movie.mkv"), stream)
    assert detection is None


def test_ensure_longest_default_sets_flag(monkeypatch: MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> DummyCompletedProcess:
        calls.append(cmd)
        return DummyCompletedProcess(bytes_stdout=b"")

    monkeypatch.setattr("subprocess.run", fake_run)

    tagger = MkvSubtitleTagger(
        extractor=cast(MkvSubtitleExtractor, object()),
        translator=cast(LibreTranslateClient, object()),
    )
    streams = [
        (
            SubtitleStream(
                ffprobe_index=0,
                subtitle_index=1,
                codec="subrip",
                language="eng",
                title=None,
                forced=False,
                default=True,
                duration=50.0,
                char_count=100,
                cue_count=10,
            ),
            "eng",
        ),
        (
            SubtitleStream(
                ffprobe_index=1,
                subtitle_index=2,
                codec="subrip",
                language="eng",
                title=None,
                forced=False,
                default=False,
                duration=60.0,
                char_count=500,
                cue_count=20,
            ),
            "eng",
        ),
    ]

    tagger.ensure_longest_default(Path("movie.mkv"), streams)

    assert len(calls) == 4  # default + forced for each stream


def test_metrics_penalize_forced(monkeypatch: MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> DummyCompletedProcess:
        calls.append(cmd)
        return DummyCompletedProcess(bytes_stdout=b"")

    monkeypatch.setattr("subprocess.run", fake_run)

    tagger = MkvSubtitleTagger(
        extractor=cast(MkvSubtitleExtractor, object()),
        translator=cast(LibreTranslateClient, object()),
    )
    streams = [
        (
            SubtitleStream(
                ffprobe_index=0,
                subtitle_index=1,
                codec="subrip",
                language="eng",
                title=None,
                forced=True,
                default=True,
                duration=100.0,
                char_count=500,
                cue_count=20,
            ),
            "eng",
        ),
        (
            SubtitleStream(
                ffprobe_index=1,
                subtitle_index=2,
                codec="subrip",
                language="eng",
                title=None,
                forced=False,
                default=False,
                duration=50.0,
                char_count=400,
                cue_count=18,
            ),
            "eng",
        ),
    ]

    tagger.ensure_longest_default(Path("movie.mkv"), streams)

    assert len(calls) == 4


def test_non_english_defaults_removed(monkeypatch: MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> DummyCompletedProcess:
        calls.append(cmd)
        return DummyCompletedProcess(bytes_stdout=b"")

    monkeypatch.setattr("subprocess.run", fake_run)

    tagger = MkvSubtitleTagger(
        extractor=cast(MkvSubtitleExtractor, object()),
        translator=cast(LibreTranslateClient, object()),
    )
    streams = [
        (
            SubtitleStream(
                ffprobe_index=0,
                subtitle_index=1,
                codec="subrip",
                language="spa",
                title=None,
                forced=True,
                default=True,
                duration=40.0,
                char_count=200,
                cue_count=8,
            ),
            "spa",
        ),
        (
            SubtitleStream(
                ffprobe_index=1,
                subtitle_index=2,
                codec="subrip",
                language="spa",
                title=None,
                forced=False,
                default=False,
                duration=60.0,
                char_count=400,
                cue_count=12,
            ),
            "spa",
        ),
    ]

    tagger.ensure_longest_default(Path("movie.mkv"), streams)

    assert len(calls) == 4
    assert all("flag-default=0" in cmd for cmd in calls[0::2])
    assert all("flag-forced=0" in cmd for cmd in calls[1::2])


class _TranslationDummyExtractor:
    def __init__(self, temp_dir: Path) -> None:
        self.temp_dir = temp_dir

    def create_temp_path(self, suffix: str) -> Path:
        path = self.temp_dir / f"temp_{uuid.uuid4().hex}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        return path

    def extract_stream(self, path: Path, stream: SubtitleStream, dest: Path) -> None:
        dest.write_bytes(b"dummy")


class _TranslationDummyTranslator:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def translate(self, path: Path, lang: str, *, src_lang: str | None = None) -> bytes:
        return self.payload

    def close(self) -> None:
        return None

    def wait_until_available(self) -> None:
        return None

    def supports_translation(self, src_lang: str, target_lang: str) -> bool:
        return True

    def is_target_supported(self, target_lang: str) -> bool:
        return True


class _TranslationDummyProbeCache:
    def list_streams(self, path: Path) -> list[SubtitleStream]:
        return []

    def invalidate_path(self, path: Path | str) -> None:
        pass

    def delete_entry(self, path: Path) -> None:
        pass


def _build_translation_scanner(
    tmp_path: Path, translator: _TranslationDummyTranslator
) -> MkvScanner:
    extractor = _TranslationDummyExtractor(tmp_path)

    DummyTagger = type("DummyTagger", (), {"extractor": extractor})

    return MkvScanner(
        directories=[str(tmp_path)],
        tagger=cast(MkvSubtitleTagger, DummyTagger()),
        translator=translator,
        ensure_langs=["en"],
        probe_cache=cast(MkvProbeCache, _TranslationDummyProbeCache()),
        cache_enabled=False,
    )


def test_translate_stream_writes_new_subtitle(tmp_path: Path) -> None:
    translator = _TranslationDummyTranslator(b"hello\n")
    scanner = _build_translation_scanner(tmp_path, translator)
    source = tmp_path / "movie.mkv"
    source.write_text("content")
    stream = SubtitleStream(
        ffprobe_index=0,
        subtitle_index=1,
        codec="subrip",
        language="eng",
        title="English",
        forced=False,
        default=False,
    )

    updated = scanner._translate_stream(source, stream, "en", "nl")

    assert updated is True
    assert (tmp_path / "movie.nl.srt").read_bytes() == b"hello\n"


def test_translate_stream_skips_when_content_matches(tmp_path: Path) -> None:
    translator = _TranslationDummyTranslator(b"hello\n")
    scanner = _build_translation_scanner(tmp_path, translator)
    source = tmp_path / "movie.mkv"
    source.write_text("content")
    subtitle = tmp_path / "movie.nl.srt"
    subtitle.write_bytes(b"hello\n")
    older_ts = time.time() - 60
    newer_ts = time.time()
    os.utime(subtitle, (older_ts, older_ts))
    os.utime(source, (newer_ts, newer_ts))
    mkv_mtime_ns = source.stat().st_mtime_ns
    stream = SubtitleStream(
        ffprobe_index=0,
        subtitle_index=1,
        codec="subrip",
        language="eng",
        title="English",
        forced=False,
        default=False,
    )

    updated = scanner._translate_stream(
        source, stream, "en", "nl", mkv_mtime_ns=mkv_mtime_ns
    )

    assert updated is False
    assert subtitle.read_bytes() == b"hello\n"
    assert subtitle.stat().st_mtime_ns >= mkv_mtime_ns
