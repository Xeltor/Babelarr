import json
from pathlib import Path
from typing import cast

import pytest

from babelarr.mkv import (
    DetectionResult,
    MkvSubtitleExtractor,
    MkvSubtitleTagger,
    SubtitleStream,
    normalize_language_code,
)
from babelarr.translator import LibreTranslateClient


class DummyCompletedProcess:
    def __init__(self, stdout: str = "", bytes_stdout: bytes | None = None):
        self.stdout = bytes_stdout if bytes_stdout is not None else stdout
        self.stderr = ""
        self.returncode = 0
        self.bytes_stdout = bytes_stdout


def test_list_streams_parses_ffprobe(monkeypatch):
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

    def fake_run(cmd, **kwargs):
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


def test_extract_sample_invokes_ffmpeg(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return DummyCompletedProcess(bytes_stdout=b"sample data")

    monkeypatch.setattr("subprocess.run", fake_run)

    extractor = MkvSubtitleExtractor()
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


def test_normalize_language_code_handles_two_letter():
    assert normalize_language_code("EN") == "eng"
    assert normalize_language_code("eng") == "eng"
    assert normalize_language_code(None) is None


def test_detect_and_tag_applies_new_language(monkeypatch):
    sample_calls = []

    class FakeExtractor(MkvSubtitleExtractor):
        def __init__(self):
            pass

        def extract_sample(self, path, stream):
            sample_calls.append((path, stream))
            return b"hello"

    class FakeTranslator:
        def detect_language(self, sample, *, min_confidence=0.0):
            assert sample == b"hello"
            assert min_confidence == pytest.approx(0.9)
            return DetectionResult("en", 0.93)

    mkv_calls = []

    def fake_run(cmd, **kwargs):
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


def test_detect_and_tag_skips_existing_language(monkeypatch):
    class FakeExtractor(MkvSubtitleExtractor):
        def __init__(self):
            pass

        def extract_sample(self, path, stream):
            return b"ignored"

    class FakeTranslator:
        def detect_language(self, sample, *, min_confidence=0.0):
            return DetectionResult("en", 0.95)

    monkeypatch.setattr("subprocess.run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("should not run")))

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


def test_detect_and_tag_skips_unsupported_codec():
    class FakeExtractor(MkvSubtitleExtractor):
        def __init__(self):
            pass

        def extract_sample(self, path, stream):
            raise AssertionError("should not sample unsupported codecs")

    class FakeTranslator:
        def detect_language(self, sample, *, min_confidence=0.0):
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
