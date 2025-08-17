import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import requests
import schedule
from watchdog.events import PatternMatchingEventHandler
from watchdog.observers import Observer

from .config import Config
from .queue_db import QueueRepository
from .translator import Translator

logger = logging.getLogger("babelarr")


@dataclass(frozen=True)
class TranslationTask:
    path: Path
    lang: str
    task_id: str
    priority: int = 0


class SrtHandler(PatternMatchingEventHandler):
    def __init__(self, app: "Application"):
        self.app = app
        self._debounce = self.app.config.debounce
        self._max_wait = 30
        self._recent: dict[Path, float] = {}
        super().__init__(
            patterns=[f"*{self.app.config.src_ext}"],
            ignore_directories=True,
        )

    def _wait_for_complete(self, path: Path) -> bool:
        """Wait until *path* appears stable before enqueueing.

        Returns ``False`` if the file disappears while waiting or the timeout
        is exceeded.
        """
        start = time.monotonic()
        while True:
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                return False
            time.sleep(self._debounce)
            try:
                new_size = path.stat().st_size
            except FileNotFoundError:
                return False
            if new_size == size:
                return True
            if time.monotonic() - start > self._max_wait:
                logger.warning("Timeout waiting for %s to stabilize", path)
                return False

    def _handle(self, path: Path) -> None:
        now = time.monotonic()
        # prune expired entries
        for p, ts in list(self._recent.items()):
            if now - ts > self._debounce:
                del self._recent[p]

        last = self._recent.get(path)
        if last and now - last < self._debounce:
            logger.debug("Skipping %s; handled %.2fs ago", path, now - last)
            return

        if self._wait_for_complete(path):
            self._recent[path] = now
            self.app.enqueue(path)

    def on_created(self, event):
        logger.debug("Detected new file %s", event.src_path)
        self._handle(Path(event.src_path))

    def on_deleted(self, event):
        logger.debug("Detected deleted file %s", event.src_path)
        self.app.db.remove(Path(event.src_path))

    def on_modified(self, event):
        """Ignore file modification events."""
        return

    def on_moved(self, event):
        dest = Path(event.dest_path)
        logger.debug("Detected moved file %s -> %s", event.src_path, dest)
        self._handle(dest)


