from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable
from pathlib import Path

import schedule

from . import watch as watch_module
from .config import Config
from .ignore import is_path_ignored
from .jellyfin_api import JellyfinClient
from .mkv import MkvSubtitleTagger
from .mkv_probe_cache import MkvProbeCache
from .mkv_scan import MkvScanner
from .mkv_work_index import MkvWorkIndex
from .mkv_workflow import MkvWorkflow
from .profiling import WorkloadProfiler
from .profiling_ui import ProfilingDashboard
from .sidecar_cleanup import SidecarCleaner
from .tdarr_webhook import TdarrWebhookServer
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
    ) -> None:
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
        self.sidecar_cleaner: SidecarCleaner | None = None
        self._sidecar_cleanup_thread: threading.Thread | None = None
        self._sidecar_cleanup_lock = threading.Lock()
        self.webhook_server: TdarrWebhookServer | None = None

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

    def enqueue_webhook_paths(
        self, paths: Iterable[Path], priority: int = 0
    ) -> tuple[list[Path], list[tuple[Path, str]]]:
        queued: list[Path] = []
        skipped: list[tuple[Path, str]] = []
        workflow = self.workflow
        if not workflow:
            for path in paths:
                skipped.append((Path(path), "workflow_unavailable"))
            return queued, skipped
        normalized_paths = []
        for raw_path in paths:
            try:
                normalized_paths.append(Path(raw_path).resolve())
            except OSError as exc:
                skipped.append((Path(raw_path), f"unresolvable:{exc}"))
        allowed_roots = [Path(root).resolve() for root in self.config.mkv_dirs or []]
        if not allowed_roots:
            for path in normalized_paths:
                skipped.append((path, "no_mkv_dirs"))
            return queued, skipped
        for path in normalized_paths:
            try:
                stat = path.stat()
            except FileNotFoundError:
                skipped.append((path, "missing"))
                continue
            except OSError as exc:  # pragma: no cover - defensive
                skipped.append((path, f"stat_error:{exc}"))
                continue
            if path.suffix.lower() != ".mkv":
                skipped.append((path, "not_mkv"))
                continue
            if not any(
                path.is_relative_to(root) or path == root for root in allowed_roots
            ):
                skipped.append((path, "outside_mkv_dirs"))
                continue
            root_match = next(
                (root for root in allowed_roots if path.is_relative_to(root)), None
            )
            if is_path_ignored(path, root=root_match):
                skipped.append((path, "ignored"))
                continue
            self.invalidate_mkv_cache_state(path)
            workflow.enqueue_translation(path, priority=priority)
            queued.append(path)
            logger.info(
                "webhook_enqueue path=%s size=%s priority=%d",
                path.name,
                stat.st_size,
                priority,
            )
        return queued, skipped

    def run(self) -> None:
        if self.mkv_tagger and self.config.mkv_dirs:
            self._probe_cache = MkvProbeCache(
                self.mkv_tagger.extractor,
                db_path=self.config.mkv_cache_path,
                profiler=self.profiler,
            )
            self._work_index = MkvWorkIndex(self.config.mkv_cache_path)
            preferred_source = (
                "en"
                if "en" in self.config.ensure_langs
                else (self.config.ensure_langs[0] if self.config.ensure_langs else None)
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
            self.sidecar_cleaner = SidecarCleaner(self.config.mkv_dirs)
            if self.config.watch_enabled:
                watcher_thread = threading.Thread(
                    target=watch_module.watch, args=(self,), name="watcher"
                )
                watcher_thread.start()
            else:
                logger.info("skip_observer reason=disabled")
            self._schedule_sidecar_cleanup()
        logger.info("service_started")
        if self.config.webhook_port > 0 and self.workflow:
            self.webhook_server = TdarrWebhookServer(
                self,
                self.config.webhook_host,
                self.config.webhook_port,
                token=self.config.webhook_token,
            )
            self.webhook_server.start()
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
            self._wait_for_sidecar_cleanup()
            close = getattr(self.translator, "close", None)
            if callable(close):
                close()
            if self.profiler and self.profiler.enabled:
                lines = self.profiler.report_lines()
                if lines:
                    logger.info("profiling_summary %s", " | ".join(lines))
            if self.profiling_dashboard:
                self.profiling_dashboard.stop()
            if self.webhook_server:
                self.webhook_server.stop()
            logger.info("shutdown_complete")

    def _schedule_sidecar_cleanup(self) -> None:
        self._clean_orphaned_sidecars()
        schedule.every().day.do(self._clean_orphaned_sidecars)

    def _clean_orphaned_sidecars(self) -> None:
        if not self.sidecar_cleaner:
            return
        with self._sidecar_cleanup_lock:
            if self._sidecar_cleanup_thread and self._sidecar_cleanup_thread.is_alive():
                logger.info("sidecar_cleanup_skip reason=in_progress")
                return
            self._sidecar_cleanup_thread = threading.Thread(
                target=self._run_sidecar_cleanup,
                name="sidecar-cleanup",
                daemon=True,
            )
            self._sidecar_cleanup_thread.start()

    def _run_sidecar_cleanup(self) -> None:
        if not self.sidecar_cleaner:
            return
        try:
            self.sidecar_cleaner.remove_orphans()
        except Exception as exc:
            logger.error("sidecar_cleanup_error error=%s", exc)

    def _wait_for_sidecar_cleanup(self) -> None:
        thread = self._sidecar_cleanup_thread
        if thread and thread.is_alive():
            thread.join(timeout=5)
