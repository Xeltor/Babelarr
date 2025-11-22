from __future__ import annotations

from pathlib import Path

from babelarr.mkv import MkvSubtitleExtractor, is_text_subtitle_codec


def test_extractor_lists_and_extracts_subtitles(tmp_path: Path) -> None:
    fixture = Path("tests/fixtures/sample_subs.mkv")
    extractor = MkvSubtitleExtractor()

    streams = extractor.list_streams(fixture)
    # We expect multiple text subtitle streams with languages present.
    assert streams
    assert all(is_text_subtitle_codec(s.codec) for s in streams)
    english = next(stream for stream in streams if stream.language == "eng")

    temp_srt = extractor.create_temp_path(".srt")
    extractor.extract_stream(fixture, english, temp_srt)

    content = temp_srt.read_text(encoding="utf-8", errors="ignore")
    assert "00:00:" in content
    assert content.strip()
