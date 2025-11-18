from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path

import schedule

from . import watch as watch_module
from .config import Config
from .jellyfin_api import JellyfinClient
from .mkv import MkvSubtitleTagger
from .mkv_scan import MkvCache, MkvScanner
from .translator import Translator

logger = logging.getLogger(__name__)


class Application:
    def __init__(
        self,
        config: Config,
        translator: Translator,
        jellyfin: JellyfinClient | None = None,
        mkv_tagger: MkvSubtitleTagger | None = None,
    ):
        self.config = config
        self.translator = translator
        self.jellyfin = jellyfin
        self.mkv_tagger = mkv_tagger

        self.shutdown_event = threading.Event()
        self.mkv_scan_queue: queue.Queue[Path | None] = queue.Queue()
        self._mkv_thread: threading.Thread | None = None
        self._mkv_cache: MkvCache | None = None
        self._mkv_scanner: MkvScanner | None = None

    def mkv_scan(self) -> None:
        if not self._mkv_scanner:
            return
        logger.info("mkv_scan_start")
        files, translated = self._mkv_scanner.scan()
        logger.info("mkv_scan_complete files=%d translated=%d", files, translated)

    def mkv_scan_file(self, path: Path) -> None:
        if not self._mkv_scanner:
            return
        logger.info("mkv_scan_file path=%s", path)
        files, translated = self._mkv_scanner.scan_files([path])
        logger.info(
            "mkv_scan_file_complete path=%s processed=%d translated=%d",
            path,
            files,
            translated,
        )

    def request_mkv_scan(self, path: Path | None = None) -> None:
        if not self._mkv_scanner:
            return
        self.mkv_scan_queue.put(path)

    def handle_new_mkv(self, path: Path) -> None:
        if not self._mkv_scanner:
            return
        if not path.is_file() or path.suffix.lower() != ".mkv":
            return
        self.request_mkv_scan(path)

    def mkv_scan_worker(self) -> None:
        name = threading.current_thread().name
        logger.debug("mkv_worker_start name=%s", name)
        while not self.shutdown_event.is_set():
            try:
                item = self.mkv_scan_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if item is None:
                    self.mkv_scan()
                else:
                    self.mkv_scan_file(item)
            finally:
                self.mkv_scan_queue.task_done()
        logger.debug("mkv_worker_exit name=%s", name)

    def run(self) -> None:
        if self.mkv_tagger and self.config.mkv_dirs:
            if self.config.mkv_cache_enabled:
                self._mkv_cache = MkvCache(self.config.mkv_cache_path)
            preferred_source = (
                self.config.ensure_langs[0] if self.config.ensure_langs else None
            )
            self._mkv_scanner = MkvScanner(
                directories=self.config.mkv_dirs,
                tagger=self.mkv_tagger,
                translator=self.translator,
                ensure_langs=self.config.ensure_langs,
                cache=self._mkv_cache,
                preferred_source=preferred_source,
                worker_count=self.config.workers,
            )
            self.request_mkv_scan()
            schedule.every(self.config.mkv_scan_interval_minutes).minutes.do(
                self.request_mkv_scan
            )

        if self._mkv_scanner:
            self._mkv_thread = threading.Thread(
                target=self.mkv_scan_worker,
                name="mkv-foreman",
            )
            self._mkv_thread.start()

        watcher_thread: threading.Thread | None = None
        if self.config.mkv_dirs:
            watcher_thread = threading.Thread(
                target=watch_module.watch, args=(self,), name="watcher"
            )
            watcher_thread.start()
        logger.info("service_started")

        try:
            while not self.shutdown_event.is_set():
                schedule.run_pending()
                time.sleep(1)
        finally:
            self.shutdown_event.set()
            if watcher_thread:
                watcher_thread.join()
            if self._mkv_thread:
                self._mkv_thread.join()
            close = getattr(self.translator, "close", None)
            if callable(close):
                close()
            logger.info("shutdown_complete")
