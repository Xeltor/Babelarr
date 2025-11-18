from __future__ import annotations

import json
import logging
import re
import subprocess
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path

from .translator import DetectionResult, LibreTranslateClient

logger = logging.getLogger(__name__)


class MkvToolError(RuntimeError):
    """Raised when ffprobe/ffmpeg/mkvpropedit fails."""


@dataclass
class SubtitleStream:
    """Metadata about a subtitle stream within an MKV container."""

    ffprobe_index: int
    subtitle_index: int
    codec: str | None
    language: str | None
    title: str | None
    forced: bool
    default: bool
    duration: float | None = None
    char_count: int = 0
    cue_count: int = 0

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
        sample_bytes: int = 0,
    ) -> None:
        self.ffprobe_path = ffprobe_path
        self.ffmpeg_path = ffmpeg_path
        self.sample_bytes = sample_bytes
        self._copy_codecs = {
            "subrip",
            "srt",
            "webvtt",
            "text",
            "mov_text",
        }
        self._transcode_codecs = {
            "ass",
            "ssa",
        }

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
            duration = self._parse_duration(raw.get("duration") or tags.get("DURATION"))
            streams.append(
                SubtitleStream(
                    ffprobe_index=stream_index,
                    subtitle_index=idx,
                    codec=codec,
                    language=language,
                    title=title,
                    forced=bool(dispositions.get("forced")),
                    default=bool(dispositions.get("default")),
                    duration=duration,
                )
            )
        return streams

    @staticmethod
    def _parse_duration(value: str | float | None) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            if isinstance(value, str) and ":" in value:
                parts = value.split(":")
                if len(parts) == 3:
                    try:
                        hours = float(parts[0])
                        minutes = float(parts[1])
                        seconds = float(parts[2])
                        return hours * 3600 + minutes * 60 + seconds
                    except ValueError:
                        return None
        return None

    def extract_sample(self, path: Path, stream: SubtitleStream) -> bytes:
        """Return a small sample of the subtitle stream using ffmpeg."""

        codec = (stream.codec or "").lower()
        output_format = "srt"
        copy_mode = "copy"
        if codec and codec not in self._copy_codecs:
            logger.info(
                "transcode_sample path=%s track=%s codec=%s",
                path.name,
                stream.track_selector,
                codec,
            )
            copy_mode = "srt"
            output_format = "srt"
        cmd = [
            self.ffmpeg_path,
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            f"0:s:{stream.subtitle_index - 1}",
        ]
        if copy_mode == "copy":
            cmd.extend(["-c", "copy"])
        else:
            cmd.extend(["-c:s", copy_mode])
        cmd.extend(
            [
                "-f",
                output_format,
                "-",
            ]
        )
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
        data = result.stdout
        sample = (
            data
            if self.sample_bytes <= 0
            else data[: self.sample_bytes]
        )
        stats = self._compute_sample_stats(data)
        stream.char_count = stats["char_count"]
        stream.cue_count = stats["cue_count"]
        return sample

    @staticmethod
    def _compute_sample_stats(content: bytes) -> dict[str, int]:
        text = content.decode("utf-8", errors="ignore")
        cues = 0
        chars = 0
        for block in text.split("\n\n"):
            if "-->" in block:
                cues += 1
                # count text lines after timestamp
                lines = block.splitlines()
                for line in lines:
                    if "-->" in line:
                        continue
                    chars += len(line.strip())
        return {"cue_count": cues, "char_count": chars}

    def extract_stream(self, path: Path, stream: SubtitleStream, output_path: Path) -> None:
        """Extract the subtitle stream into a file."""

        codec = (stream.codec or "").lower()
        output_format = "srt"
        copy_mode = "copy"
        if codec and codec not in self._copy_codecs:
            logger.info(
                "transcode_stream path=%s track=%s codec=%s",
                path.name,
                stream.track_selector,
                codec,
            )
            copy_mode = "srt"
        cmd = [
            self.ffmpeg_path,
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            f"0:s:{stream.subtitle_index - 1}",
        ]
        if copy_mode == "copy":
            cmd.extend(["-c", "copy"])
        else:
            cmd.extend(["-c:s", copy_mode])
        cmd.extend(
            [
                "-f",
                output_format,
                "-y",
                str(output_path),
            ]
        )
        if not path.exists():
            raise FileNotFoundError(path)
        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:  # pragma: no cover - passthrough
            if not path.exists():
                raise FileNotFoundError(path) from exc
            stderr = (exc.stderr or b"").decode("utf-8", errors="ignore").strip()
            raise MkvToolError(
                f"ffmpeg failed for {path} track={stream.track_selector} stderr={stderr}"
            ) from exc


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

