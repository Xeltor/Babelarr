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

    def on_created(self, event):
        if not event.is_directory:
            logger.debug("Detected new file %s", event.src_path)
            self.app.enqueue(Path(event.src_path))


class Application:
    def __init__(self, config: Config, translator: Translator):
        self.config = config
        self.translator = translator
        self.tasks: queue.Queue[Path] = queue.Queue()
        self.db = QueueRepository(self.config.queue_db)
        self.shutdown_event = threading.Event()

    def translate_file(self, src: Path, lang: str) -> None:
        logger.debug("Translating %s to %s", src, lang)
        content = self.translator.translate(src, lang)
        output = src.with_suffix(f".{lang}.srt")
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
                        out = path.with_suffix(f".{lang}.srt")
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
            out = path.with_suffix(f".{lang}.srt")
            if not out.exists():
                return True
        return False

    def enqueue(self, path: Path):
        logger.debug("Attempting to enqueue %s", path)
        if not str(path).endswith(self.config.src_ext) or not path.is_file():
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
        logger.info("Shutdown complete")
