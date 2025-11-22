import threading
import time
from pathlib import Path
from typing import cast

import pytest

from babelarr.mkv_scan import MkvScanner, ProcessResult
from babelarr.mkv_work_index import MkvWorkIndex
from babelarr.mkv_workflow import MkvWorkflow


def _stat_payload(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def test_work_index_recover_and_cleanup(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.db"
    index = MkvWorkIndex(db_path)
    movie = tmp_path / "movie.mkv"
    movie.write_text("content")
    missing = tmp_path / "missing.mkv"
    missing.write_text("gone")
    missing_mtime, missing_size = _stat_payload(missing)
    missing.unlink()

    mtime, size = _stat_payload(movie)
    index.record_pending(movie, mtime_ns=mtime, size_bytes=size, priority=0)
    index.mark_in_progress(movie)
    index.record_pending(
        missing,
        mtime_ns=missing_mtime,
        size_bytes=missing_size,
        priority=1,
    )

    recovered = index.recover_pending()

    assert (movie, 0) in recovered
    assert all(path != missing for path, _ in recovered)

    index.mark_finished(
        movie,
        mtime_ns=mtime,
        size_bytes=size,
        pending=False,
        missing=False,
    )

    assert index.recover_pending() == []


def test_work_index_handles_pending_and_prune(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.db"
    index = MkvWorkIndex(db_path)
    movie = tmp_path / "movie.mkv"
    movie.write_text("content")
    mtime, size = _stat_payload(movie)
    index.record_pending(movie, mtime_ns=mtime, size_bytes=size, priority=2)
    index.mark_in_progress(movie)
    index.mark_finished(
        movie, mtime_ns=None, size_bytes=None, pending=True, missing=False
    )

    # Entry remains pending after failed completion.
    recovered = index.recover_pending()
    assert recovered == [(movie, 1)]

    # Prune removes entries not in valid set.
    index.prune_missing([])
    assert index.recover_pending() == []


class _DummyScanner:
    def __init__(self) -> None:
        self.processed: list[Path] = []

    def scan(self) -> tuple[int, list[tuple[Path, int]], list[Path]]:
        return 0, [], []

    def process_file(
        self,
        path: Path,
        *,
        position: int | None = None,
        total_paths: int | None = None,
    ) -> ProcessResult:
        self.processed.append(path)
        mtime, size = _stat_payload(path)
        return ProcessResult(
            translated=0,
            pending=False,
            mtime_ns=mtime,
            size_bytes=size,
            missing=False,
        )


def test_workflow_replays_persisted_tasks(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.db"
    index = MkvWorkIndex(db_path)
    mkv = tmp_path / "movie.mkv"
    mkv.write_text("payload")
    mtime, size = _stat_payload(mkv)
    index.record_pending(mkv, mtime_ns=mtime, size_bytes=size, priority=1)

    scanner = _DummyScanner()
    shutdown = threading.Event()
    workflow = MkvWorkflow(
        scanner=cast(MkvScanner, scanner),
        worker_count=1,
        shutdown_event=shutdown,
        profiler=None,
        work_index=index,
    )

    workflow.start()
    workflow.mkv_scan_queue.join()
    workflow.stop()
    time.sleep(0.05)

    assert mkv in scanner.processed
    assert index.recover_pending() == []


def test_queue_status_reports_counts(tmp_path: Path) -> None:
    scanner = _DummyScanner()
    shutdown = threading.Event()
    workflow = MkvWorkflow(
        scanner=cast(MkvScanner, scanner),
        worker_count=1,
        shutdown_event=shutdown,
        profiler=None,
    )
    priority_path = tmp_path / "priority.mkv"
    priority_path.write_text("a")
    normal_path = tmp_path / "normal.mkv"
    normal_path.write_text("b")

    workflow.enqueue_translation(priority_path, priority=0)
    workflow.enqueue_translation(normal_path, priority=1)

    status = workflow.queue_status()

    assert status["queue_size"] == 2
    assert status["priority_0"] == 1
    assert status["priority_1"] == 1
    assert status["pending_paths"] == 2


def test_work_index_no_db_is_noop(tmp_path: Path) -> None:
    index = MkvWorkIndex(None)
    mkv = tmp_path / "movie.mkv"
    mkv.write_text("data")
    mtime, size = _stat_payload(mkv)

    index.record_pending(mkv, mtime_ns=mtime, size_bytes=size, priority=0)
    index.mark_in_progress(mkv)
    index.mark_finished(
        mkv, mtime_ns=mtime, size_bytes=size, pending=False, missing=False
    )
    assert index.recover_pending() == []


def test_work_index_dir_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_mkdir(self: Path, parents: bool = False, exist_ok: bool = False) -> None:
        raise OSError("fail")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    index = MkvWorkIndex(tmp_path / "missing" / "cache.db")
    assert index._db_path is None
    assert index._conn is None