ISO639_ALIASES = {
    "dut": "nld",
    "ger": "deu",
    "fre": "fra",
    "per": "fas",
    "iw": "heb",
    "scc": "srp",
}


LANGUAGE_TITLE_HINTS: list[tuple[str, str]] = [
    ("latin american spanish", "spa"),
    ("european spanish", "spa"),
    ("brazilian portuguese", "por"),
    ("bahasa indonesia", "ind"),
    ("bahasa melayu", "msa"),
    ("english signs", "eng"),
    ("english cc", "eng"),
    ("简体中文", "zho"),
    ("繁體中文", "zho"),
    ("中文", "zho"),
    ("cantonese", "zho"),
    ("mandarin", "zho"),
    ("arabic", "ara"),
    ("spanish", "spa"),
    ("german", "deu"),
    ("deutsch", "deu"),
    ("french", "fra"),
    ("italian", "ita"),
    ("polish", "pol"),
    ("portuguese", "por"),
    ("russian", "rus"),
    ("turkish", "tur"),
    ("thai", "tha"),
    ("malay", "msa"),
    ("indonesian", "ind"),
    ("vietnamese", "vie"),
    ("tiếng việt", "vie"),
    ("korean", "kor"),
    ("japanese", "jpn"),
    ("dutch", "nld"),
    ("czech", "ces"),
    ("danish", "dan"),
    ("ukrainian", "ukr"),
    ("swedish", "swe"),
    ("norwegian", "nob"),
    ("finnish", "fin"),
    ("english", "eng"),
    ("ger", "deu"),
    ("eng", "eng"),
    ("fre", "fra"),
    ("spa", "spa"),
    ("por", "por"),
    ("ita", "ita"),
    ("rus", "rus"),
    ("ara", "ara"),
]


_LANGUAGE_HINT_PATTERNS = [
    (re.compile(rf"(?<!\w){re.escape(needle)}(?!\w)", re.IGNORECASE), code)
    for needle, code in LANGUAGE_TITLE_HINTS
]


_HEARING_IMPAIRED_PATTERNS = [
    re.compile(r"(?<!\w)sdh(?!\w)", re.IGNORECASE),
    re.compile(r"(?<!\w)hearing impaired(?!\w)", re.IGNORECASE),
    re.compile(r"(?<!\w)hard of hearing(?!\w)", re.IGNORECASE),
    re.compile(r"(?<!\w)deaf(?!\w)", re.IGNORECASE),
    re.compile(r"(?<!\w)closed captions?(?!\w)", re.IGNORECASE),
]


ISO639_2_TO_1 = {v: k for k, v in ISO639_1_TO_2.items()}


@lru_cache(maxsize=512)
def _normalize_language_code_from_normalized(normalized: str) -> str:
    alias = ISO639_ALIASES.get(normalized)
    if alias:
        return alias
    if len(normalized) == 2:
        return ISO639_1_TO_2.get(normalized, normalized)
    if len(normalized) == 3:
        return normalized
    return normalized


def normalize_language_code(code: str | None) -> str | None:
    """Return a mkvpropedit-friendly ISO-639-2 code."""

    if not code:
        return None
    normalized = code.strip().lower()
    if not normalized:
        return normalized
    return _normalize_language_code_from_normalized(normalized)


def normalize_language_code_iso1(code: str | None) -> str | None:
    """Return a LibreTranslate-friendly ISO-639-1 code."""

    iso2 = normalize_language_code(code)
    if not iso2:
        return None
    return ISO639_2_TO_1.get(iso2, iso2)


@lru_cache(maxsize=512)
def _language_hint_from_normalized_title(title: str) -> str | None:
    for pattern, code in _LANGUAGE_HINT_PATTERNS:
        if pattern.search(title):
            return code
    return None


def language_hint_from_title(title: str | None) -> str | None:
    """Attempt to infer the language code from a track title."""

    if not title:
        return None
    stripped = title.strip()
    if not stripped:
        return None
    return _language_hint_from_normalized_title(stripped)


