import functools
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import requests

import babelarr.worker as worker_module
from babelarr import cli
from babelarr.config import Config


def test_full_scan(tmp_path, monkeypatch, app):
    first = tmp_path / "one.en.srt"
    first.write_text("a")
    subdir = tmp_path / "sub"
    subdir.mkdir()
    second = subdir / "two.en.srt"
    second.write_text("b")

    app_instance = app()
    called = []
    monkeypatch.setattr(
        app_instance, "enqueue", lambda p, *, priority=0: called.append(p)
    )

    app_instance.full_scan()

    assert sorted(called) == sorted([first, second])


def test_enqueue_tracks_pending_languages(tmp_path, app, monkeypatch):
    src = tmp_path / "video.en.srt"
    src.write_text("hello")
    instance = app()
    monkeypatch.setattr(instance, "_ensure_workers", lambda: None)
    instance.enqueue(src)
    assert instance.pending_translations[src] == {"nl"}


def test_full_scan_logs_completion(tmp_path, caplog, app, monkeypatch):
    first = tmp_path / "one.en.srt"
    first.write_text("a")
    second = tmp_path / "two.en.srt"
    second.write_text("b")

    app_instance = app()
    monkeypatch.setattr(app_instance, "_ensure_workers", lambda: None)
    with caplog.at_level(logging.INFO):
        app_instance.full_scan()

    assert "scan_complete files=2" in caplog.text


def test_request_scan_runs_on_scanner_thread(monkeypatch, app):
    instance = app()
    called: list[str] = []

    def fake_full_scan():
        called.append(threading.current_thread().name)

    monkeypatch.setattr(instance, "full_scan", fake_full_scan)

    thread = threading.Thread(target=instance.scan_worker, name="scanner")
    thread.start()
    instance.request_scan()
    instance.scan_queue.join()
    instance.shutdown_event.set()
    thread.join()

    assert called == ["scanner"]


def test_scan_tasks_lower_priority_than_watcher(tmp_path, app, monkeypatch):
    scan_file = tmp_path / "scan.en.srt"
    scan_file.write_text("a")
    instance = app()

    order: list[Path] = []

    class RecordingTranslator:
        def translate(self, path, lang):
            order.append(path)
            return b""

        def wait_until_available(self):
            return None

    instance.translator = RecordingTranslator()

    monkeypatch.setattr(instance, "_ensure_workers", lambda: None)

    instance.full_scan()

    watch_file = tmp_path / "watch.en.srt"
    watch_file.write_text("b")
    instance.enqueue(watch_file)

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(worker_module.worker, instance)
        instance.tasks.join()
        instance.shutdown_event.set()

    assert order == [watch_file, scan_file]


def test_db_persistence_across_restarts(tmp_path, app):
    src = tmp_path / "video.en.srt"
    src.write_text("hello")

    app1 = app()
    app1._ensure_workers = lambda: None
    app1.enqueue(src, priority=5)

    app2 = app()
    app2._ensure_workers = lambda: None
    app2.load_pending()
    assert app2.tasks.queue[0][0] == 5

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(worker_module.worker, app2)
        app2.tasks.join()
        app2.shutdown_event.set()

    assert app2.output_path(src, "nl").exists()


def test_load_pending_logs_summary(tmp_path, caplog, app):
    first = tmp_path / "one.en.srt"
    first.write_text("a")
    second = tmp_path / "two.en.srt"
    second.write_text("b")

    app1 = app()
    app1._ensure_workers = lambda: None
    app1.enqueue(first)
    app1.enqueue(second)

    app2 = app()
    app2._ensure_workers = lambda: None

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        app2.load_pending()

    info_restored = [
        rec.message
        for rec in caplog.records
        if rec.levelno == logging.INFO and "restored" in rec.message
    ]
    assert info_restored == ["load_pending restored=2"]

    debug_restored = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.DEBUG and rec.message.startswith("restored ")
    ]
    assert len(debug_restored) == 2


