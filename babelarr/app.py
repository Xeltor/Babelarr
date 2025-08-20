from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from uuid import uuid4

import schedule

from . import watch as watch_module
from .config import Config
from .jellyfin_api import JellyfinClient
from .queue_db import QueueRepository
from .translator import Translator
from .worker import TranslationLogger, TranslationTask
from .worker import worker as worker_loop

logger = logging.getLogger(__name__)


class Application:
    def __init__(
        self,
        config: Config,
        translator: Translator,
        jellyfin: JellyfinClient | None = None,
    ):
        """Create an application coordinator.

        Initializes queues and thread primitives; call once at startup.
        """
        self.config = config
        self.translator = translator
        self.jellyfin = jellyfin
        self.tasks: queue.PriorityQueue[tuple[int, int, TranslationTask]] = (
            queue.PriorityQueue()
        )
        self.pending_translations: dict[Path, set[str]] = {}
        self._pending_lock = threading.Lock()
        self.db = QueueRepository(self.config.queue_db)
        self.shutdown_event = threading.Event()
        self.translator_available = threading.Event()
        self._worker_lock = threading.Lock()
        self._worker_threads: set[threading.Thread] = set()
        self._active_workers = 0
        self._worker_counter = 0
        self._task_counter = 0
        self.scan_queue: queue.Queue[None] = queue.Queue()
        self._scan_thread: threading.Thread | None = None

    def output_path(self, src: Path, lang: str) -> Path:
        """Return translation output path for *src* and *lang*.

        Pure helper safe for concurrent use.
        """
        stem = src.name.removesuffix(self.config.src_ext)
        return src.with_name(f"{stem}.{lang}.srt")

    def translate_file(self, src: Path, lang: str, task_id: str | None = None) -> bool:
        """Translate *src* into *lang*.

        Runs in worker threads and blocks on network and disk I/O. Returns
        ``True`` if the translated output was written, ``False`` if the source
        disappears during translation.
        """

        tlog = TranslationLogger(src, lang, task_id)
        tlog.debug("translating")
        content = self.translator.translate(src, lang)
        if not src.exists():
            tlog.warning("skip reason=source_missing")
            return False
        output = self.output_path(src, lang)
        output.write_bytes(content)
        tlog.debug("save output=%s", output.name)
        return True

    def needs_translation(self, path: Path, lang: str) -> bool:
        """Return ``True`` if *path* lacks a translation for *lang*.

        Performs a filesystem check and is thread-safe.
        """
        out = self.output_path(path, lang)
        return not out.exists()

    def translation_done(self, path: Path, lang: str) -> None:
        """Update pending translations and refresh Jellyfin when complete."""
        folder = path.parent
        with self._pending_lock:
            pending = self.pending_translations.get(path)
            if pending is None:
                return
            pending.discard(lang)
            if pending:
                return
            del self.pending_translations[path]
            if any(p.parent == folder for p in self.pending_translations):
                return
        if self.jellyfin:
            try:
                self.jellyfin.refresh_path(folder)
                TranslationLogger(folder).info(
                    "jellyfin_refresh show=%s", folder.parent.name
                )
            except Exception as exc:  # noqa: BLE001
                TranslationLogger(folder).error("jellyfin_refresh_failed error=%s", exc)

    def enqueue(self, path: Path, *, priority: int = 0) -> None:
        """Queue *path* for translation with the given *priority*.

        Thread-safe; may block briefly when interacting with the task queue.
        """
        TranslationLogger(path).debug("enqueue_attempt")
        if not path.is_file() or not path.name.lower().endswith(
            self.config.src_ext.lower()
        ):
            return
        queued_any = False
        for lang in self.config.target_langs:
            tlog = TranslationLogger(path, lang)
            if not self.needs_translation(path, lang):
                tlog.debug(
                    "translation_exists output=%s",
                    self.output_path(path, lang).name,
                )
                continue
            if self.db.add(path, lang, priority):
                with self._pending_lock:
                    self.pending_translations.setdefault(path, set()).add(lang)
                queued_any = True
                task_id = uuid4().hex
                task = TranslationTask(path, lang, task_id, priority)
                self.tasks.put((priority, self._task_counter, task))
                self._task_counter += 1
                self._ensure_workers()
                TranslationLogger(path, lang, task_id).info(
                    "queued queue=%d",
                    self.db.count(),
                )
            else:
                tlog.debug("already_queued")
        if not queued_any:
            TranslationLogger(path).debug("skip reason=all_present")

    def request_scan(self) -> None:
        """Signal the scanner thread to perform a full directory scan.

        Non-blocking and safe to call from any thread.
        """
        self.scan_queue.put(None)

    def scan_worker(self) -> None:
        """Background worker that performs full filesystem scans.

        Runs in a dedicated thread and exits when ``shutdown_event`` is set.
        """
        name = threading.current_thread().name
        logger.debug("scan_worker_start name=%s", name)
        while not self.shutdown_event.is_set():
            try:
                self.scan_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.full_scan()
            finally:
                self.scan_queue.task_done()
        logger.debug("scan_worker_exit name=%s", name)

    def full_scan(self) -> None:
        """Recursively scan roots and enqueue missing translations.

        Blocking operation intended for the scanner thread.
        """
        logger.info("scan_start")
        total = 0
        for root in self.config.root_dirs:
            logger.debug("scan path=%s", Path(root).name)
            for file in Path(root).rglob(f"*{self.config.src_ext}"):
                total += 1
                self.enqueue(file, priority=1)
        logger.info("scan_complete files=%d", total)

    def load_pending(self) -> None:
        """Restore queued tasks from the persistent repository.

        Should run before worker threads start; blocks on database access.
        """
        logger.debug("load_pending")
        restored = 0
        for path, lang, priority in self.db.all():
            task = TranslationTask(path, lang, uuid4().hex, priority)
            self.tasks.put((priority, self._task_counter, task))
            self._task_counter += 1
            with self._pending_lock:
                self.pending_translations.setdefault(path, set()).add(lang)
            tlog = TranslationLogger(path, lang, task.task_id)
            tlog.debug("restored")
            self._ensure_workers()
            restored += 1
        logger.info("load_pending restored=%d", restored)

    def run(self) -> None:
        """Run the service until shutdown.

        Starts worker, watcher, and scanner threads and blocks until
        ``shutdown_event`` is set.
        """
        self.load_pending()
        self.request_scan()
        schedule.every(self.config.scan_interval_minutes).minutes.do(self.request_scan)

        self._scan_thread = threading.Thread(target=self.scan_worker, name="scanner")
        self._scan_thread.start()

        watcher = threading.Thread(
            target=watch_module.watch, args=(self,), name="watcher"
        )
        watcher.start()
        logger.info("service_started")

        while not self.shutdown_event.is_set():
            schedule.run_pending()
            time.sleep(1)

        logger.info("shutdown_initiated")
        watcher.join()
        if self._scan_thread:
            self._scan_thread.join()
        for t in list(self._worker_threads):
            t.join()
        self.db.close()
        close = getattr(self.translator, "close", None)
        if callable(close):
            close()
        logger.info("shutdown_complete")

    def _ensure_workers(self) -> None:
        """Spawn worker threads up to the configured limit.

        Thread-safe via an internal lock.
        """
        with self._worker_lock:
            needed = min(self.tasks.qsize(), self.config.workers) - self._active_workers
            for _ in range(needed):
                name = f"worker_{self._worker_counter}"
                self._worker_counter += 1
                thread = threading.Thread(target=worker_loop, args=(self,), name=name)
                self._worker_threads.add(thread)
                self._active_workers += 1
                thread.start()
