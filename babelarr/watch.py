from __future__ import annotations

import logging
import time
from pathlib import Path

from watchdog.events import PatternMatchingEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)


class MkvHandler(PatternMatchingEventHandler):
    def __init__(self, app):
        self.app = app
        self._debounce = self.app.config.debounce
        self._max_wait = self.app.config.stabilize_timeout
        self._recent: dict[Path, float] = {}
        self._last_prune = 0.0
        super().__init__(
            patterns=["*.mkv"],
            ignore_directories=True,
            case_sensitive=False,
        )

    def _wait_for_complete(self, path: Path) -> bool:
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
                logger.warning("mkv_timeout_stabilize path=%s", path.name)
                return False

    def _handle(self, path: Path) -> None:
        now = time.monotonic()
        if now - self._last_prune > self._debounce:
            for p, ts in list(self._recent.items()):
                if now - ts > self._debounce:
                    del self._recent[p]
            self._last_prune = now

        last = self._recent.get(path)
        if last and now - last < self._debounce:
            logger.debug("mkv_skip_recent path=%s age=%.2fs", path.name, now - last)
            return

        if self._wait_for_complete(path):
            self._recent[path] = now
            logger.info("observer_priority path=%s", path.name)
            self.app.handle_new_mkv(path)

    def _invalidate(self, path: Path) -> None:
        self.app.invalidate_mkv_cache_state(path)

    def on_created(self, event):
        logger.debug("mkv_detect_new path=%s", Path(event.src_path).name)
        self._handle(Path(event.src_path))

    def on_moved(self, event):
        dest = Path(event.dest_path)
        logger.debug(
            "mkv_detect_move src=%s dest=%s",
            Path(event.src_path).name,
            dest.name,
        )
        self._invalidate(Path(event.src_path))
        self._handle(dest)

    def on_deleted(self, event):
        path = Path(event.src_path)
        logger.debug("mkv_detect_deleted path=%s", path.name)
        self._invalidate(path)

    def on_modified(self, event):
        path = Path(event.src_path)
        logger.debug("mkv_detect_modified path=%s", path.name)
        if self._wait_for_complete(path):
            self._invalidate(path)
            self._handle(path)


def watch(app) -> None:
    observer = Observer()
    observer.name = "watchdog"
    for root in app.config.mkv_dirs or []:
        logger.debug("watch_mkv path=%s", Path(root).name)
        root_path = Path(root)
        if not root_path.exists():
            logger.warning("missing_mkv_directory path=%s", root_path.name)
            continue
        observer.schedule(MkvHandler(app), root, recursive=True)
    observer.start()
    logger.info("observer_started")
    try:
        while not app.shutdown_event.is_set():
            time.sleep(1)
    finally:
        observer.stop()
        observer.join()
        logger.info("observer_stopped")
