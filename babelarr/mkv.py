from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .translator import DetectionResult, LibreTranslateClient

logger = logging.getLogger(__name__)


class MkvToolError(RuntimeError):
    """Raised when ffprobe/ffmpeg/mkvpropedit fails."""


@dataclass(frozen=True)
class SubtitleStream:
    """Metadata about a subtitle stream within an MKV container."""

    ffprobe_index: int
    subtitle_index: int
    codec: str | None
    language: str | None
    title: str | None
    forced: bool
    default: bool

    @property
    def track_selector(self) -> str:
        """Return mkvpropedit-compatible track selector."""

        return f"track:s{self.subtitle_index}"


class MkvSubtitleExtractor:
    """Helper for enumerating and sampling subtitle streams from MKV files."""

    def __init__(
        self,
        ffprobe_path: str = "ffprobe",
        ffmpeg_path: str = "ffmpeg",
        sample_bytes: int = 8192,
    ) -> None:
        self.ffprobe_path = ffprobe_path
        self.ffmpeg_path = ffmpeg_path
        self.sample_bytes = sample_bytes

    def list_streams(self, path: Path) -> list[SubtitleStream]:
        """Return subtitle streams discovered via ffprobe."""

        cmd = [
            self.ffprobe_path,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_streams",
            "-select_streams",
            "s",
            str(path),
        ]
        try:
            result = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:  # pragma: no cover - passthrough
            raise MkvToolError(f"ffprobe failed for {path}") from exc

        payload = json.loads(result.stdout or "{}")
        streams: list[SubtitleStream] = []
        for idx, raw in enumerate(payload.get("streams", []), start=1):
            tags = raw.get("tags") or {}
            dispositions = raw.get("disposition") or {}
            language = (tags.get("language") or tags.get("LANGUAGE") or "").strip()
            language = language.lower() or None
            title = (tags.get("title") or tags.get("TITLE") or "").strip() or None
            codec = raw.get("codec_name")
            stream_index = int(raw.get("index", idx))
            streams.append(
                SubtitleStream(
                    ffprobe_index=stream_index,
                    subtitle_index=idx,
                    codec=codec,
                    language=language,
                    title=title,
                    forced=bool(dispositions.get("forced")),
                    default=bool(dispositions.get("default")),
                )
            )
        return streams

    def extract_sample(self, path: Path, stream: SubtitleStream) -> bytes:
        """Return a small sample of the subtitle stream using ffmpeg."""

        cmd = [
            self.ffmpeg_path,
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            f"0:s:{stream.subtitle_index - 1}",
            "-c",
            "copy",
            "-f",
            "srt",
            "-",
        ]
        try:
            result = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:  # pragma: no cover - passthrough
            stderr = (exc.stderr or b"").decode("utf-8", errors="ignore").strip()
            raise MkvToolError(
                f"ffmpeg failed for {path} track={stream.track_selector} stderr={stderr}"
            ) from exc
        sample = result.stdout[: self.sample_bytes]
        return sample


ISO639_1_TO_2 = {
    "aa": "aar",
    "ab": "abk",
    "af": "afr",
    "am": "amh",
    "ar": "ara",
    "az": "aze",
    "be": "bel",
    "bg": "bul",
    "bn": "ben",
    "bs": "bos",
    "ca": "cat",
    "cs": "ces",
    "cy": "cym",
    "da": "dan",
    "de": "deu",
    "dv": "div",
    "dz": "dzo",
    "el": "ell",
    "en": "eng",
    "es": "spa",
    "et": "est",
    "eu": "eus",
    "fa": "fas",
    "fi": "fin",
    "fr": "fra",
    "ga": "gle",
    "he": "heb",
    "hi": "hin",
    "hr": "hrv",
    "hu": "hun",
    "hy": "hye",
    "id": "ind",
    "is": "isl",
    "it": "ita",
    "ja": "jpn",
    "ka": "kat",
    "kk": "kaz",
    "ko": "kor",
    "la": "lat",
    "lb": "ltz",
    "lt": "lit",
    "lv": "lav",
    "mk": "mkd",
    "mn": "mon",
    "ms": "msa",
    "nb": "nob",
    "nl": "nld",
    "pl": "pol",
    "pt": "por",
    "ro": "ron",
    "ru": "rus",
    "sk": "slk",
    "sl": "slv",
    "sq": "sqi",
    "sr": "srp",
    "sv": "swe",
    "th": "tha",
    "tr": "tur",
    "uk": "ukr",
    "ur": "urd",
    "vi": "vie",
    "zh": "zho",
}


