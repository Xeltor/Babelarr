#!/usr/bin/env python3
import os
import queue
import threading
import time
from pathlib import Path

import requests
import schedule
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# Configuration via environment variables
ROOT_DIRS = [p for p in os.environ.get("WATCH_DIRS", "/data").split(":") if p]
TARGET_LANGS = os.environ.get("TARGET_LANGS", "nl,bs").split(",")
SRC_EXT = os.environ.get("SRC_EXT", ".en.srt")
API_URL = os.environ.get("LIBRETRANSLATE_URL", "http://libretranslate:5000/translate")
WORKERS = int(os.environ.get("WORKERS", "1"))

# task queue
tasks = queue.Queue()


def translate_file(src: Path, lang: str) -> None:
    """Send the SRT file to LibreTranslate and store the translated version."""
    with open(src, "rb") as fh:
        files = {"file": fh}
        data = {"source": "en", "target": lang, "format": "srt"}
        resp = requests.post(API_URL, files=files, data=data, timeout=60)
        resp.raise_for_status()
    output = src.with_suffix(f".{lang}.srt")
    output.write_bytes(resp.content)
    print(f"[{lang}] saved -> {output}")


def worker():
    while True:
        path = tasks.get()
        try:
            for lang in TARGET_LANGS:
                out = path.with_suffix(f".{lang}.srt")
                if not out.exists():
                    translate_file(path, lang)
        except Exception as exc:
            print(f"translation failed for {path}: {exc}")
        finally:
            tasks.task_done()


def enqueue(path: Path):
    if path.suffix == SRC_EXT and path.is_file():
        tasks.put(path)
        print(f"queued {path}")


def full_scan():
    for root in ROOT_DIRS:
        for file in Path(root).rglob(f"*{SRC_EXT}"):
            enqueue(file)


class SrtHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            enqueue(Path(event.src_path))


def watch():
    observer = Observer()
    for root in ROOT_DIRS:
        observer.schedule(SrtHandler(), root, recursive=True)
    observer.start()
    try:
        while True:
            time.sleep(1)
    finally:
        observer.stop()
        observer.join()


def main():
    for _ in range(WORKERS):
        threading.Thread(target=worker, daemon=True).start()

    full_scan()
    schedule.every().hour.do(full_scan)

    threading.Thread(target=watch, daemon=True).start()

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
