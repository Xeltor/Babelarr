import logging
import queue
import threading
import time
from pathlib import Path

import requests
import schedule
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from config import Config
from queue_db import QueueRepository

logger = logging.getLogger("babelarr")

ERROR_MESSAGES = {
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    429: "Too Many Requests",
    500: "Server Error",
}


class SrtHandler(FileSystemEventHandler):
    def __init__(self, app: "Application"):
        self.app = app

    def on_created(self, event):
        if not event.is_directory:
            logger.debug("Detected new file %s", event.src_path)
            self.app.enqueue(Path(event.src_path))


class Application:
    def __init__(self, config: Config):
        self.config = config
        self.tasks: queue.Queue[Path] = queue.Queue()
        self.db = QueueRepository(self.config.queue_db)
        self.shutdown_event = threading.Event()

    def translate_file(self, src: Path, lang: str) -> None:
        logger.debug("Translating %s to %s", src, lang)
        with open(src, "rb") as fh:
            files = {"file": fh}
            data = {"source": "en", "target": lang, "format": "srt"}
            resp = requests.post(self.config.api_url, files=files, data=data, timeout=60)
            if resp.status_code != 200:
                message = ERROR_MESSAGES.get(resp.status_code, "Unexpected error")
                try:
                    err_json = resp.json()
                    detail = err_json.get("error") or err_json.get("message") or err_json.get("detail")
                    if detail:
                        message = f"{message}: {detail}"
                except ValueError:
                    pass
                logger.error("HTTP %s from LibreTranslate: %s", resp.status_code, message)
                logger.error("Headers: %s", resp.headers)
                logger.error("Body: %s", resp.text)
                if logger.isEnabledFor(logging.DEBUG):
                    import tempfile

                    tmp = tempfile.NamedTemporaryFile(delete=False, prefix="babelarr-", suffix=".err")
                    try:
                        tmp.write(resp.content)
                        logger.debug("Saved failing response to %s", tmp.name)
                    finally:
                        tmp.close()
                resp.raise_for_status()
        output = src.with_suffix(f".{lang}.srt")
        output.write_bytes(resp.content)
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
        workers = []
        for _ in range(self.config.workers):
            t = threading.Thread(target=self.worker)
            t.start()
            workers.append(t)

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
        for t in workers:
            t.join()
        self.db.close()
        logger.info("Shutdown complete")
