from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Iterable

from .mkv import MkvSubtitleTagger, MkvToolError, SubtitleStream

logger = logging.getLogger(__name__)


class MkvCache:
    """JSON-backed store for MKV metadata to avoid reprocessing."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return {}
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("mkv_cache_corrupt path=%s", self.path)
            return {}

    def _flush(self) -> None:
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        temp.replace(self.path)

    def get_mtime(self, path: Path) -> int | None:
        with self._lock:
            entry = self._data.get(str(path))
            if entry is None:
                return None
            try:
                return int(entry["mtime_ns"])
            except (KeyError, ValueError, TypeError):
                return None

    def update(self, path: Path, mtime_ns: int) -> None:
        with self._lock:
            self._data[str(path)] = {"mtime_ns": int(mtime_ns)}
            self._flush()

    def delete(self, path: Path) -> None:
        with self._lock:
            removed = self._data.pop(str(path), None)
            if removed is not None:
                self._flush()

    def prune(self, valid_paths: Iterable[str]) -> None:
        valid = set(valid_paths)
        with self._lock:
            keys = list(self._data.keys())
            removed = False
            for key in keys:
                if key not in valid:
                    del self._data[key]
                    removed = True
            if removed:
                self._flush()


class MkvScanner:
    """Walk configured directories and tag MKV subtitle streams."""

    def __init__(
        self,
        directories: list[str],
        tagger: MkvSubtitleTagger,
        cache: MkvCache,
    ) -> None:
        self.directories = directories
        self.tagger = tagger
        self.cache = cache

    def scan(self) -> tuple[int, int]:
        """Scan configured directories once.

        Returns a tuple of (files discovered, streams tagged).
        """

        processed = 0
        tagged = 0
        seen: set[str] = set()
        for root in self.directories:
            root_path = Path(root)
            if not root_path.is_dir():
                logger.warning("mkv_dir_missing path=%s", root_path)
                continue
            for file_path in root_path.rglob("*.mkv"):
                processed += 1
                seen.add(str(file_path))
                tagged += self._process_file(file_path)
        if seen:
            self.cache.prune(seen)
        return processed, tagged

    def scan_files(self, paths: Iterable[Path]) -> tuple[int, int]:
        """Process specific MKV files."""

        processed = 0
        tagged = 0
        for raw in paths:
            path = Path(raw)
            if not path.is_file():
                continue
            if path.suffix.lower() != ".mkv":
                continue
            processed += 1
            tagged += self._process_file(path)
        return processed, tagged

    def _process_file(self, path: Path) -> int:
        try:
            mtime_ns = path.stat().st_mtime_ns
        except FileNotFoundError:
            self.cache.delete(path)
            return 0
        cached = self.cache.get_mtime(path)
        if cached == mtime_ns:
            logger.debug("mkv_skip_cached path=%s", path.name)
            return 0
        try:
            streams = self.tagger.extractor.list_streams(path)
        except MkvToolError as exc:
            logger.error("mkv_stream_enum_failed path=%s error=%s", path.name, exc)
            return 0
        tagged = 0
        for stream in streams:
            tagged += self._tag_stream(path, stream)
        try:
            updated_mtime = path.stat().st_mtime_ns
        except FileNotFoundError:
            self.cache.delete(path)
            return tagged
        self.cache.update(path, updated_mtime)
        return tagged

    def _tag_stream(self, path: Path, stream: SubtitleStream) -> int:
        try:
            detection = self.tagger.detect_and_tag(path, stream)
        except MkvToolError as exc:
            logger.error(
                "mkv_tag_failed path=%s track=%s error=%s",
                path.name,
                stream.track_selector,
                exc,
            )
            return 0
        return 1 if detection else 0
