from __future__ import annotations

import json
import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

from .mkv import (
    BITMAP_SUBTITLE_CODECS,
    MkvSubtitleTagger,
    MkvToolError,
    SubtitleMetrics,
    SubtitleStream,
    language_hint_from_title,
    normalize_language_code,
    normalize_language_code_iso1,
)
from .translator import LibreTranslateClient

logger = logging.getLogger(__name__)


class MkvCache:
    """SQLite-backed store for MKV metadata to avoid reprocessing."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        try:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS cache (path TEXT PRIMARY KEY, mtime_ns INTEGER, languages TEXT, streams TEXT)"
            )
        except sqlite3.DatabaseError as exc:
            logger.warning(
                "rebuild_cache path=%s reason=%s", self.path, exc
            )
            self._conn.close()
            try:
                self.path.unlink(missing_ok=True)
            except Exception:
                pass
            self._conn = sqlite3.connect(
                str(self.path), check_same_thread=False, isolation_level=None
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS cache (path TEXT PRIMARY KEY, mtime_ns INTEGER, languages TEXT, streams TEXT)"
            )
        finally:
            self._ensure_columns()

    def _ensure_columns(self) -> None:
        cursor = self._conn.execute("PRAGMA table_info(cache)")
        columns = [row[1] for row in cursor.fetchall()]
        if "languages" not in columns:
            self._conn.execute("ALTER TABLE cache ADD COLUMN languages TEXT")
        if "streams" not in columns:
            self._conn.execute("ALTER TABLE cache ADD COLUMN streams TEXT")

    def _execute(self, query: str, params: tuple = ()):
        with self._lock:
            cursor = self._conn.execute(query, params)
            return cursor

    def get_mtime(self, path: Path) -> int | None:
        cursor = self._execute("SELECT mtime_ns FROM cache WHERE path = ?", (str(path),))
        row = cursor.fetchone()
        if row is None:
            logger.debug("get_cache path=%s result=%s", path, None)
            return None
        try:
            value = int(row[0])
        except (TypeError, ValueError):
            logger.warning("invalidate_cache path=%s value=%s", path, row[0])
            return None
        logger.debug("get_cache path=%s result=%d", path, value)
        return value

    def get_languages(self, path: Path) -> set[str]:
        cursor = self._execute("SELECT languages FROM cache WHERE path = ?", (str(path),))
        row = cursor.fetchone()
        if not row or not row[0]:
            return set()
        try:
            langs = json.loads(row[0])
        except ValueError:
            logger.warning("decode_languages_failed path=%s data=%s", path, row[0])
            return set()
        if not isinstance(langs, list):
            return set()
        return {str(entry) for entry in langs if isinstance(entry, str)}

    def get_streams(self, path: Path) -> list[dict[str, object]]:
        cursor = self._execute("SELECT streams FROM cache WHERE path = ?", (str(path),))
        row = cursor.fetchone()
        if not row or not row[0]:
            return []
        try:
            streams = json.loads(row[0])
        except ValueError:
            logger.warning("decode_streams_failed path=%s data=%s", path, row[0])
            return []
        if not isinstance(streams, list):
            return []
        valid_streams: list[dict[str, object]] = []
        for entry in streams:
            if isinstance(entry, dict):
                valid_streams.append(entry)
        return valid_streams

    def _encode_languages(self, languages: Iterable[str] | None) -> str | None:
        if not languages:
            return None
        normalized = sorted({lang for lang in languages if lang})
        if not normalized:
            return None
        return json.dumps(normalized)

    def _encode_streams(self, streams: Iterable[dict[str, object]] | None) -> str | None:
        if not streams:
            return None
        normalized: list[dict[str, object]] = []
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            normalized.append(stream)
        if not normalized:
            return None
        return json.dumps(normalized)

    def update(
        self,
        path: Path,
        mtime_ns: int,
        *,
        languages: Iterable[str] | None = None,
        streams: Iterable[dict[str, object]] | None = None,
    ) -> None:
        self._execute(
            "INSERT INTO cache (path, mtime_ns, languages, streams) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET mtime_ns=excluded.mtime_ns, languages=excluded.languages, streams=excluded.streams",
            (
                str(path),
                int(mtime_ns),
                self._encode_languages(languages),
                self._encode_streams(streams),
            ),
        )
        logger.debug("update_cache path=%s mtime_ns=%d", path, mtime_ns)

    def delete(self, path: Path) -> None:
        self._execute("DELETE FROM cache WHERE path = ?", (str(path),))
        logger.debug("delete_cache path=%s", path)

    def prune(self, valid_paths: Iterable[str]) -> None:
        valid_tuple = tuple(valid_paths)
        if not valid_tuple:
            delete_sql = "DELETE FROM cache"
            params: tuple[str, ...] = ()
        else:
            placeholders = ",".join("?" for _ in valid_tuple)
            delete_sql = f"DELETE FROM cache WHERE path NOT IN ({placeholders})"
            params = valid_tuple
        with self._lock:
            cursor = self._conn.execute(delete_sql, params)
            removed = cursor.rowcount or 0
            if not removed:
                return
            cursor = self._conn.execute("SELECT COUNT(*) FROM cache")
            row = cursor.fetchone()
            remaining = int(row[0]) if row and row[0] is not None else 0
        logger.debug(
            "prune_cache removed=%d remaining=%d",
            removed,
            remaining,
        )


class MkvScanner:
    """Walk configured directories and translate missing subtitle languages."""

    def __init__(
        self,
        directories: list[str],
        tagger: MkvSubtitleTagger,
        translator: LibreTranslateClient,
        *,
        ensure_langs: list[str],
        cache: MkvCache | None,
        preferred_source: str | None = None,
        worker_count: int = 1,
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
        self.cache = cache
        self.preferred_source = (
            normalize_language_code(preferred_source) if preferred_source else None
        )
        self.worker_count = max(1, worker_count)

    def scan(self) -> tuple[int, int]:
        file_paths: list[Path] = []
        seen: set[str] = set()
        for root in self.directories:
            root_path = Path(root)
            if not root_path.is_dir():
                logger.warning("skip_missing_dir path=%s reason=not_found", root_path)
                continue
            for file_path in root_path.rglob("*.mkv"):
                file_paths.append(file_path)
                seen.add(str(file_path))
        translated = self._process_paths(file_paths)
        if seen and self.cache:
            self.cache.prune(seen)
        return len(file_paths), translated

    def scan_files(self, paths: Iterable[Path]) -> tuple[int, int]:
        valid_paths: list[Path] = []
        for raw in paths:
            path = Path(raw)
            if not path.is_file():
                continue
            if path.suffix.lower() != ".mkv":
                continue
            valid_paths.append(path)
        translated = self._process_paths(valid_paths)
        return len(valid_paths), translated

    def _process_paths(self, paths: list[Path]) -> int:
        if not paths:
            return 0
        total_paths = len(paths)
        if self.worker_count <= 1:
            total = 0
            for idx, path in enumerate(paths, start=1):
                total += self._process_file(path, idx, total_paths)
            return total

        total = 0
        with ThreadPoolExecutor(max_workers=self.worker_count) as executor:
            future_to_path = {
                executor.submit(self._process_file, path, idx, total_paths): path
                for idx, path in enumerate(paths, start=1)
            }
            for future in as_completed(future_to_path):
                path = future_to_path[future]
                try:
                    total += future.result()
                except Exception as exc:
                    logger.error("fail_process_file path=%s error=%s", path, exc)
        return total

    def _process_file(self, path: Path, position: int, total_paths: int) -> int:
        try:
            mtime_ns = path.stat().st_mtime_ns
        except FileNotFoundError:
            if self.cache:
                self.cache.delete(path)
            return 0
        self._log_file_start(path, position, total_paths)
        cache_state = "disabled"
        cached_langs: set[str] | None = None
        cached: int | None = None
        if self.cache:
            cached = self.cache.get_mtime(path)
            cached_langs = self.cache.get_languages(path)
            cache_state = "cache_miss"
        sidecars_ready = self._sidecars_up_to_date(path, mtime_ns)
        if sidecars_ready:
            cache_state = "sidecar_skip"
            if self.cache:
                cached_stream_data = self.cache.get_streams(path)
                self.cache.update(
                    path,
                    mtime_ns,
                    languages=cached_langs,
                    streams=cached_stream_data,
                )
                cache_state = "cache_sidecar_hit"
            self._log_file_finish(
                path,
                position,
                total_paths,
                streams=0,
                translated=0,
                cache_state=cache_state,
                reason="sidecars_up_to_date",
            )
            return 0
        if self.cache and cached == mtime_ns and not self._has_pending_targets(
            path, mtime_ns, cached_langs
        ):
            self._log_file_finish(
                path,
                position,
                total_paths,
                streams=0,
                translated=0,
                cache_state="cache_hit",
                reason="cached",
            )
            return 0
        streams: list[SubtitleStream]
        reused_streams = False
        if self.cache and cached == mtime_ns:
            cached_stream_data = self.cache.get_streams(path)
            if cached_stream_data:
                try:
                    streams = [
                        SubtitleStream.from_cache_dict(entry)
                        for entry in cached_stream_data
                    ]
                    reused_streams = True
                except Exception as exc:  # pragma: no cover - defend
                    logger.warning(
                        "reuse_streams_failed path=%s error=%s", path.name, exc
                    )
                    reused_streams = False
        if not reused_streams:
            try:
                streams = self.tagger.extractor.list_streams(path)
            except MkvToolError as exc:
                logger.error("fail_stream_enum path=%s error=%s", path.name, exc)
                self._log_file_finish(
                    path,
                    position,
                    total_paths,
                    streams=0,
                    translated=0,
                    cache_state=cache_state,
                    reason="stream_enum_failed",
                )
                return 0
            self._ensure_tagged_streams(path, streams)
        else:
            logger.debug(
                "reuse_streams path=%s cached=%s entries=%d",
                path.name,
                cached,
                len(streams),
            )
        language_candidates = self._map_streams_to_languages(path, streams)
        existing_langs = set(language_candidates.keys())
        self._cleanup_embedded_sidecars(path, existing_langs)
        translated, translation_errors, no_source_targets = self._translate_missing(
            path, language_candidates, mtime_ns, existing_langs
        )
        try:
            updated_mtime = path.stat().st_mtime_ns
        except FileNotFoundError:
            if self.cache:
                self.cache.delete(path)
            self._log_file_finish(
                path,
                position,
                total_paths,
                streams=len(streams),
                translated=translated,
                cache_state="missing",
                reason="missing_post_process",
            )
            return translated
        if self.cache:
            if translation_errors:
                cache_state = "cache_skipped"
            else:
                stream_cache_data = [stream.to_cache_dict() for stream in streams]
                cache_languages: Iterable[str] | None = existing_langs or None
                if no_source_targets:
                    cache_languages = set(self.ensure_langs)
                self.cache.update(
                    path,
                    updated_mtime,
                    languages=cache_languages,
                    streams=stream_cache_data,
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
            position,
            total_paths,
            streams=len(streams),
            translated=translated,
            cache_state=cache_state,
            reason=finish_reason,
        )
        return translated

    def _log_file_start(self, path: Path, position: int, total_paths: int) -> None:
        logger.info(
            "file_start path=%s index=%d total=%d",
            path.name,
            position,
            total_paths,
        )

    def _log_file_finish(
        self,
        path: Path,
        position: int,
        total_paths: int,
        *,
        streams: int | None = None,
        translated: int | None = None,
        cache_state: str | None = None,
        reason: str | None = None,
    ) -> None:
        extra_reason = f" reason={reason}" if reason else ""
        logger.info(
            "file_done path=%s index=%d total=%d streams=%s translated=%s cache=%s%s",
            path.name,
            position,
            total_paths,
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
        for target_lang in self.ensure_langs:
            if not self._needs_translation(path, target_lang, mtime_ns, existing_langs):
                continue
            had_pending_translation = True
            source_lang, stream = self._pick_source_stream(candidates, target_lang)
            if not source_lang or not stream:
                logger.warning(
                    "skip_translation path=%s target=%s reason=no_source",
                    path.name,
                    target_lang,
                )
                continue
            had_source_language = True
            try:
                self._translate_stream(path, stream, source_lang, target_lang)
            except FileNotFoundError:
                logger.info("mkv_missing_during_translation path=%s target=%s", path.name, target_lang)
                if self.cache:
                    self.cache.delete(path)
                return translated, True
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
            translated += 1
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

    def _map_streams_to_languages(
        self, path: Path, streams: list[SubtitleStream]
    ) -> dict[str, tuple[SubtitleStream, SubtitleMetrics]]:
        candidates: dict[str, tuple[SubtitleStream, SubtitleMetrics]] = {}
        for stream in streams:
            codec = (stream.codec or "").lower()
            if codec in BITMAP_SUBTITLE_CODECS:
                continue
            lang_iso2 = self._determine_language(path, stream)
            lang = normalize_language_code_iso1(lang_iso2)
            if not lang:
                continue
            metrics = SubtitleMetrics.from_stream(stream)
            previous = candidates.get(lang)
            if previous and previous[1].score() >= metrics.score():
                continue
            candidates[lang] = (stream, metrics)
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
        self, candidates: dict[str, tuple[SubtitleStream, SubtitleMetrics]], target: str
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
                    "using_fallback_source target=%s source=%s",
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
    ) -> None:
        extractor = self.tagger.extractor
        subtitle_blob = self._subtitle_path(path, target_lang)
        if not extractor:
            raise RuntimeError("subtitle extractor is not available")
        if not path.exists():
            raise FileNotFoundError(path)
        temp_path = extractor.create_temp_path(".srt")
        try:
            extractor.extract_stream(path, stream, temp_path)
            translated = self.translator.translate(temp_path, target_lang, src_lang=source_lang)
            translated = self._sanitize_translated_subtitle(translated)
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
        finally:
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