def test_validate_environment_no_valid_dirs(tmp_path, monkeypatch):
    cfg = Config(
        root_dirs=[str(tmp_path / "missing")],
        target_langs=["nl"],
        src_lang="en",
        src_ext=".en.srt",
        api_url="http://example",
        workers=1,
        queue_db=str(tmp_path / "queue.db"),
    )

    monkeypatch.setattr(
        cli.requests, "head", lambda *a, **k: type("R", (), {"status_code": 200})()
    )

    with pytest.raises(SystemExit):
        cli.validate_environment(cfg)


def test_validate_environment_api_unreachable(config, monkeypatch, caplog):
    def fail(*a, **k):
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(cli.requests, "head", fail)
    with caplog.at_level(logging.ERROR):
        cli.validate_environment(config)
        assert "service_unreachable" in caplog.text


def test_configurable_scan_interval(monkeypatch, config, app):
    config.scan_interval_minutes = 5
    instance = app(cfg=config)

    import babelarr.app as app_mod

    called: dict[str, int] = {}

    def fake_every(n):
        called["interval"] = n

        class Job:
            def _minutes(self):
                return self

            def do(self, func):
                called["func"] = func
                return self

            minutes = property(_minutes)

        return Job()

    monkeypatch.setattr(app_mod.schedule, "every", fake_every)
    monkeypatch.setattr(app_mod.watch_module, "watch", lambda _app: None)
    monkeypatch.setattr(instance, "scan_worker", lambda: None)
    instance.shutdown_event.set()
    instance.run()

    assert called["interval"] == 5
    assert called["func"].__self__ is instance
    assert called["func"].__func__ is instance.request_scan.__func__


def test_workers_spawn_and_exit(tmp_path, app, caplog, monkeypatch):
    src = tmp_path / "video.en.srt"
    src.write_text("hello")

    class BlockingTranslator:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()

        def translate(self, path, lang):
            self.started.set()
            self.release.wait()
            return b""

        def wait_until_available(self):
            return None

    translator = BlockingTranslator()
    import babelarr.app as app_module

    monkeypatch.setattr(
        app_module,
        "worker_loop",
        functools.partial(worker_module.worker, idle_timeout=0.1),
    )
    instance = app(translator=translator)
    assert instance._active_workers == 0

    instance.enqueue(src)

    assert translator.started.wait(timeout=1)
    assert instance._active_workers > 0

    translator.release.set()
    instance.tasks.join()
    for t in list(instance._worker_threads):
        t.join(timeout=1)

    assert instance._active_workers == 0


def test_worker_limit_enforced_and_threads_exit(tmp_path, app, monkeypatch):
    cfg = Config(
        root_dirs=[str(tmp_path)],
        target_langs=["nl"],
        src_lang="en",
        src_ext=".en.srt",
        api_url="http://example",
        workers=2,
        queue_db=str(tmp_path / "queue.db"),
    )

    start = threading.Event()
    active_counts: list[int] = []

    import babelarr.app as app_module

    original_get_task = worker_module.get_task

    def blocking_get_task(app_instance):
        start.wait()
        return original_get_task(app_instance)

    monkeypatch.setattr(worker_module, "get_task", blocking_get_task)

    def recording_worker(app_instance):
        active_counts.append(app_instance._active_workers)
        worker_module.worker(app_instance, idle_timeout=0.1)

    monkeypatch.setattr(app_module, "worker_loop", recording_worker)

    instance = app(cfg=cfg)

    for i in range(5):
        src = tmp_path / f"video{i}.en.srt"
        src.write_text("hello")
        instance.enqueue(src)

    for _ in range(20):
        if len(active_counts) == cfg.workers:
            break
        time.sleep(0.05)
    assert len(active_counts) == cfg.workers
    assert instance._active_workers == cfg.workers

    start.set()
    instance.tasks.join()
    for t in list(instance._worker_threads):
        t.join(timeout=1)

    assert max(active_counts) == cfg.workers
    assert instance._active_workers == 0
    assert not instance._worker_threads


