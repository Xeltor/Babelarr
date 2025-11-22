from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from watchdog.events import FileSystemEvent, PatternMatchingEventHandler
from watchdog.observers import Observer

from .ignore import is_path_ignored

if TYPE_CHECKING:
    from .app import Application

logger = logging.getLogger(__name__)


class MkvHandler(PatternMatchingEventHandler):
    def __init__(self, app: Application, root: Path | str | None = None) -> None:
        self.app = app
        self._debounce = self.app.config.debounce
        self._max_wait = self.app.config.stabilize_timeout
        self._recent: dict[Path, float] = {}
        self._last_prune = 0.0
        self._root_path = Path(root) if root is not None else None
        self._queue: queue.Queue[Path | None] = queue.Queue()
        self._stop_event: threading.Event = getattr(
            app, "shutdown_event", threading.Event()
        )
        self._idle_event = threading.Event()
        self._idle_event.set()
        self._worker = threading.Thread(
            target=self._process_queue,
            name="mkv-watch-worker",
            daemon=True,
        )
        self._worker.start()
        super().__init__(
            patterns=["*.mkv"],
            ignore_directories=True,
            case_sensitive=False,
        )

    def _should_ignore(self, path: Path) -> bool:
        return is_path_ignored(path, root=self._root_path)

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
        if self._should_ignore(path):
            logger.debug("mkv_ignore_path path=%s", path)
            return
        self._idle_event.clear()
        self._queue.put(path)

    def _process_path(self, path: Path) -> None:
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

    def on_created(self, event: FileSystemEvent) -> None:
        src = Path(str(event.src_path))
        logger.debug("mkv_detect_new path=%s", src.name)
        self._handle(src)

    def on_moved(self, event: FileSystemEvent) -> None:
        dest = Path(str(event.dest_path))
        logger.debug(
            "mkv_detect_move src=%s dest=%s",
            Path(str(event.src_path)).name,
            dest.name,
        )
        self._invalidate(Path(str(event.src_path)))
        self._handle(dest)

    def on_deleted(self, event: FileSystemEvent) -> None:
        path = Path(str(event.src_path))
        logger.debug("mkv_detect_deleted path=%s", path.name)
        self._invalidate(path)

    def on_modified(self, event: FileSystemEvent) -> None:
        path = Path(str(event.src_path))
        logger.debug("mkv_detect_modified path=%s", path.name)
        self._invalidate(path)
        self._handle(path)

    def _process_queue(self) -> None:
        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                self._queue.task_done()
                break
            try:
                self._process_path(item)
            finally:
                self._queue.task_done()
                if self._queue.empty():
                    self._idle_event.set()

    def wait_until_idle(self, timeout: float | None = None) -> bool:
        return self._idle_event.wait(timeout=timeout)

    def stop(self) -> None:
        self._stop_event.set()
        self._queue.put(None)
        self._worker.join(timeout=self._max_wait + self._debounce)
        self._idle_event.set()


def watch(app: Application) -> None:
    observer = Observer()
    observer.name = "watchdog"
    handlers: list[MkvHandler] = []
    for root in app.config.mkv_dirs or []:
        logger.debug("watch_mkv path=%s", Path(root).name)
        root_path = Path(root)
        if is_path_ignored(root_path, root=root_path):
            logger.info("watch_skip_ignored path=%s", root_path)
            continue
        if not root_path.exists():
            logger.warning("missing_mkv_directory path=%s", root_path.name)
            continue
        handler = MkvHandler(app, root=root_path)
        handlers.append(handler)
        observer.schedule(handler, root, recursive=True)
    observer.start()
    logger.info("observer_started")
    try:
        while not app.shutdown_event.is_set():
            time.sleep(1)
    finally:
        observer.stop()
        observer.join()
        for handler in handlers:
            handler.stop()
        logger.info("observer_stopped")
