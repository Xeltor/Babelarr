import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

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
        self.tasks: queue.Queue[TranslationTask] = queue.Queue()
        self.db = QueueRepository(self.config.queue_db)
        self.shutdown_event = threading.Event()
        self.translator_available = threading.Event()

    def output_path(self, src: Path, lang: str) -> Path:
        stem = src.name.removesuffix(self.config.src_ext)
        return src.with_name(f"{stem}.{lang}.srt")

    def translate_file(self, src: Path, lang: str) -> bool:
        """Translate *src* into *lang*.

        Returns ``True`` if the translated output was written to disk, ``False``
        if the source file disappeared mid-translation and no output was
        produced.
        """

        logger.debug("Translating %s to %s", src, lang)
        content = self.translator.translate(src, lang)
        if not src.exists():
            logger.warning("Source %s disappeared during translation; skipping", src)
            return False
        output = self.output_path(src, lang)
        output.write_bytes(content)
        logger.info("[%s] saved -> %s", lang, output)
        return True

    def worker(self):
        name = threading.current_thread().name
        logger.debug("Worker %s starting", name)
        wait = getattr(self.translator, "wait_until_available", None)
        if callable(wait):
            wait()
        self.translator_available.set()
        while not self.shutdown_event.is_set():
            if not self.translator_available.wait(timeout=1):
                continue
            try:
                task = self.tasks.get(timeout=1)
            except queue.Empty:
                continue
            path, lang = task.path, task.lang
            start_time = time.monotonic()
            logger.debug("Worker %s picked up %s [%s]", name, path, lang)
            requeue = False
            success = False
            try:
                if path.exists():
                    logger.info("Worker %s translating %s to %s", name, path, lang)
                    success = self.translate_file(path, lang)
                else:
                    logger.warning("Worker %s missing %s, skipping", name, path)
            except requests.RequestException as exc:
                logger.error(
                    "Worker %s translation failed for %s to %s: %s",
                    name,
                    path,
                    lang,
                    exc,
                )
                logger.debug("Traceback:", exc_info=True)
                requeue = True
                self.translator_available.clear()
                if callable(wait):
                    wait()
                self.translator_available.set()
            except Exception as exc:
                logger.error(
                    "Worker %s translation failed for %s to %s: %s",
                    name,
                    path,
                    lang,
                    exc,
                )
                logger.debug("Traceback:", exc_info=True)
            finally:
                if requeue:
                    self.tasks.put(task)
                    logger.info(
                        "Worker %s requeued %s to %s for later processing (queue length: %d)",
                        name,
                        path,
                        lang,
                        self.db.count(),
                    )
                else:
                    self.db.remove(path, lang)
                    status = "Completed" if success else "Skipped"
                    logger.info(
                        "Worker %s %s %s to %s (queue length: %d)",
                        name,
                        status,
                        path,
                        lang,
                        self.db.count(),
                    )
                self.tasks.task_done()
                elapsed = time.monotonic() - start_time
                logger.debug(
                    "Worker %s finished processing %s [%s] in %.2fs",
                    name,
                    path,
                    lang,
                    elapsed,
                )

    def needs_translation(self, path: Path, lang: str) -> bool:
        out = self.output_path(path, lang)
        return not out.exists()

    def enqueue(self, path: Path):
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
            if self.db.add(path, lang):
                queued_any = True
                self.tasks.put(TranslationTask(path, lang))
                logger.info(
                    "queued %s to %s (queue length: %d)",
                    path,
                    lang,
                    self.db.count(),
                )
            else:
                logger.debug("%s to %s already queued", path, lang)
        if not queued_any:
            logger.debug("All translations present for %s; skipping", path)

    def full_scan(self):
        logger.info("Performing full scan")
        for root in self.config.root_dirs:
            logger.debug("Scanning %s", root)
            for file in Path(root).rglob(f"*{self.config.src_ext}"):
                self.enqueue(file)

    def load_pending(self):
        logger.info("Loading pending tasks")
        for path, lang in self.db.all():
            task = TranslationTask(path, lang)
            self.tasks.put(task)
            logger.info("restored %s to %s", path, lang)

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
        logger.info("Starting %d worker threads", self.config.workers)
        executor = ThreadPoolExecutor(
            max_workers=self.config.workers, thread_name_prefix="worker"
        )
        for _ in range(self.config.workers):
            executor.submit(self.worker)

        self.load_pending()
        self.full_scan()
        schedule.every(self.config.scan_interval_minutes).minutes.do(self.full_scan)

        watcher = threading.Thread(target=self.watch)
        watcher.start()
        logger.info("Service started")

        while not self.shutdown_event.is_set():
            schedule.run_pending()
            time.sleep(1)

        logger.info("Shutdown initiated")
        watcher.join()
        executor.shutdown(wait=True)
        self.db.close()
        close = getattr(self.translator, "close", None)
        if callable(close):
            close()
        logger.info("Shutdown complete")
