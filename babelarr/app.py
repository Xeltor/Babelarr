import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import schedule
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .config import Config
from .queue_db import QueueRepository
from .translator import Translator

logger = logging.getLogger("babelarr")


class SrtHandler(FileSystemEventHandler):
    def __init__(self, app: "Application"):
        self.app = app
        self._debounce = self.app.config.debounce
        self._max_wait = 30

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
        if self._wait_for_complete(path):
            self.app.enqueue(path)

    def on_created(self, event):
        if not event.is_directory:
            logger.debug("Detected new file %s", event.src_path)
            self._handle(Path(event.src_path))

    def on_modified(self, event):
        if not event.is_directory:
            path = Path(event.src_path)
            logger.debug("Detected modified file %s", path)
            for lang in self.app.config.target_langs:
                out = self.app.output_path(path, lang)
                if out.exists():
                    out.unlink()
            self._handle(path)

    def on_moved(self, event):
        if not event.is_directory:
            dest = Path(event.dest_path)
            logger.debug("Detected moved file %s -> %s", event.src_path, dest)
            self._handle(dest)


class Application:
    def __init__(self, config: Config, translator: Translator):
        self.config = config
        self.translator = translator
        self.tasks: queue.Queue[Path] = queue.Queue()
        self.db = QueueRepository(self.config.queue_db)
        self.shutdown_event = threading.Event()

    def output_path(self, src: Path, lang: str) -> Path:
        stem = src.name.removesuffix(self.config.src_ext)
        return src.with_name(f"{stem}.{lang}.srt")

    def translate_file(self, src: Path, lang: str) -> None:
        logger.debug("Translating %s to %s", src, lang)
        content = self.translator.translate(src, lang)
        output = self.output_path(src, lang)
        output.write_bytes(content)
        logger.info("[%s] saved -> %s", lang, output)

    def worker(self):
        while not self.shutdown_event.is_set():
            try:
                path = self.tasks.get(timeout=1)
            except queue.Empty:
                continue
            logger.debug("Worker picked up %s", path)
            try:
                if path.exists():
                    for lang in self.config.target_langs:
                        out = self.output_path(path, lang)
                        if not out.exists():
                            logger.info("Translating %s to %s", path, lang)
                            self.translate_file(path, lang)
                        else:
                            logger.debug("Translation already exists: %s", out)
                else:
                    logger.warning("missing %s, skipping", path)
            except Exception as exc:
                logger.error("translation failed for %s: %s", path, exc)
                logger.debug("Traceback:", exc_info=True)
            finally:
                self.db.remove(path)
                self.tasks.task_done()
                logger.debug("Finished processing %s", path)

    def needs_translation(self, path: Path) -> bool:
        for lang in self.config.target_langs:
            out = self.output_path(path, lang)
            if not out.exists():
                return True
        return False

    def enqueue(self, path: Path):
        logger.debug("Attempting to enqueue %s", path)
        if not path.is_file() or not path.name.lower().endswith(
            self.config.src_ext.lower()
        ):
            return
        if not self.needs_translation(path):
            logger.debug("All translations present for %s; skipping", path)
            return
        if self.db.add(path):
            self.tasks.put(path)
            logger.info("queued %s", path)
        else:
            logger.debug("%s already queued", path)

    def full_scan(self):
        logger.info("Performing full scan")
        for root in self.config.root_dirs:
            logger.debug("Scanning %s", root)
            for file in Path(root).rglob(f"*{self.config.src_ext}"):
                self.enqueue(file)

    def load_pending(self):
        logger.info("Loading pending tasks")
        for p in self.db.all():
            self.tasks.put(p)
            logger.info("restored %s", p)

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
        executor = ThreadPoolExecutor(max_workers=self.config.workers)
        for _ in range(self.config.workers):
            executor.submit(self.worker)

        self.load_pending()
        self.full_scan()
        schedule.every().hour.do(self.full_scan)

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
