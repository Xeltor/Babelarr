from __future__ import annotations

import logging
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Iterable

from .jellyfin_api import JellyfinClient
from .mkv import (
    MkvSubtitleTagger,
    MkvToolError,
    SubtitleMetrics,
    SubtitleStream,
    language_hint_from_title,
    normalize_language_code,
    normalize_language_code_iso1,
    title_indicates_hearing_impaired,
    is_text_subtitle_codec,
)
from .mkv_probe_cache import MkvProbeCache
from .profiling import WorkloadProfiler
from .translator import LibreTranslateClient
from .ignore import is_path_ignored

logger = logging.getLogger(__name__)
RECENT_PRIORITY_WINDOW_NS = 24 * 60 * 60 * 1_000_000_000


class MkvScanner:
    """Walk configured directories and translate missing subtitle languages."""

    def __init__(
        self,
        directories: list[str],
        tagger: MkvSubtitleTagger,
        translator: LibreTranslateClient,
        *,
        ensure_langs: list[str],
        probe_cache: MkvProbeCache | None = None,
        cache_enabled: bool = True,
        preferred_source: str | None = None,
        profiler: WorkloadProfiler | None = None,
        jellyfin_client: JellyfinClient | None = None,
    ) -> None:
        self.directories = directories
        self.tagger = tagger
        self.translator = translator
        self.ensure_langs = [
            language_iso1
            for lang in ensure_langs
            if (
                language_iso1 := normalize_language_code_iso1(lang)
            )
        ]
        self.cache_enabled = cache_enabled
        self.preferred_source = (
            normalize_language_code(preferred_source) if preferred_source else None
        )
        self.profiler = profiler
        self._probe_cache = probe_cache or MkvProbeCache(
            self.tagger.extractor,
            profiler=profiler,
        )
        self._jellyfin_client = jellyfin_client

    def _profile(self, name: str):
        if not self.profiler:
            return nullcontext()
        return self.profiler.track(name)

    def scan(self) -> tuple[int, list[tuple[Path, int]], list[Path]]:
        file_paths: list[Path] = []
        seen: set[str] = set()
        for root in self.directories:
            root_path = Path(root)
            if not root_path.is_dir():
                logger.warning("skip_missing_dir path=%s reason=not_found", root_path)
                continue
            if is_path_ignored(root_path, root=root_path):
                logger.info("scan_skip_ignored path=%s", root_path)
                continue
            for file_path in root_path.rglob("*.mkv"):
                if is_path_ignored(file_path, root=root_path):
                    continue
                file_paths.append(file_path)
                seen.add(str(file_path))
        recent_threshold_ns = time.time_ns() - RECENT_PRIORITY_WINDOW_NS
        tasks: list[tuple[Path, int]] = []
        recent_paths: list[Path] = []
        with self._profile("mkv.scan.full"):
            for path in file_paths:
                needs_translation, is_recent = self._evaluate_file(path, recent_threshold_ns)
                if not needs_translation:
                    continue
                priority = 0 if is_recent else 1
                tasks.append((path, priority))
                if is_recent:
                    recent_paths.append(path)
        if seen and self.cache_enabled:
            self._probe_cache.prune_entries(seen)
        return len(file_paths), tasks, recent_paths

    def scan_files(self, paths: Iterable[Path]) -> tuple[int, list[tuple[Path, int]]]:
        valid_paths: list[Path] = []
        for raw in paths:
            path = Path(raw)
            if not path.is_file():
                continue
            if path.suffix.lower() != ".mkv":
                continue
            if is_path_ignored(path):
                continue
            valid_paths.append(path)
        recent_threshold_ns = time.time_ns() - RECENT_PRIORITY_WINDOW_NS
        tasks: list[tuple[Path, int]] = []
        with self._profile("mkv.scan.files"):
            for path in valid_paths:
                needs_translation, is_recent = self._evaluate_file(path, recent_threshold_ns)
                if not needs_translation:
                    continue
                priority = 0 if is_recent else 1
                tasks.append((path, priority))
        return len(valid_paths), tasks

    def _evaluate_file(
        self,
        path: Path,
        recent_threshold_ns: int,
    ) -> tuple[bool, bool]:
        try:
            mtime_ns = path.stat().st_mtime_ns
        except FileNotFoundError:
            self._probe_cache.invalidate_path(path)
            return False, False
        is_recent = mtime_ns >= recent_threshold_ns
        if self._sidecars_up_to_date(path, mtime_ns):
            return False, is_recent
        cached_mtime: int | None = None
        cached_langs: set[str] | None = None
        if self.cache_enabled:
            cached_mtime, cached_langs = self._probe_cache.get_entry(path)
        if (
            self.cache_enabled
            and cached_mtime == mtime_ns
            and not self._has_pending_targets(path, mtime_ns, cached_langs)
        ):
            return False, is_recent
        try:
            streams = self._probe_cache.list_streams(path)
        except FileNotFoundError:
            self._probe_cache.invalidate_path(path)
            return False, is_recent
        except MkvToolError as exc:
            logger.error("fail_stream_enum path=%s error=%s", path.name, exc)
            return False, is_recent
        self._ensure_tagged_streams(path, streams)
        language_candidates = self._map_streams_to_languages(path, streams)
        existing_langs = set(language_candidates.keys())
        if self._has_pending_targets(path, mtime_ns, existing_langs):
            return True, is_recent
        return False, is_recent

    def process_file(
        self,
        path: Path,
        *,
        position: int | None = None,
        total_paths: int | None = None,
    ) -> int:
        start = time.monotonic()
        try:
            return self._process_file_impl(path, position=position, total_paths=total_paths)
        finally:
            if self.profiler:
                self.profiler.record("mkv.scan.file", time.monotonic() - start)

    def _process_file_impl(
        self,
        path: Path,
        position: int | None,
        total_paths: int | None,
    ) -> int:
        try:
            mtime_ns = path.stat().st_mtime_ns
        except FileNotFoundError:
            self._probe_cache.invalidate_path(path)
            if self.cache_enabled:
                self._probe_cache.delete_entry(path)
            return 0
        self._log_file_start(path, position=position, total_paths=total_paths)
        cache_state = "disabled"
        cached_langs: set[str] | None = None
        cached: int | None = None
        if self.cache_enabled:
            cached, cached_langs = self._probe_cache.get_entry(path)
            cache_state = "cache_miss"
        streams: list[SubtitleStream]
        try:
            streams = self._probe_cache.list_streams(path)
        except FileNotFoundError:
            if self.cache_enabled:
                self._probe_cache.delete_entry(path)
            self._probe_cache.invalidate_path(path)
            self._log_file_finish(
                path,
                position=position,
                total_paths=total_paths,
                streams=0,
                translated=0,
                cache_state="missing",
                reason="missing_pre_process",
            )
            return 0
        except MkvToolError as exc:
            logger.error("fail_stream_enum path=%s error=%s", path.name, exc)
            self._log_file_finish(
                path,
                position=position,
                total_paths=total_paths,
                streams=0,
                translated=0,
                cache_state=cache_state,
                reason="stream_enum_failed",
            )
            return 0
        self._ensure_tagged_streams(path, streams)
        language_candidates = self._map_streams_to_languages(path, streams)
        existing_langs = set(language_candidates.keys())
        self._cleanup_embedded_sidecars(path, existing_langs)
        sidecars_ready = self._sidecars_up_to_date(path, mtime_ns)
        if sidecars_ready:
            cache_state = "sidecar_skip"
            if self.cache_enabled:
                self._probe_cache.update_entry(
                    path,
                    mtime_ns,
                    languages=cached_langs,
                )
                cache_state = "cache_sidecar_hit"
            self._log_file_finish(
                path,
                position=position,
                total_paths=total_paths,
                streams=0,
                translated=0,
                cache_state=cache_state,
                reason="sidecars_up_to_date",
            )
            return 0
        if (
            self.cache_enabled
            and cached == mtime_ns
            and not self._has_pending_targets(path, mtime_ns, existing_langs)
        ):
            self._log_file_finish(
                path,
                position=position,
                total_paths=total_paths,
                streams=0,
                translated=0,
                cache_state="cache_hit",
                reason="cached",
            )
            return 0
        translated, translation_errors, no_source_targets = self._translate_missing(
            path, language_candidates, mtime_ns, existing_langs
        )
        try:
            updated_mtime = path.stat().st_mtime_ns
        except FileNotFoundError:
            self._probe_cache.invalidate_path(path)
            if self.cache_enabled:
                self._probe_cache.delete_entry(path)
            self._log_file_finish(
                path,
                position=position,
                total_paths=total_paths,
                streams=len(streams),
                translated=translated,
                cache_state="missing",
                reason="missing_post_process",
            )
            return translated
        if self.cache_enabled:
            if translation_errors:
                cache_state = "cache_skipped"
            else:
                cache_languages: Iterable[str] | None = existing_langs or None
                if no_source_targets:
                    cache_languages = set(self.ensure_langs)
                self._probe_cache.update_entry(
                    path,
                    updated_mtime,
                    languages=cache_languages,
                )
                cache_state = "cache_no_source" if no_source_targets else "cache_updated"
        if translation_errors:
            finish_reason = "translation_errors"
        elif no_source_targets:
            finish_reason = "no_source"
        else:
            finish_reason = None
        self._log_file_finish(
            path,
            position=position,
            total_paths=total_paths,
            streams=len(streams),
            translated=translated,
            cache_state=cache_state,
            reason=finish_reason,
        )
        if translated:
            self._notify_jellyfin(path)
        return translated

    def _log_file_start(
        self,
        path: Path,
        position: int | None = None,
        total_paths: int | None = None,
    ) -> None:
        logger.info(
            "file_start path=%s index=%s total=%s",
            path.name,
            position if position is not None else "-",
            total_paths if total_paths is not None else "-",
        )

    def _log_file_finish(
        self,
        path: Path,
        *,
        position: int | None = None,
        total_paths: int | None = None,
        streams: int | None = None,
        translated: int | None = None,
        cache_state: str | None = None,
        reason: str | None = None,
    ) -> None:
        extra_reason = f" reason={reason}" if reason else ""
        logger.info(
            "file_done path=%s index=%s total=%s streams=%s translated=%s cache=%s%s",
            path.name,
            position if position is not None else "-",
            total_paths if total_paths is not None else "-",
            streams if streams is not None else "-",
            translated if translated is not None else "-",
            cache_state or "-",
            extra_reason,
        )

    def _translate_missing(
        self,
        path: Path,
        candidates: dict[str, tuple[SubtitleStream, SubtitleMetrics]],
        mtime_ns: int,
        existing_langs: set[str] | None,
    ) -> tuple[int, bool, bool]:
        translated = 0
        translation_errors = False
        had_pending_translation = False
        had_source_language = False
        extractor = self.tagger.extractor
        extracted_streams: dict[str, Path] = {}
        try:
            for target_lang in self.ensure_langs:
                if not self._needs_translation(path, target_lang, mtime_ns, existing_langs):
                    continue
                had_pending_translation = True
                source_lang, stream = self._pick_source_stream(path, candidates, target_lang)
                if not source_lang or not stream:
                    logger.warning(
                        "skip_translation path=%s target=%s reason=no_source",
                        path.name,
                        target_lang,
                    )
                    continue
                had_source_language = True
                try:
                    source_path: Path | None = None
                    if extractor:
                        key = stream.track_selector
                        source_path = extracted_streams.get(key)
                        if source_path is None:
                            source_path = extractor.create_temp_path(".srt")
                            extractor.extract_stream(path, stream, source_path)
                            extracted_streams[key] = source_path
                    translated_stream = self._translate_stream(
                        path,
                        stream,
                        source_lang,
                        target_lang,
                        source_path=source_path,
                    )
                except FileNotFoundError:
                    logger.info("mkv_missing_during_translation path=%s target=%s", path.name, target_lang)
                    if self.cache_enabled:
                        self._probe_cache.delete_entry(path)
                    self._probe_cache.invalidate_path(path)
                    return translated, True, False
                except Exception as exc:
                    logger.error(
                        "fail_translation path=%s source=%s target=%s error=%s",
                        path.name,
                        source_lang,
                        target_lang,
                        exc,
                    )
                    translation_errors = True
                    continue
                if translated_stream:
                    translated += 1
        finally:
            for temp_path in extracted_streams.values():
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception:
                    pass
        no_source_targets = had_pending_translation and not had_source_language
        return translated, translation_errors, no_source_targets

    def _ensure_tagged_streams(self, path: Path, streams: list[SubtitleStream]) -> None:
        for stream in streams:
            if stream.language:
                continue
            try:
                detection = self.tagger.detect_and_tag(path, stream)
            except MkvToolError as exc:
                logger.error(
                    "fail_tagging path=%s track=%s error=%s",
                    path.name,
                    stream.track_selector,
                    exc,
                )
                continue
            if detection:
                stream.language = detection.language

    def _needs_translation(
        self,
        path: Path,
        lang: str,
        mtime_ns: int,
        existing_langs: Iterable[str] | None = None,
    ) -> bool:
        if existing_langs and lang in existing_langs:
            return False
        dest = self._subtitle_path(path, lang)
        if not dest.exists():
            return True
        try:
            return dest.stat().st_mtime_ns < mtime_ns
        except FileNotFoundError:
            return True

    def _has_pending_targets(
        self,
        path: Path,
        mtime_ns: int,
        existing_langs: Iterable[str] | None = None,
    ) -> bool:
        for lang in self.ensure_langs:
            if self._needs_translation(path, lang, mtime_ns, existing_langs):
                return True
        return False

    @staticmethod
    def _is_specialized_stream(stream: SubtitleStream) -> bool:
        """Return True when a track appears to be specialized (forced/hearing-impaired)."""

        if stream.forced:
            return True
        return title_indicates_hearing_impaired(stream.title)

    @staticmethod
    def _score_with_specialization(
        metrics: SubtitleMetrics, specialized: bool
    ) -> float:
        multiplier = 0.5 if specialized else 1.0
        return metrics.score() * multiplier

    def _map_streams_to_languages(
        self, path: Path, streams: list[SubtitleStream]
    ) -> dict[str, tuple[SubtitleStream, SubtitleMetrics, bool]]:
        candidates: dict[
            str, tuple[SubtitleStream, SubtitleMetrics, bool]
        ] = {}
        for stream in streams:
            codec = (stream.codec or "").lower()
            if not is_text_subtitle_codec(codec):
                continue
            lang_iso2 = self._determine_language(path, stream)
            lang = normalize_language_code_iso1(lang_iso2)
            if not lang:
                continue
            metrics = SubtitleMetrics.from_stream(stream)
            specialized = self._is_specialized_stream(stream)
            previous = candidates.get(lang)
            if previous:
                previous_score = self._score_with_specialization(previous[1], previous[2])
                candidate_score = self._score_with_specialization(metrics, specialized)
                if previous_score >= candidate_score:
                    continue
            candidates[lang] = (stream, metrics, specialized)
        return candidates

    def _determine_language(self, path: Path, stream: SubtitleStream) -> str | None:
        if not stream.language:
            detection = self.tagger.detect_stream_language(path, stream)
            if detection:
                normalized = normalize_language_code(detection.language)
                if normalized:
                    return normalized
        fallback = normalize_language_code(stream.language)
        if fallback:
            return fallback
        hint = language_hint_from_title(stream.title)
        return normalize_language_code(hint)

    def _pick_source_stream(
        self,
        path: Path,
        candidates: dict[str, tuple[SubtitleStream, SubtitleMetrics, bool]],
        target: str,
    ) -> tuple[str | None, SubtitleStream | None]:
        order: list[str] = []
        seen: set[str] = set()
        if self.preferred_source and self.preferred_source in candidates:
            order.append(self.preferred_source)
            seen.add(self.preferred_source)
        for lang in self.ensure_langs:
            if lang == target or lang in seen:
                continue
            if lang not in candidates:
                continue
            order.append(lang)
            seen.add(lang)
        for lang in sorted(candidates):
            if lang == target or lang in seen:
                continue
            order.append(lang)
            seen.add(lang)
        for lang in order:
            if not self.translator.supports_translation(lang, target):
                continue
            if lang not in self.ensure_langs and lang != self.preferred_source:
                logger.info(
                    "using_fallback_source path=%s target=%s source=%s",
                    path.name,
                    target,
                    lang,
                )
            return lang, candidates[lang][0]
        return None, None

    def _translate_stream(
        self,
        path: Path,
        stream: SubtitleStream,
        source_lang: str,
        target_lang: str,
        *,
        source_path: Path | None = None,
    ) -> bool:
        extractor = self.tagger.extractor
        subtitle_blob = self._subtitle_path(path, target_lang)
        if not extractor:
            raise RuntimeError("subtitle extractor is not available")
        if not path.exists():
            raise FileNotFoundError(path)
        temp_path = source_path
        cleanup_temp = False
        if temp_path is None:
            temp_path = extractor.create_temp_path(".srt")
            extractor.extract_stream(path, stream, temp_path)
            cleanup_temp = True
        try:
            translated = self.translator.translate(
                temp_path, target_lang, src_lang=source_lang
            )
            translated = self._sanitize_translated_subtitle(translated)
            existing: bytes | None = None
            if subtitle_blob.exists():
                try:
                    existing = subtitle_blob.read_bytes()
                except Exception:
                    existing = None
            if existing == translated:
                logger.info(
                    "translation_skipped path=%s target=%s source=%s reason=unchanged",
                    path.name,
                    target_lang,
                    source_lang,
                )
                return False
            temp_output = subtitle_blob.with_suffix(subtitle_blob.suffix + ".tmp")
            temp_output.write_bytes(translated)
            temp_output.replace(subtitle_blob)
            logger.info(
                "translation_saved path=%s target=%s source=%s output=%s",
                path.name,
                target_lang,
                source_lang,
                subtitle_blob.name,
            )
            return True
        finally:
            if cleanup_temp:
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception:  # pragma: no cover - cleanup best effort
                    pass

    @staticmethod
    def _sanitize_translated_subtitle(subtitle_bytes: bytes) -> bytes:
        try:
            text = subtitle_bytes.decode("utf-8", errors="replace")
        except Exception:
            return subtitle_bytes
        lines = text.splitlines()
        filtered = [
            line
            for line in lines
            if not line.strip() or any(ch != "#" for ch in line.strip())
        ]
        if not filtered:
            return subtitle_bytes
        return ("\n".join(filtered) + ("\n" if text.endswith("\n") else "")).encode("utf-8")

    def _subtitle_path(self, path: Path, lang: str) -> Path:
        return path.with_suffix(f".{lang}.srt")

    def _notify_jellyfin(self, path: Path) -> None:
        if not self._jellyfin_client:
            return
        try:
            self._jellyfin_client.refresh_path(path)
            logger.info("jellyfin_refresh path=%s", path.name)
        except Exception as exc:
            logger.error(
                "jellyfin_refresh_fail path=%s error=%s",
                path.name,
                exc,
            )

    def _sidecars_up_to_date(self, path: Path, mtime_ns: int) -> bool:
        if not self.ensure_langs:
            return False
        for lang in self.ensure_langs:
            subtitle = self._subtitle_path(path, lang)
            if not subtitle.exists():
                return False
            try:
                if subtitle.stat().st_mtime_ns < mtime_ns:
                    return False
            except FileNotFoundError:
                return False
            except OSError as exc:
                logger.warning(
                    "sidecar_check_failed path=%s target=%s error=%s",
                    path.name,
                    lang,
                    exc,
                )
                return False
        return True

    def _cleanup_embedded_sidecars(
        self,
        path: Path,
        languages: Iterable[str] | None,
    ) -> None:
        if not languages:
            return
        for lang in languages:
            subtitle = self._subtitle_path(path, lang)
            if not subtitle.exists():
                continue
            try:
                subtitle.unlink()
            except Exception as exc:
                logger.warning(
                    "remove_sidecar_failed path=%s target=%s error=%s",
                    path.name,
                    lang,
                    exc,
                )
            else:
                logger.info(
                    "remove_sidecar path=%s target=%s reason=embedded_stream",
                    path.name,
                    lang,
                )
