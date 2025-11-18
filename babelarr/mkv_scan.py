from __future__ import annotations

import logging
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

from .mkv import (
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
                "CREATE TABLE IF NOT EXISTS cache (path TEXT PRIMARY KEY, mtime_ns INTEGER)"
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
                "CREATE TABLE IF NOT EXISTS cache (path TEXT PRIMARY KEY, mtime_ns INTEGER)"
            )

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

    def update(self, path: Path, mtime_ns: int) -> None:
        self._execute(
            "INSERT INTO cache (path, mtime_ns) VALUES (?, ?) "
            "ON CONFLICT(path) DO UPDATE SET mtime_ns=excluded.mtime_ns",
            (str(path), int(mtime_ns)),
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
        if self.worker_count <= 1:
            total = 0
            for path in paths:
                total += self._process_file(path)
            return total

        total = 0
        with ThreadPoolExecutor(max_workers=self.worker_count) as executor:
            future_to_path = {
                executor.submit(self._process_file, path): path for path in paths
            }
            for future in as_completed(future_to_path):
                path = future_to_path[future]
                try:
                    total += future.result()
                except Exception as exc:
                    logger.error("fail_process_file path=%s error=%s", path, exc)
        return total

    def _process_file(self, path: Path) -> int:
        try:
            mtime_ns = path.stat().st_mtime_ns
        except FileNotFoundError:
            if self.cache:
                self.cache.delete(path)
            return 0
        cache_state = "disabled"
        if self.cache:
            cached = self.cache.get_mtime(path)
            cache_state = "cache_miss"
            if cached == mtime_ns and not self._has_pending_targets(path, mtime_ns):
                logger.debug("skip_cached path=%s", path.name)
                return 0
        streams: list[SubtitleStream]
        try:
            streams = self.tagger.extractor.list_streams(path)
        except MkvToolError as exc:
            logger.error("fail_stream_enum path=%s error=%s", path.name, exc)
            return 0
        self._ensure_tagged_streams(path, streams)
        language_candidates = self._map_streams_to_languages(path, streams)
        translated = self._translate_missing(path, language_candidates, mtime_ns)
        try:
            updated_mtime = path.stat().st_mtime_ns
        except FileNotFoundError:
            if self.cache:
                self.cache.delete(path)
            return translated
        if self.cache:
            self.cache.update(path, updated_mtime)
            cache_state = "cache_updated"
        logger.info(
            "file_state path=%s streams=%d translated=%d cache=%s",
            path.name,
            len(streams),
            translated,
            cache_state,
        )
        return translated

    def _translate_missing(
        self,
        path: Path,
        candidates: dict[str, tuple[SubtitleStream, SubtitleMetrics]],
        mtime_ns: int,
    ) -> int:
        translated = 0
        for target_lang in self.ensure_langs:
            if not self._needs_translation(path, target_lang, mtime_ns):
                continue
            source_lang, stream = self._pick_source_stream(candidates, target_lang)
            if not source_lang or not stream:
                logger.warning(
                    "skip_translation path=%s target=%s reason=no_source",
                    path.name,
                    target_lang,
                )
                continue
            try:
                self._translate_stream(path, stream, source_lang, target_lang)
            except FileNotFoundError:
                logger.info("mkv_missing_during_translation path=%s target=%s", path.name, target_lang)
                if self.cache:
                    self.cache.delete(path)
                return translated
            except Exception as exc:
                logger.error(
                    "fail_translation path=%s source=%s target=%s error=%s",
                    path.name,
                    source_lang,
                    target_lang,
                    exc,
                )
                continue
            translated += 1
        return translated

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

    def _needs_translation(self, path: Path, lang: str, mtime_ns: int) -> bool:
        dest = self._subtitle_path(path, lang)
        if not dest.exists():
            return True
        try:
            return dest.stat().st_mtime_ns < mtime_ns
        except FileNotFoundError:
            return True

    def _has_pending_targets(self, path: Path, mtime_ns: int) -> bool:
        for lang in self.ensure_langs:
            if self._needs_translation(path, lang, mtime_ns):
                return True
        return False

    def _map_streams_to_languages(
        self, path: Path, streams: list[SubtitleStream]
    ) -> dict[str, tuple[SubtitleStream, SubtitleMetrics]]:
        candidates: dict[str, tuple[SubtitleStream, SubtitleMetrics]] = {}
        for stream in streams:
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
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".srt")
        temp_path = Path(temp_file.name)
        temp_file.close()
        try:
            extractor.extract_stream(path, stream, temp_path)
            translated = self.translator.translate(temp_path, target_lang, src_lang=source_lang)
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

    def _subtitle_path(self, path: Path, lang: str) -> Path:
        return path.with_suffix(f".{lang}.srt")
