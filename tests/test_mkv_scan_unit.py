from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace

from babelarr.mkv import SubtitleMetrics, SubtitleStream
from babelarr.mkv_scan import MkvScanner
from babelarr.translator import Translator


class _DummyTagger:
    def __init__(self, detection: object | None = None) -> None:
        self.extractor = None

        self._detection = detection

    def detect_stream_language(
        self, path: Path, stream: SubtitleStream
    ) -> object | None:
        return self._detection


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


class _DummyProbeCache:
    def __init__(self) -> None:
        self.pruned: set[str] | None = None

    def prune_entries(self, entries: Iterable[str]) -> None:
        self.pruned = set(entries)


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


def test_scan_collects_tasks(tmp_path: Path) -> None:
    cache = _DummyProbeCache()
    tagger = _DummyTagger()
    translator = _DummyTranslator()
    scanner = MkvScanner(
        directories=[str(tmp_path)],
        tagger=tagger,
        translator=translator,
        ensure_langs=["es"],
        probe_cache=cache,
        cache_enabled=True,
    )
    mkv_file = tmp_path / "movie.mkv"
    mkv_file.write_text("dummy")

    evaluation_calls: list[Path] = []
    priorities: list[int] = []

    def fake_evaluate(path: Path, _) -> tuple[bool, bool, int, int]:
        evaluation_calls.append(path)
        return True, True, 123, 456

    def fake_record(
        path: Path, mtime_ns: int | None, size_bytes: int | None, priority: int
    ) -> None:
        priorities.append(priority)

    scanner._evaluate_file = fake_evaluate
    scanner._record_pending_task = fake_record

    total, tasks, recent = scanner.scan()

    assert total == 1
    assert tasks == [(mkv_file, 0)]
    assert recent == [mkv_file]
    assert evaluation_calls == [mkv_file]
    assert priorities == [0]
    assert cache.pruned == {str(mkv_file)}


def test_scan_files_filters_paths(tmp_path: Path) -> None:
    tagger = _DummyTagger()
    translator = _DummyTranslator()
    scanner = MkvScanner(
        directories=[],
        tagger=tagger,
        translator=translator,
        ensure_langs=["es"],
        probe_cache=SimpleNamespace(),
        cache_enabled=False,
    )
    valid = tmp_path / "movie.mkv"
    valid.write_text("movie")
    tmp_path.joinpath("notes.txt").write_text("skip")

    scanner._evaluate_file = lambda path, _: (True, False, 1, 2)

    total, tasks = scanner.scan_files(
        [valid, tmp_path / "missing.mkv", tmp_path / "notes.txt"]
    )

    assert total == 1
    assert tasks == [(valid, 1)]


def test_determine_language_prefers_detection(tmp_path: Path) -> None:
    detection = SimpleNamespace(language="eng")
    tagger = _DummyTagger(detection=detection)
    translator = _DummyTranslator()
    scanner = MkvScanner(
        directories=[],
        tagger=tagger,
        translator=translator,
        ensure_langs=["es"],
        probe_cache=SimpleNamespace(),
        cache_enabled=False,
    )
    stream = SubtitleStream(
        ffprobe_index=1,
        subtitle_index=1,
        codec="srt",
        language=None,
        title=None,
        forced=False,
        default=False,
        char_count=0,
        cue_count=0,
    )

    assert scanner._determine_language(tmp_path, stream) == "eng"


def test_determine_language_falls_back_to_language_code(tmp_path: Path) -> None:
    tagger = _DummyTagger()
    translator = _DummyTranslator()
    scanner = MkvScanner(
        directories=[],
        tagger=tagger,
        translator=translator,
        ensure_langs=["es"],
        probe_cache=SimpleNamespace(),
        cache_enabled=False,
    )
    stream = SubtitleStream(
        ffprobe_index=1,
        subtitle_index=1,
        codec="srt",
        language="EN",
        title=None,
        forced=False,
        default=False,
        char_count=0,
        cue_count=0,
    )

    assert scanner._determine_language(tmp_path, stream) == "eng"


def test_determine_language_uses_title_hint(tmp_path: Path) -> None:
    tagger = _DummyTagger()
    translator = _DummyTranslator()
    scanner = MkvScanner(
        directories=[],
        tagger=tagger,
        translator=translator,
        ensure_langs=["es"],
        probe_cache=SimpleNamespace(),
        cache_enabled=False,
    )
    stream = SubtitleStream(
        ffprobe_index=1,
        subtitle_index=1,
        codec="srt",
        language=None,
        title="Spanish track",
        forced=False,
        default=False,
        char_count=0,
        cue_count=0,
    )

    assert scanner._determine_language(tmp_path, stream) == "spa"


def test_needs_translation_checks_sidecar(tmp_path: Path) -> None:
    tagger = _DummyTagger()
    translator = _DummyTranslator()
    scanner = MkvScanner(
        directories=[],
        tagger=tagger,
        translator=translator,
        ensure_langs=["es"],
        probe_cache=SimpleNamespace(),
        cache_enabled=False,
    )
    movie = tmp_path / "movie.mkv"
    movie.write_text("movie")
    target_lang = "es"
    dest = scanner._subtitle_path(movie, target_lang)
    dest.write_text("subs")
    mtime_ns = movie.stat().st_mtime_ns
    mtime_sec = mtime_ns / 1_000_000_000
    os.utime(dest, (mtime_sec + 1, mtime_sec + 1))

    assert not scanner._needs_translation(movie, target_lang, mtime_ns)

    dest.unlink()
    assert scanner._needs_translation(movie, target_lang, mtime_ns)


def test_is_specialized_stream_detects_forced_and_title(tmp_path: Path) -> None:
    tagger = _DummyTagger()
    translator = _DummyTranslator()
    scanner = MkvScanner(
        directories=[],
        tagger=tagger,
        translator=translator,
        ensure_langs=["es"],
        probe_cache=SimpleNamespace(),
        cache_enabled=False,
    )
    forced_stream = SubtitleStream(
        ffprobe_index=1,
        subtitle_index=1,
        codec="srt",
        language="en",
        title=None,
        forced=True,
        default=False,
        char_count=0,
        cue_count=0,
    )
    hoh_stream = SubtitleStream(
        ffprobe_index=2,
        subtitle_index=2,
        codec="srt",
        language="en",
        title="SDH track",
        forced=False,
        default=False,
        char_count=0,
        cue_count=0,
    )

    assert scanner._is_specialized_stream(forced_stream)
    assert scanner._is_specialized_stream(hoh_stream)


def test_score_with_specialization_applies_multiplier(tmp_path: Path) -> None:
    tagger = _DummyTagger()
    translator = _DummyTranslator()
    scanner = MkvScanner(
        directories=[],
        tagger=tagger,
        translator=translator,
        ensure_langs=["es"],
        probe_cache=SimpleNamespace(),
        cache_enabled=False,
    )
    metrics = SubtitleMetrics(
        char_count=10,
        cue_count=2,
        duration=5.0,
        forced=False,
    )
    base_score = metrics.score()

    assert scanner._score_with_specialization(metrics, False) == base_score
    assert scanner._score_with_specialization(metrics, True) == base_score * 0.5