class Application:
    def __init__(self, config: Config, translator: Translator):
        self.config = config
        self.translator = translator
        self.tasks: queue.PriorityQueue[tuple[int, int, TranslationTask]] = (
            queue.PriorityQueue()
        )
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
        stem = src.name.removesuffix(self.config.src_ext)
        return src.with_name(f"{stem}.{lang}.srt")

    def translate_file(self, src: Path, lang: str, task_id: str | None = None) -> bool:
        """Translate *src* into *lang*.

        Returns ``True`` if the translated output was written to disk, ``False``
        if the source file disappeared mid-translation and no output was
        produced.
        """

        prefix = f"[{task_id}] " if task_id else ""
        logger.debug("%sTranslating %s to %s", prefix, src, lang)
        content = self.translator.translate(src, lang)
        if not src.exists():
            logger.warning(
                "%sSource %s disappeared during translation; skipping",
                prefix,
                src,
            )
            return False
        output = self.output_path(src, lang)
        output.write_bytes(content)
        logger.debug("%s[%s] saved -> %s", prefix, lang, output)
        return True

    def _get_task(self) -> TranslationTask | None:
        try:
            _, _, task = self.tasks.get(timeout=0.1)
            return task
        except queue.Empty:
            return None

    def _process_translation(self, task: TranslationTask, worker_name: str) -> bool:
        path, lang, task_id = task.path, task.lang, task.task_id
        if path.exists():
            logger.debug(
                "Worker %s translating %s to %s id=%s", worker_name, path, lang, task_id
            )
            return self.translate_file(path, lang, task_id)
        logger.warning(
            "Worker %s missing %s, skipping id=%s", worker_name, path, task_id
        )
        return False

    def _handle_failure(
        self,
        task: TranslationTask,
        exc: Exception,
        worker_name: str,
        wait: Callable[[], None] | None,
    ) -> bool:
        path, lang, task_id = task.path, task.lang, task.task_id
        logger.error(
            "Worker %s translation failed for %s to %s id=%s: %s",
            worker_name,
            path,
            lang,
            task_id,
            exc,
        )
        logger.debug("Traceback:", exc_info=True)
        if isinstance(exc, requests.RequestException):
            self.translator_available.clear()
            if callable(wait):
                wait()
            self.translator_available.set()
            return True
        return False

    def worker(self):
        name = threading.current_thread().name
        logger.debug("Worker %s starting", name)
        wait = getattr(self.translator, "wait_until_available", None)
        if callable(wait):
            wait()
        self.translator_available.set()
        try:
            while not self.shutdown_event.is_set():
                if not self.translator_available.wait(timeout=1):
                    continue
                task = self._get_task()
                if task is None:
                    break
                path, lang, task_id = task.path, task.lang, task.task_id
                start_time = time.monotonic()
                logger.debug(
                    "Worker %s picked up %s [%s] id=%s", name, path, lang, task_id
                )
                try:
                    success = self._process_translation(task, name)
                    requeue = False
                    outcome = "succeeded" if success else "skipped"
                except Exception as exc:
                    requeue = self._handle_failure(task, exc, name, wait)
                    success = False
                    outcome = "failed"
                elapsed = time.monotonic() - start_time
                if requeue:
                    self.tasks.put((task.priority, self._task_counter, task))
                    self._task_counter += 1
                    logger.info(
                        "Worker %s requeued %s to %s id=%s for later processing (queue length: %d)",
                        name,
                        path,
                        lang,
                        task_id,
                        self.db.count(),
                    )
                else:
                    self.db.remove(path, lang)
                    logger.info(
                        "translation %s to %s %s in %.2fs (queue length: %d)",
                        path,
                        lang,
                        outcome,
                        elapsed,
                        self.db.count(),
                    )
                self.tasks.task_done()
                logger.debug(
                    "Worker %s finished processing %s [%s] id=%s in %.2fs",
                    name,
                    path,
                    lang,
                    task_id,
                    elapsed,
                )
        finally:
            with self._worker_lock:
                self._active_workers -= 1
                self._worker_threads.discard(threading.current_thread())

    def needs_translation(self, path: Path, lang: str) -> bool:
        out = self.output_path(path, lang)
        return not out.exists()

    def enqueue(self, path: Path, *, priority: int = 0):
        logger.debug("Attempting to enqueue %s", path)
        if not path.is_file() or not path.name.lower().endswith(
            self.config.src_ext.lower()
        ):
            return
        queued_any = False
        for lang in self.config.target_langs:
            if not self.needs_translation(path, lang):
                logger.debug(
                    "Translation already exists: %s", self.output_path(path, lang)
                )
                continue
            if self.db.add(path, lang, priority):
                queued_any = True
                task_id = uuid4().hex
                task = TranslationTask(path, lang, task_id, priority)
                self.tasks.put((priority, self._task_counter, task))
                self._task_counter += 1
                self._ensure_workers()
                logger.info(
                    "queued %s to %s id=%s (queue length: %d)",
                    path,
                    lang,
                    task_id,
                    self.db.count(),
                )
            else:
                logger.debug("%s to %s already queued", path, lang)
        if not queued_any:
            logger.debug("All translations present for %s; skipping", path)

    def request_scan(self) -> None:
        """Enqueue a full directory scan to be handled by the scanner thread."""
        self.scan_queue.put(None)

    def scan_worker(self) -> None:
        """Background worker that performs full filesystem scans."""
        name = threading.current_thread().name
        logger.debug("Scan worker %s starting", name)
        while not self.shutdown_event.is_set():
            try:
                self.scan_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.full_scan()
            finally:
                self.scan_queue.task_done()
        logger.debug("Scan worker %s exiting", name)

    def full_scan(self):
        logger.info("Performing full scan")
        for root in self.config.root_dirs:
            logger.debug("Scanning %s", root)
            for file in Path(root).rglob(f"*{self.config.src_ext}"):
                self.enqueue(file, priority=1)

    def load_pending(self):
        logger.info("Loading pending tasks")
        for path, lang, priority in self.db.all():
            task = TranslationTask(path, lang, uuid4().hex, priority)
            self.tasks.put((priority, self._task_counter, task))
            self._task_counter += 1
            logger.info("restored %s to %s id=%s", path, lang, task.task_id)
            self._ensure_workers()

    def watch(self):
        observer = Observer()
        for root in self.config.root_dirs:
            logger.debug("Watching %s", root)
            root_path = Path(root)
            if not root_path.exists():
                logger.warning("Directory %s does not exist; skipping", root_path)
                continue
            observer.schedule(SrtHandler(self), root, recursive=True)
        observer.start()
        logger.info("Observer started")
        try:
            while not self.shutdown_event.is_set():
                time.sleep(1)
        finally:
            observer.stop()
            observer.join()
            logger.info("Observer stopped")

    def run(self):
        self.load_pending()
        self.request_scan()
        schedule.every(self.config.scan_interval_minutes).minutes.do(self.request_scan)

        self._scan_thread = threading.Thread(target=self.scan_worker, name="scanner")
        self._scan_thread.start()

        watcher = threading.Thread(target=self.watch, name="watcher")
        watcher.start()
        logger.info("Service started")

        while not self.shutdown_event.is_set():
            schedule.run_pending()
            time.sleep(1)

        logger.info("Shutdown initiated")
        watcher.join()
        if self._scan_thread:
            self._scan_thread.join()
        for t in list(self._worker_threads):
            t.join()
        self.db.close()
        close = getattr(self.translator, "close", None)
        if callable(close):
            close()
        logger.info("Shutdown complete")

    def _ensure_workers(self) -> None:
        with self._worker_lock:
            needed = min(self.tasks.qsize(), self.config.workers) - self._active_workers
            for _ in range(needed):
                name = f"worker_{self._worker_counter}"
                self._worker_counter += 1
                thread = threading.Thread(target=self.worker, name=name)
                self._worker_threads.add(thread)
                self._active_workers += 1
                thread.start()
