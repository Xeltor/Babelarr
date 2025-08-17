import logging
import queue
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import requests

from babelarr import cli
from babelarr.app import TranslationTask
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
        executor.submit(instance.worker)
        instance.tasks.join()
        instance.shutdown_event.set()

    assert order == [watch_file, scan_file]


def test_db_persistence_across_restarts(tmp_path, app):
    src = tmp_path / "video.en.srt"
    src.write_text("hello")

    app1 = app()
    app1.enqueue(src)

    app2 = app()
    app2.load_pending()
    app2.tasks.join()
    assert app2.output_path(src, "nl").exists()


def test_worker_retry_on_network_failure(tmp_path, caplog, app):
    src = tmp_path / "fail.en.srt"
    src.write_text("hello")

    class UnstableTranslator:
        def __init__(self):
            self.attempts = 0

        def translate(self, path, lang):
            self.attempts += 1
            if self.attempts == 1:
                raise requests.ConnectionError("boom")
            return b"ok"

        def wait_until_available(self):
            return None

    translator = UnstableTranslator()
    app_instance = app(translator=translator)

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(app_instance.worker)

        with caplog.at_level(logging.ERROR):
            app_instance.enqueue(src)
            app_instance.tasks.join()
            assert any("translation failed" in r.message for r in caplog.records)

        app_instance.shutdown_event.set()

    assert app_instance.output_path(src, "nl").exists()


def test_queue_length_logging(tmp_path, monkeypatch, app, config, caplog):
    sub_file = tmp_path / "video.en.srt"
    sub_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    config.src_ext = ".srt"
    app_instance = app(cfg=config)

    def fake_translate_file(src, lang, task_id=None):
        app_instance.output_path(src, lang).write_text("Hallo")
        return True

    monkeypatch.setattr(app_instance, "translate_file", fake_translate_file)

    with caplog.at_level(logging.INFO):
        app_instance.enqueue(sub_file)
        assert "queue length: 1" in caplog.text
        caplog.clear()

        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(app_instance.worker)
            app_instance.tasks.join()
            app_instance.shutdown_event.set()

        assert "queue length: 0" in caplog.text
    assert app_instance.db.all() == []


def test_worker_skips_output_if_source_deleted(tmp_path, caplog, app):
    src = tmp_path / "gone.en.srt"
    src.write_text("hello")

    class DeletingTranslator:
        def translate(self, path, lang):
            path.unlink()
            return b"ok"

        def wait_until_available(self):
            return None

    translator = DeletingTranslator()
    app_instance = app(translator=translator)

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(app_instance.worker)

        with caplog.at_level(logging.INFO):
            app_instance.enqueue(src)
            app_instance.tasks.join()
            assert "disappeared" in caplog.text
            assert "skipped" in caplog.text.lower()
            assert "succeeded" not in caplog.text.lower()

        app_instance.shutdown_event.set()

    assert not app_instance.output_path(src, "nl").exists()
    assert app_instance.db.all() == []


def test_worker_logs_processing_time(tmp_path, caplog, app):
    src = tmp_path / "video.en.srt"
    src.write_text("hello")

    app_instance = app()

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(app_instance.worker)

        with caplog.at_level(logging.DEBUG):
            app_instance.enqueue(src)
            app_instance.tasks.join()
            app_instance.shutdown_event.set()

    assert any(
        rec.levelno == logging.DEBUG
        and "finished processing" in rec.message.lower()
        and str(src) in rec.message
        and "[nl]" in rec.message
        for rec in caplog.records
    )


def test_worker_translating_logged_as_debug(tmp_path, caplog, app):
    src = tmp_path / "video.en.srt"
    src.write_text("hello")

    app_instance = app()

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="worker") as executor:
        executor.submit(app_instance.worker)

        with caplog.at_level(logging.DEBUG):
            app_instance.enqueue(src)
            app_instance.tasks.join()
            app_instance.shutdown_event.set()

    assert any(
        rec.levelno == logging.DEBUG
        and rec.message.startswith("Worker worker_0")
        and re.search(r"\btranslating\b", rec.message)
        for rec in caplog.records
    )
    assert not any(
        rec.levelno == logging.INFO and re.search(r"\btranslating\b", rec.message)
        for rec in caplog.records
    )


