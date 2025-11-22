from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from babelarr.mkv import SubtitleStream
from babelarr.mkv_scan import MkvScanner
from babelarr.translator import Translator


class _DummyTagger:
    def __init__(self) -> None:
        self.extractor = None

    def detect_stream_language(self, path: Path, stream: SubtitleStream) -> None:
        return None


class _DummyTranslator(Translator):
    def __init__(self) -> None:
        self.supported: set[tuple[str, str]] = set()

    def translate(self, path: Path, lang: str, *, src_lang: str | None = None) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        return None

    def wait_until_available(self) -> None:
        return None

    def supports_translation(self, src_lang: str, target_lang: str) -> bool:
        if not self.supported:
            return True
        return (src_lang, target_lang) in self.supported

    def is_target_supported(self, target_lang: str) -> bool:
        return True


def test_map_streams_prefers_non_specialized(tmp_path: Path) -> None:
    tagger = _DummyTagger()
    translator = _DummyTranslator()
    scanner = MkvScanner(
        directories=[],
        tagger=tagger,
        translator=translator,
        ensure_langs=["es"],
        probe_cache=SimpleNamespace(),
        cache_enabled=False,
        preferred_source="en",
    )
    normal = SubtitleStream(
        ffprobe_index=1,
        subtitle_index=1,
        codec="srt",
        language="en",
        title=None,
        forced=False,
        default=False,
        char_count=10,
        cue_count=1,
    )
    specialized = SubtitleStream(
        ffprobe_index=2,
        subtitle_index=2,
        codec="srt",
        language="en",
        title="HOH",
        forced=False,
        default=False,
        char_count=5,
        cue_count=0,
    )

    candidates = scanner._map_streams_to_languages(tmp_path, [specialized, normal])

    assert candidates["en"][0] is normal


def test_pick_source_stream_ordering(tmp_path: Path) -> None:
    tagger = _DummyTagger()
    translator = _DummyTranslator()
    translator.supported = {("en", "es"), ("de", "es")}
    scanner = MkvScanner(
        directories=[],
        tagger=tagger,
        translator=translator,
        ensure_langs=["es", "fr"],
        probe_cache=SimpleNamespace(),
        cache_enabled=False,
        preferred_source="de",
    )
    stream_en = SubtitleStream(
        ffprobe_index=1,
        subtitle_index=1,
        codec="srt",
        language="en",
        title=None,
        forced=False,
        default=False,
        char_count=5,
        cue_count=1,
    )
    stream_de = SubtitleStream(
        ffprobe_index=2,
        subtitle_index=2,
        codec="srt",
        language="de",
        title=None,
        forced=False,
        default=False,
        char_count=5,
        cue_count=1,
    )
    metrics = scanner._map_streams_to_languages(tmp_path, [stream_en, stream_de])
    source, stream = scanner._pick_source_stream(tmp_path, metrics, target="es")

    assert source == "en"
    assert stream is stream_en


def test_sanitize_translated_subtitle_strips_hash_only_lines() -> None:
    raw = b"1\n00:00:00,000 --> 00:00:01,000\n#####\nHola\n"
    cleaned = MkvScanner._sanitize_translated_subtitle(raw)
    assert b"#####" not in cleaned