def normalize_language_code(code: str | None) -> str | None:
    """Return a mkvpropedit-friendly ISO-639-2 code."""

    if not code:
        return None
    normalized = code.strip().lower()
    if len(normalized) == 2:
        return ISO639_1_TO_2.get(normalized, normalized)
    if len(normalized) == 3:
        return normalized
    return normalized


class MkvSubtitleTagger:
    """High-level helper for detecting and tagging MKV subtitle streams."""

    def __init__(
        self,
        extractor: MkvSubtitleExtractor,
        translator: LibreTranslateClient,
        *,
        mkvpropedit_path: str = "mkvpropedit",
        min_confidence: float = 0.85,
    ) -> None:
        self.extractor = extractor
        self.translator = translator
        self.mkvpropedit_path = mkvpropedit_path
        self.min_confidence = min_confidence

        self._text_codecs = {
            "subrip",
            "srt",
            "ass",
            "ssa",
            "webvtt",
            "text",
            "mov_text",
        }

    def _is_supported_codec(self, stream: SubtitleStream) -> bool:
        codec = (stream.codec or "").lower()
        return not codec or codec in self._text_codecs

    def detect_stream_language(self, path: Path, stream: SubtitleStream) -> DetectionResult | None:
        """Return the detected language for *stream* or ``None``."""

        if not self._is_supported_codec(stream):
            logger.debug(
                "mkv_detect_skip path=%s track=%s reason=unsupported_codec codec=%s",
                path.name,
                stream.track_selector,
                stream.codec or "unknown",
            )
            return None
        sample = self.extractor.extract_sample(path, stream)
        return self.translator.detect_language(
            sample, min_confidence=self.min_confidence
        )

    def _apply_language_tag(
        self, path: Path, stream: SubtitleStream, language_code: str
    ) -> None:
        cmd = [
            self.mkvpropedit_path,
            str(path),
            "--edit",
            stream.track_selector,
            "--set",
            f"language={language_code}",
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as exc:  # pragma: no cover - passthrough
            raise MkvToolError(f"mkvpropedit failed for {path}") from exc

    def detect_and_tag(self, path: Path, stream: SubtitleStream) -> DetectionResult | None:
        """Detect language and apply track tag when necessary."""

        detection = self.detect_stream_language(path, stream)
        if not detection:
            logger.debug(
                "mkv_detect_skip path=%s track=%s reason=no_detection",
                path.name,
                stream.track_selector,
            )
            return None
        iso_code = normalize_language_code(detection.language)
        if not iso_code:
            logger.debug(
                "mkv_detect_skip path=%s track=%s reason=unknown_code",
                path.name,
                stream.track_selector,
            )
            return None
        current_code = normalize_language_code(stream.language)
        if current_code == iso_code:
            logger.debug(
                "mkv_detect_skip path=%s track=%s reason=already_tagged language=%s",
                path.name,
                stream.track_selector,
                iso_code,
            )
            return None
        self._apply_language_tag(path, stream, iso_code)
        logger.info(
            "mkv_tag_applied path=%s track=%s language=%s confidence=%.3f",
            path.name,
            stream.track_selector,
            iso_code,
            detection.confidence,
        )
        return detection
