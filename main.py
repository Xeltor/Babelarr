#!/usr/bin/env python3
import logging
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path

import requests
import schedule
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# Logging setup
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("babelarr")

# Configuration via environment variables
ROOT_DIRS = [p for p in os.environ.get("WATCH_DIRS", "/data").split(":") if p]
TARGET_LANGS = os.environ.get("TARGET_LANGS", "nl,bs").split(",")
SRC_EXT = os.environ.get("SRC_EXT", ".en.srt")
API_URL = os.environ.get("LIBRETRANSLATE_URL", "http://libretranslate:5000/translate")
WORKERS = int(os.environ.get("WORKERS", "1"))
QUEUE_DB = os.environ.get("QUEUE_DB", "queue.db")
logger.debug(
    "Config: ROOT_DIRS=%s TARGET_LANGS=%s SRC_EXT=%s API_URL=%s WORKERS=%s QUEUE_DB=%s",
    ROOT_DIRS,
    TARGET_LANGS,
    SRC_EXT,
    API_URL,
    WORKERS,
    QUEUE_DB,
)

# persistent task queue
tasks = queue.Queue()
db_lock = threading.Lock()
conn = sqlite3.connect(QUEUE_DB, check_same_thread=False)
conn.execute("CREATE TABLE IF NOT EXISTS queue (path TEXT PRIMARY KEY)")
conn.commit()


def translate_file(src: Path, lang: str) -> None:
    """Send the SRT file to LibreTranslate and store the translated version."""
    logger.debug("Translating %s to %s", src, lang)
    with open(src, "rb") as fh:
        files = {"file": fh}
        data = {"source": "en", "target": lang, "format": "srt"}
        resp = requests.post(API_URL, files=files, data=data, timeout=60)
        resp.raise_for_status()
    output = src.with_suffix(f".{lang}.srt")
    output.write_bytes(resp.content)
    logger.info("[%s] saved -> %s", lang, output)


def worker():
    while True:
        path = tasks.get()
        logger.debug("Worker picked up %s", path)
        try:
            if path.exists():
                for lang in TARGET_LANGS:
                    out = path.with_suffix(f".{lang}.srt")
                    if not out.exists():
                        logger.info("Translating %s to %s", path, lang)
                        translate_file(path, lang)
                    else:
                        logger.debug("Translation already exists: %s", out)
            else:
                logger.warning("missing %s, skipping", path)
        except Exception as exc:
            logger.error("translation failed for %s: %s", path, exc)
            logger.debug("Traceback:", exc_info=True)
        finally:
            with db_lock:
                conn.execute("DELETE FROM queue WHERE path = ?", (str(path),))
                conn.commit()
            tasks.task_done()
            logger.debug("Finished processing %s", path)


def enqueue(path: Path):
    logger.debug("Attempting to enqueue %s", path)
    if path.suffix == SRC_EXT and path.is_file():
        with db_lock:
            cur = conn.execute(
                "INSERT OR IGNORE INTO queue(path) VALUES (?)", (str(path),)
            )
            conn.commit()
        if cur.rowcount:
            tasks.put(path)
            logger.info("queued %s", path)
        else:
            logger.debug("%s already queued", path)


def full_scan():
    logger.info("Performing full scan")
    for root in ROOT_DIRS:
        logger.debug("Scanning %s", root)
        for file in Path(root).rglob(f"*{SRC_EXT}"):
            enqueue(file)


def load_pending():
    logger.info("Loading pending tasks")
    with db_lock:
        rows = conn.execute("SELECT path FROM queue").fetchall()
    for (p,) in rows:
        tasks.put(Path(p))
        logger.info("restored %s", p)


class SrtHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            logger.debug("Detected new file %s", event.src_path)
            enqueue(Path(event.src_path))


def watch():
    observer = Observer()
    for root in ROOT_DIRS:
        logger.debug("Watching %s", root)
        observer.schedule(SrtHandler(), root, recursive=True)
    observer.start()
    logger.info("Observer started")
    try:
        while True:
            time.sleep(1)
    finally:
        observer.stop()
        observer.join()
        logger.info("Observer stopped")


def main():
    logger.info("Starting %d worker threads", WORKERS)
    for _ in range(WORKERS):
        threading.Thread(target=worker, daemon=True).start()

    load_pending()
    full_scan()
    schedule.every().hour.do(full_scan)

    threading.Thread(target=watch, daemon=True).start()
    logger.info("Service started")

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