def title_indicates_hearing_impaired(title: str | None) -> bool:
    """Return True when the track title suggests a hearing-impaired subtitle."""

    if not title:
        return False
    stripped = title.strip()
    if not stripped:
        return False
    lower_title = stripped.lower()
    for pattern in _HEARING_IMPAIRED_PATTERNS:
        if pattern.search(lower_title):
            return True
    return False


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

        self._copy_codecs = {"subrip", "srt", "webvtt", "text", "mov_text"}
        self._transcode_codecs = {"ass", "ssa"}
        self._score_forced_penalty = 0.2
        self._score_duration_weight = 0.1
        self._score_cue_weight = 5.0

    def _is_supported_codec(self, stream: SubtitleStream) -> bool:
        codec = (stream.codec or "").lower()
        return (
            not codec
            or codec in self._copy_codecs
            or codec in self._transcode_codecs
        )

    def detect_stream_language(self, path: Path, stream: SubtitleStream) -> DetectionResult | None:
        """Return the detected language for *stream* or ``None``."""

        if not self._is_supported_codec(stream):
            logger.info(
                "skip_detection path=%s track=%s reason=unsupported_codec codec=%s",
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
        current_code = normalize_language_code(stream.language)
        hint_code = language_hint_from_title(stream.title)
        if hint_code:
            hint_code = normalize_language_code(hint_code)
        iso_code = (
            normalize_language_code(detection.language) if detection else None
        )

        if detection and not iso_code:
            logger.info(
                "skip_detection path=%s track=%s reason=unknown_code",
                path.name,
                stream.track_selector,
            )
            detection = None
            iso_code = None

        selected_code = None
        selected_source = None

        if iso_code and iso_code != current_code:
            selected_code = iso_code
            selected_source = "detection"
        elif hint_code and hint_code != current_code:
            selected_code = hint_code
            selected_source = "title_hint"

        if not selected_code:
            if detection:
                logger.info(
                    "skip_detection path=%s track=%s reason=already_tagged language=%s",
                    path.name,
                    stream.track_selector,
                    current_code or "unknown",
                )
            else:
                logger.debug(
                    "skip_detection path=%s track=%s reason=no_detection",
                    path.name,
                    stream.track_selector,
                )
            return None

        self._apply_language_tag(path, stream, selected_code)
        stream.language = selected_code

        confidence = detection.confidence if detection else 1.0
        detection = DetectionResult(selected_code, confidence)

        logger.info(
            "apply_tag path=%s track=%s language=%s confidence=%.3f source=%s",
            path.name,
            stream.track_selector,
            selected_code,
            confidence,
            selected_source,
        )
        return detection

    def ensure_longest_default(
        self, path: Path, streams: list[tuple[SubtitleStream, str | None]]
    ) -> None:
        groups: dict[
            str, list[tuple[SubtitleStream, SubtitleMetrics, bool]]
        ] = {}
        for stream, lang in streams:
            if not lang:
                continue
            metrics = SubtitleMetrics.from_stream(stream)
            hearing_impaired = title_indicates_hearing_impaired(stream.title)
            groups.setdefault(lang, []).append(
                (stream, metrics, hearing_impaired)
            )
        for lang, items in groups.items():
            if not items:
                continue
            is_english = lang == "eng"
            best_stream = None
            if is_english:
                non_hearing_impaired = [
                    entry for entry in items if not entry[2]
                ]
                best_entry = max(
                    non_hearing_impaired,
                    key=lambda entry: entry[1].score(),
                    default=None,
                )
                if best_entry is None:
                    best_entry = max(
                        items,
                        key=lambda entry: entry[1].score(),
                        default=None,
                    )
                if best_entry:
                    best_stream = best_entry[0]
            for stream, metrics, _ in items:
                desired = 1 if (is_english and stream is best_stream) else 0
                self._set_default_flag(path, stream, desired)
                self._set_forced_flag(path, stream, 0)
                logger.info(
                    "update_default path=%s track=%s language=%s default=%d forced=%d score=%.1f cues=%d chars=%d forced_src=%s english=%s",
                    path.name,
                    stream.track_selector,
                    lang,
                    desired,
                    0,
                    metrics.score(),
                    metrics.cue_count,
                    metrics.char_count,
                    metrics.forced,
                    is_english,
                )

    def _set_default_flag(self, path: Path, stream: SubtitleStream, value: int) -> None:
        cmd = [
            self.mkvpropedit_path,
            str(path),
            "--edit",
            stream.track_selector,
            "--set",
            f"flag-default={value}",
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as exc:  # pragma: no cover - passthrough
            raise MkvToolError(f"mkvpropedit failed for {path}") from exc

    def _set_forced_flag(self, path: Path, stream: SubtitleStream, value: int) -> None:
        cmd = [
            self.mkvpropedit_path,
            str(path),
            "--edit",
            stream.track_selector,
            "--set",
            f"flag-forced={value}",
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as exc:  # pragma: no cover
            raise MkvToolError(f"mkvpropedit failed for {path}") from exc
@dataclass
class SubtitleMetrics:
    char_count: int
    cue_count: int
    duration: float | None
    forced: bool

    def score(self) -> float:
        score = (
            self.char_count
            + self.cue_count * 5.0
            + (self.duration or 0.0) * 0.1
        )
        if self.forced:
            score *= 0.2
        return score

    @classmethod
    def from_stream(cls, stream: SubtitleStream) -> "SubtitleMetrics":
        return cls(
            char_count=stream.char_count,
            cue_count=stream.cue_count,
            duration=stream.duration,
            forced=stream.forced,
        )
