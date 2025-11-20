from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import schedule

from . import watch as watch_module
from .config import Config
from .jellyfin_api import JellyfinClient
from .mkv import MkvSubtitleTagger
from .mkv_probe_cache import MkvProbeCache
from .mkv_scan import MkvScanner
from .mkv_work_index import MkvWorkIndex
from .mkv_workflow import MkvWorkflow
from .profiling import WorkloadProfiler
from .profiling_ui import ProfilingDashboard
from .translator import Translator

logger = logging.getLogger(__name__)


class Application:
    def __init__(
        self,
        config: Config,
        translator: Translator,
        jellyfin: JellyfinClient | None = None,
        mkv_tagger: MkvSubtitleTagger | None = None,
        profiler: WorkloadProfiler | None = None,
        profiling_dashboard: ProfilingDashboard | None = None,
    ):
        self.config = config
        self.translator = translator
        self.jellyfin = jellyfin
        self.mkv_tagger = mkv_tagger
        self.profiler = profiler

        self.shutdown_event = threading.Event()
        self._probe_cache: MkvProbeCache | None = None
        self._mkv_scanner: MkvScanner | None = None
        self._work_index: MkvWorkIndex | None = None
        self.workflow: MkvWorkflow | None = None
        self.profiling_dashboard = profiling_dashboard

    def request_mkv_scan(self) -> None:
        if self.workflow:
            self.workflow.request_scan()

    def handle_new_mkv(self, path: Path) -> None:
        if self.workflow:
            self.workflow.handle_new_mkv(path)

    def invalidate_mkv_cache_state(self, path: Path) -> None:
        if self._probe_cache:
            self._probe_cache.invalidate_path(path)
        if self._work_index:
            self._work_index.delete(path)

    def run(self) -> None:
        if self.mkv_tagger and self.config.mkv_dirs:
            self._probe_cache = MkvProbeCache(
                self.mkv_tagger.extractor,
                db_path=self.config.mkv_cache_path,
                profiler=self.profiler,
            )
            self._work_index = MkvWorkIndex(self.config.mkv_cache_path)
            preferred_source = "en" if "en" in self.config.ensure_langs else (
                self.config.ensure_langs[0] if self.config.ensure_langs else None
            )
            self._mkv_scanner = MkvScanner(
                directories=self.config.mkv_dirs,
                tagger=self.mkv_tagger,
                translator=self.translator,
                ensure_langs=self.config.ensure_langs,
                probe_cache=self._probe_cache,
                cache_enabled=self.config.mkv_cache_enabled,
                preferred_source=preferred_source,
                jellyfin_client=self.jellyfin,
                profiler=self.profiler,
                work_index=self._work_index,
            )
            self.workflow = MkvWorkflow(
                scanner=self._mkv_scanner,
                worker_count=self.config.workers,
                shutdown_event=self.shutdown_event,
                profiler=self.profiler,
                work_index=self._work_index,
            )
            self.workflow.start()
            if self.profiling_dashboard:
                self.profiling_dashboard.register_status_provider(
                    "mkv_queue", self.workflow.queue_status
                )
            self.request_mkv_scan()
            schedule.every(self.config.mkv_scan_interval_minutes).minutes.do(
                self.request_mkv_scan
            )

        watcher_thread: threading.Thread | None = None
        if self.config.mkv_dirs:
            watcher_thread = threading.Thread(
                target=watch_module.watch, args=(self,), name="watcher"
            )
            watcher_thread.start()
        logger.info("service_started")
        if self.profiling_dashboard:
            self.profiling_dashboard.start()

        try:
            while not self.shutdown_event.is_set():
                schedule.run_pending()
                time.sleep(1)
        finally:
            self.shutdown_event.set()
            if watcher_thread:
                watcher_thread.join()
            if self.workflow:
                self.workflow.stop()
            close = getattr(self.translator, "close", None)
            if callable(close):
                close()
            if self.profiler and self.profiler.enabled:
                lines = self.profiler.report_lines()
                if lines:
                    logger.info("profiling_summary %s", " | ".join(lines))
            if self.profiling_dashboard:
                self.profiling_dashboard.stop()
            logger.info("shutdown_complete")
