from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from pathlib import Path
from typing import NamedTuple

from .mkv_scan import MkvScanner
from .mkv_work_index import MkvWorkIndex
from .profiling import WorkloadProfiler

logger = logging.getLogger(__name__)


class _QueueEntry(NamedTuple):
    path: Path
    position: int | None = None
    total_paths: int | None = None


class MkvWorkflow:
    def __init__(
        self,
        scanner: MkvScanner,
        worker_count: int,
        shutdown_event: threading.Event,
        profiler: WorkloadProfiler | None = None,
        work_index: MkvWorkIndex | None = None,
    ) -> None:
        self.scanner = scanner
        self.worker_count = max(1, worker_count)
        self.shutdown_event = shutdown_event
        self.profiler = profiler or WorkloadProfiler(enabled=False)
        self.work_index = work_index

        self.mkv_scan_queue: queue.PriorityQueue[tuple[int, int, _QueueEntry]] = (
            queue.PriorityQueue()
        )
        self._queue_counter = itertools.count()
        self._scan_event = threading.Event()
        self._scan_thread: threading.Thread | None = None
        self._translation_threads: list[threading.Thread] = []
        self._priority_enqueue_times: dict[str, float] = {}
        self._priority_lock = threading.Lock()
        self._pending_paths: set[str] = set()
        self._pending_rescan_priorities: dict[str, int] = {}
        self._pending_lock = threading.Lock()

    def start(self) -> None:
        if self._scan_thread:
            return
        recovered = self._recover_pending_tasks()
        self._scan_thread = threading.Thread(
            target=self._scan_loop,
            name="mkv-scanner",
        )
        self._scan_thread.start()
        for idx in range(self.worker_count):
            worker = threading.Thread(
                target=self._translation_worker,
                name=f"mkv-worker-{idx+1}",
            )
            worker.start()
            self._translation_threads.append(worker)
        if recovered:
            logger.info("recovered_pending_work items=%d", recovered)

    def stop(self) -> None:
        self.shutdown_event.set()
        self._scan_event.set()
        for worker in self._translation_threads:
            worker.join()
        if self._scan_thread:
            self._scan_thread.join()

    def request_scan(self) -> None:
        self._scan_event.set()

    def handle_new_mkv(self, path: Path) -> None:
        self.enqueue_translation(path, priority=0)

    def enqueue_translation(
        self,
        path: Path,
        priority: int = 1,
        *,
        position: int | None = None,
        total_paths: int | None = None,
    ) -> None:
        if not path.is_file() or path.suffix.lower() != ".mkv":
            return
        if priority not in (0, 1):
            priority = 1
        if self.work_index:
            try:
                stat = path.stat()
            except FileNotFoundError:
                return
            self.work_index.record_pending(
                path,
                mtime_ns=stat.st_mtime_ns,
                size_bytes=stat.st_size,
                priority=priority,
            )
        key = str(path)
        with self._pending_lock:
            if key in self._pending_paths:
                previous = self._pending_rescan_priorities.get(key)
                if previous is None or priority < previous:
                    self._pending_rescan_priorities[key] = priority
                logger.debug(
                    "enqueue_skip path=%s priority=%d reason=pending",
                    path.name,
                    priority,
                )
                return
            self._pending_paths.add(key)
            self._pending_rescan_priorities.pop(key, None)
        with self.profiler.track("mkv.workflow.enqueue"):
            if priority == 0:
                self._record_priority_enqueue(path)
            snapshot: int | None = None
            if position is None or total_paths is None:
                snapshot = self.mkv_scan_queue.qsize()
            entry_position = position if position is not None else (snapshot or 0) + 1
            entry_total_paths = (
                total_paths if total_paths is not None else (snapshot or 0) + 1
            )
            entry = _QueueEntry(
                path=path,
                position=entry_position,
                total_paths=entry_total_paths,
            )
            self.mkv_scan_queue.put((priority, next(self._queue_counter), entry))
            logger.debug("enqueue_translation path=%s priority=%d", path.name, priority)

    def _record_priority_enqueue(self, path: Path) -> None:
        key = str(path)
        with self._priority_lock:
            self._priority_enqueue_times[key] = time.monotonic()

    def queue_status(self) -> dict[str, object]:
        priority_counts = {0: 0, 1: 0}
        try:
            with self.mkv_scan_queue.mutex:
                for priority, _, _ in list(self.mkv_scan_queue.queue):
                    if priority in priority_counts:
                        priority_counts[priority] += 1
        except Exception:  # pragma: no cover - best effort snapshot
            pass
        with self._pending_lock:
            pending = len(self._pending_paths)
            pending_rescans = len(self._pending_rescan_priorities)
        return {
            "queue_size": self.mkv_scan_queue.qsize(),
            "priority_0": priority_counts[0],
            "priority_1": priority_counts[1],
            "pending_paths": pending,
            "pending_rescans": pending_rescans,
        }

    def _recover_pending_tasks(self) -> int:
        if not self.work_index:
            return 0
        recovered = self.work_index.recover_pending()
        for path, priority in recovered:
            self.enqueue_translation(path, priority=priority)
        return len(recovered)

    def _record_priority_wait(self, path: Path) -> None:
        key = str(path)
        with self._priority_lock:
            enqueued = self._priority_enqueue_times.pop(key, None)
        if enqueued is None:
            return
        wait = time.monotonic() - enqueued
        self.profiler.record("mkv.priority_wait", wait)

    def _scan_loop(self) -> None:
        name = threading.current_thread().name
        logger.debug("mkv_scan_thread_start name=%s", name)
        while not self.shutdown_event.is_set():
            triggered = self._scan_event.wait(timeout=1)
            if not triggered:
                continue
            self._scan_event.clear()
            if self.shutdown_event.is_set():
                break
            with self.profiler.track("mkv.workflow.scan"):
                files, tasks, recent_paths = self.scanner.scan()
            queued = 0
            total_tasks = len(tasks)
            for idx, (path, priority) in enumerate(tasks):
                self.enqueue_translation(
                    path,
                    priority,
                    position=idx + 1 if total_tasks else None,
                    total_paths=total_tasks if total_tasks else None,
                )
                queued += 1
            logger.info(
                "mkv_scan_complete files=%d queued=%d recent_priority=%d",
                files,
                queued,
                len(recent_paths),
            )
        logger.debug("mkv_scan_thread_exit name=%s", name)

    def _translation_worker(self) -> None:
        name = threading.current_thread().name
        logger.debug("translation_worker_start name=%s", name)
        while True:
            if self.shutdown_event.is_set() and self.mkv_scan_queue.empty():
                break
            path: Path | None = None
            try:
                _, _, entry = self.mkv_scan_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                path = entry.path
                if not path.is_file():
                    continue
                self._record_priority_wait(path)
                if self.work_index:
                    self.work_index.mark_in_progress(path)
                result = None
                with self.profiler.track("mkv.workflow.process_file"):
                    result = self.scanner.process_file(
                        path,
                        position=entry.position,
                        total_paths=entry.total_paths,
                    )
                if self.work_index and result:
                    self.work_index.mark_finished(
                        path,
                        mtime_ns=result.mtime_ns,
                        size_bytes=result.size_bytes,
                        pending=result.pending,
                        missing=result.missing,
                    )
            except Exception as exc:
                logger.error(
                    "translation_fail path=%s error=%s",
                    path.name if path else "-",
                    exc,
                )
                if self.work_index:
                    self.work_index.mark_finished(
                        entry.path,
                        mtime_ns=None,
                        size_bytes=None,
                        pending=True,
                        missing=False,
                    )
            finally:
                self.mkv_scan_queue.task_done()
                self._complete_pending(entry.path)
        logger.debug("translation_worker_exit name=%s", name)

    def _complete_pending(self, path: Path) -> None:
        key = str(path)
        with self._pending_lock:
            self._pending_paths.discard(key)
            rescan = self._pending_rescan_priorities.pop(key, None)
        if rescan is not None:
            logger.debug(
                "reschedule_translation path=%s priority=%d", path.name, rescan
            )
            self.enqueue_translation(path, priority=rescan)
