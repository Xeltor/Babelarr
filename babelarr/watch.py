from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from watchdog.events import PatternMatchingEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover
    from .app import Application


class SrtHandler(PatternMatchingEventHandler):
    def __init__(self, app: Application):
        self.app = app
        self._debounce = self.app.config.debounce
        self._max_wait = 30
        self._recent: dict[Path, float] = {}
        self._last_prune = 0.0
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
                logger.warning("timeout_stabilize path=%s", path.name)
                return False

    def _handle(self, path: Path) -> None:
        now = time.monotonic()
        # prune expired entries at most once per debounce interval
        if now - self._last_prune > self._debounce:
            for p, ts in list(self._recent.items()):
                if now - ts > self._debounce:
                    del self._recent[p]
            self._last_prune = now

        last = self._recent.get(path)
        if last and now - last < self._debounce:
            logger.debug("skip_recent path=%s age=%.2fs", path.name, now - last)
            return

        if self._wait_for_complete(path):
            self._recent[path] = now
            self.app.enqueue(path)

    def on_created(self, event):
        logger.debug("detect_new path=%s", Path(event.src_path).name)
        self._handle(Path(event.src_path))

    def on_deleted(self, event):
        logger.debug("detect_deleted path=%s", Path(event.src_path).name)
        self.app.db.remove(Path(event.src_path))

    def on_modified(self, event):
        """Ignore file modification events."""
        return

    def on_moved(self, event):
        dest = Path(event.dest_path)
        logger.debug(
            "detect_move src=%s dest=%s",
            Path(event.src_path).name,
            dest.name,
        )
        self._handle(dest)


def watch(app: Application) -> None:
    """Launch a watchdog observer for all configured directories and block until shutdown."""
    observer = Observer()
    for root in app.config.root_dirs:
        logger.debug("watch path=%s", Path(root).name)
        root_path = Path(root)
        if not root_path.exists():
            logger.warning("missing_directory path=%s", root_path.name)
            continue
        observer.schedule(SrtHandler(app), root, recursive=True)
    observer.start()
    logger.info("observer_started")
    try:
        while not app.shutdown_event.is_set():
            time.sleep(1)
    finally:
        observer.stop()
        observer.join()
        logger.info("observer_stopped")