def test_translation_logs_summary_once(tmp_path, caplog, app):
    src = tmp_path / "video.en.srt"
    src.write_text("hello")

    app_instance = app()

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(app_instance.worker)

        with caplog.at_level(logging.INFO):
            app_instance.enqueue(src)
            app_instance.tasks.join()
            app_instance.shutdown_event.set()

    info_logs = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.INFO and rec.message.startswith("translation ")
    ]
    assert len(info_logs) == 1
    msg = info_logs[0].message
    assert str(src) in msg
    assert "nl" in msg
    assert "succeeded" in msg
    assert re.search(r"in \d+\.\d+s", msg)


def test_workers_wait_when_translator_unavailable(tmp_path, app):
    src = tmp_path / "fail.en.srt"
    src.write_text("hello")

    class FlakyTranslator:
        def __init__(self):
            self.calls = 0
            self.wait_calls = 0

        def translate(self, path, lang):
            self.calls += 1
            if self.calls == 1:
                raise requests.ConnectionError("boom")
            assert self.wait_calls >= 2
            return b"ok"

        def wait_until_available(self):
            self.wait_calls += 1
            return None

    translator = FlakyTranslator()
    app_instance = app(translator=translator)

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(app_instance.worker)
        app_instance.enqueue(src)
        app_instance.tasks.join()
        app_instance.shutdown_event.set()

    assert translator.calls == 2
    assert translator.wait_calls >= 2
    assert app_instance.output_path(src, "nl").exists()


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
        assert "Translation service" in caplog.text


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
    monkeypatch.setattr(instance, "watch", lambda: None)
    monkeypatch.setattr(instance, "scan_worker", lambda: None)
    instance.shutdown_event.set()
    instance.run()

    assert called["interval"] == 5
    assert called["func"].__self__ is instance
    assert called["func"].__func__ is instance.request_scan.__func__


def test_worker_wait_called_once(app):
    calls = {"count": 0}

    class Translator:
        def wait_until_available(self):
            calls["count"] += 1

        def translate(self, path, lang):
            return b""

    instance = app(translator=Translator())

    def fast_get(timeout=1):
        raise queue.Empty

    instance.tasks.get = fast_get

    thread = threading.Thread(target=instance.worker)
    thread.start()
    time.sleep(0.1)
    instance.shutdown_event.set()
    thread.join()

    assert calls["count"] == 1


def test_workers_spawn_and_exit(tmp_path, app):
    src = tmp_path / "video.en.srt"
    src.write_text("hello")

    instance = app()
    assert instance._active_workers == 0
    instance.enqueue(src)

    for _ in range(100):
        if instance._active_workers > 0:
            break
        time.sleep(0.05)
    assert instance._active_workers > 0

    instance.tasks.join()
    for _ in range(100):
        if instance._active_workers == 0:
            break
        time.sleep(0.05)
    assert instance._active_workers == 0


def test_get_task_returns_task_or_none(tmp_path, app):
    instance = app()
    task = TranslationTask(tmp_path / "video.en.srt", "nl", "1")
    instance.tasks.put((task.priority, instance._task_counter, task))
    instance._task_counter += 1
    assert instance._get_task() == task
    assert instance._get_task() is None


def test_process_translation_missing_file(tmp_path, caplog, app):
    instance = app()
    task = TranslationTask(tmp_path / "missing.en.srt", "nl", "1")
    with caplog.at_level(logging.WARNING):
        result = instance._process_translation(task, "worker")
    assert result is False
    assert "missing" in caplog.text


def test_handle_failure_requeues_on_request_exception(app):
    instance = app()
    task = TranslationTask(Path("a"), "nl", "1")
    calls = []

    def fake_wait():
        calls.append(1)

    instance.translator_available.set()
    requeue = instance._handle_failure(
        task, requests.RequestException("boom"), "worker", fake_wait
    )
    assert requeue is True
    assert calls == [1]


def test_handle_failure_generic_exception(app):
    instance = app()
    task = TranslationTask(Path("a"), "nl", "1")
    instance.translator_available.set()
    requeue = instance._handle_failure(task, RuntimeError("boom"), "worker", None)
    assert requeue is False