def test_workers_use_unique_names_and_recycle(tmp_path, app, config):
    config.workers = 2
    src1 = tmp_path / "one.en.srt"
    src2 = tmp_path / "two.en.srt"
    src1.write_text("a")
    src2.write_text("b")

    class RecordingTranslator:
        def __init__(self):
            self.names: list[str] = []
            self.started = threading.Event()
            self.release = threading.Event()

        def translate(self, path, lang):  # pragma: no cover - simple
            self.names.append(threading.current_thread().name)
            if len(self.names) == 2:
                self.started.set()
            self.release.wait()
            return b""

        def wait_until_available(self):  # pragma: no cover - simple
            return None

    translator = RecordingTranslator()
    instance = app(cfg=config, translator=translator)

    original_ensure = instance._ensure_workers
    instance._ensure_workers = lambda: None
    instance.enqueue(src1)
    instance.enqueue(src2)
    instance._ensure_workers = original_ensure

    instance._ensure_workers()
    assert translator.started.wait(timeout=1)

    threads = list(instance._worker_threads)
    used_names = set(translator.names)
    assert len(used_names) == 2
    assert used_names <= instance._worker_name_pool
    assert instance._available_worker_names == instance._worker_name_pool - used_names

    translator.release.set()
    instance.tasks.join()
    instance.shutdown_event.set()
    for t in threads:
        t.join(timeout=1)

    assert instance._available_worker_names == instance._worker_name_pool


def test_worker_name_pool_has_enough_names():
    import babelarr.app as app_mod

    assert len(app_mod.WORKER_NAMES) >= 10


def test_translate_file_does_not_refresh_jellyfin(tmp_path, app):
    src = tmp_path / "video.en.srt"
    src.write_text("hello")

    called: list[Path] = []

    class DummyJellyfin:
        def refresh_path(self, path: Path) -> None:  # pragma: no cover - trivial
            called.append(path)

    instance = app(jellyfin=DummyJellyfin())
    instance.translate_file(src, "nl")

    assert called == []


def test_translation_done_logs_jellyfin_refresh(tmp_path, app, caplog):
    src = tmp_path / "video.en.srt"
    src.write_text("hello")

    triggered: list[Path] = []

    class DummyJellyfin:
        def refresh_path(self, path: Path) -> None:  # pragma: no cover - trivial
            triggered.append(path)

    instance = app(jellyfin=DummyJellyfin())
    with instance._pending_lock:
        instance.pending_translations[src] = {"nl"}

    caplog.clear()
    with caplog.at_level(logging.INFO):
        instance.translation_done(src, "nl")

    assert triggered == [tmp_path]
    assert "jellyfin_refresh" in caplog.text
    assert f"path={tmp_path.name}" in caplog.text
    assert f"show={tmp_path.parent.name}" in caplog.text
    assert "lang=" not in caplog.text


def test_translation_done_refreshes_once_per_folder(tmp_path, app, caplog):
    first = tmp_path / "one.en.srt"
    second = tmp_path / "two.en.srt"
    first.write_text("a")
    second.write_text("b")

    triggered: list[Path] = []

    class DummyJellyfin:
        def refresh_path(self, path: Path) -> None:  # pragma: no cover - trivial
            triggered.append(path)

    instance = app(jellyfin=DummyJellyfin())
    with instance._pending_lock:
        instance.pending_translations[first] = {"nl"}
        instance.pending_translations[second] = {"nl"}

    caplog.clear()
    with caplog.at_level(logging.INFO):
        instance.translation_done(first, "nl")
        instance.translation_done(second, "nl")

    assert triggered == [tmp_path]
    assert caplog.text.count("jellyfin_refresh") == 1
    assert f"show={tmp_path.parent.name}" in caplog.text
    assert "lang=" not in caplog.text
