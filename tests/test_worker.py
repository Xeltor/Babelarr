import logging
import queue
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

import babelarr.worker as worker_module
from babelarr.worker import TranslationTask


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
        executor.submit(worker_module.worker, app_instance)

        with caplog.at_level(logging.ERROR):
            app_instance.enqueue(src)
            app_instance.tasks.join()
            assert any("translation_failed" in r.message for r in caplog.records)

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
    monkeypatch.setattr(app_instance, "_ensure_workers", lambda: None)

    with caplog.at_level(logging.INFO):
        app_instance.enqueue(sub_file)
        queued_logs = [r for r in caplog.records if r.levelno == logging.INFO]
        assert any("queue=1" in r.message for r in queued_logs)
        assert any(
            f"path={sub_file.name}" in r.message
            and "lang=nl" in r.message
            and "task_id=" in r.message
            for r in queued_logs
        )
        caplog.clear()

        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(worker_module.worker, app_instance)
            app_instance.tasks.join()
            app_instance.shutdown_event.set()

        done_logs = [r for r in caplog.records if "queue=0" in r.message]
        assert any(
            f"path={sub_file.name}" in r.message and "lang=nl" in r.message
            for r in done_logs
        )
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
        executor.submit(worker_module.worker, app_instance)

        with caplog.at_level(logging.INFO):
            app_instance.enqueue(src)
            app_instance.tasks.join()
            app_instance.shutdown_event.set()
            assert "reason=source_missing" in caplog.text
            assert "outcome=skipped" in caplog.text
            assert "succeeded" not in caplog.text

    assert not app_instance.output_path(src, "nl").exists()
    assert app_instance.db.all() == []


def test_worker_logs_processing_time(tmp_path, caplog, app):
    src = tmp_path / "video.en.srt"
    src.write_text("hello")

    app_instance = app()

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(worker_module.worker, app_instance)

        with caplog.at_level(logging.DEBUG):
            app_instance.enqueue(src)
            app_instance.tasks.join()
            app_instance.shutdown_event.set()

    assert any(
        rec.levelno == logging.DEBUG
        and rec.message.startswith("worker_finish")
        and f"path={src.name}" in rec.message
        and "lang=nl" in rec.message
        and "task_id=" in rec.message
        for rec in caplog.records
    )


def test_worker_translating_logged_as_debug(tmp_path, caplog, app):
    src = tmp_path / "video.en.srt"
    src.write_text("hello")

    app_instance = app()

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="worker") as executor:
        executor.submit(worker_module.worker, app_instance)

        with caplog.at_level(logging.DEBUG):
            app_instance.enqueue(src)
            app_instance.tasks.join()
            app_instance.shutdown_event.set()

    assert any(
        rec.levelno == logging.DEBUG
        and rec.message.startswith("worker_translate")
        and "name=worker_0" in rec.message
        for rec in caplog.records
    )
    assert not any(
        rec.levelno == logging.INFO and "worker_translate" in rec.message
        for rec in caplog.records
    )


def test_translation_logs_summary_once(tmp_path, caplog, app):
    src = tmp_path / "video.en.srt"
    src.write_text("hello")

    app_instance = app()

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(worker_module.worker, app_instance)

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
    assert f"path={src.name}" in msg
    assert "lang=nl" in msg
    assert "task_id=" in msg
    assert "outcome=succeeded" in msg
    assert re.search(r"duration=\d+\.\d+s", msg)


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
        executor.submit(worker_module.worker, app_instance)
        app_instance.enqueue(src)
        app_instance.tasks.join()
        app_instance.shutdown_event.set()

    assert translator.calls == 2
    assert translator.wait_calls >= 2
    assert app_instance.output_path(src, "nl").exists()


def test_worker_wait_called_once(app):
    calls = {"count": 0}

    class Translator:
        def __init__(self):
            self.called = threading.Event()

        def wait_until_available(self):
            calls["count"] += 1
            self.called.set()

        def translate(self, path, lang):
            return b""

    translator = Translator()
    instance = app(translator=translator)

    def fast_get(timeout=1):
        raise queue.Empty

    instance.tasks.get = fast_get

    thread = threading.Thread(target=worker_module.worker, args=(instance,))
    thread.start()
    assert translator.called.wait(timeout=1)
    thread.join(timeout=1)

    assert calls["count"] == 1


def test_get_task_returns_task_or_none(tmp_path, app):
    instance = app()
    task = TranslationTask(tmp_path / "video.en.srt", "nl", "1")
    instance.tasks.put((task.priority, instance._task_counter, task))
    instance._task_counter += 1
    assert worker_module.get_task(instance) == task
    assert worker_module.get_task(instance) is None


def test_process_translation_missing_file(tmp_path, caplog, app):
    instance = app()
    task = TranslationTask(tmp_path / "missing.en.srt", "nl", "1")
    with caplog.at_level(logging.WARNING):
        result = worker_module.process_translation(instance, task, "worker")
    assert result is False
    assert "missing" in caplog.text


def test_handle_failure_requeues_on_request_exception(app):
    instance = app()
    task = TranslationTask(Path("a"), "nl", "1")
    calls = []

    def fake_wait():
        calls.append(1)

    instance.translator_available.set()
    requeue = worker_module.handle_failure(
        instance, task, requests.RequestException("boom"), "worker", fake_wait
    )
    assert requeue is True
    assert calls == [1]


def test_handle_failure_generic_exception(app):
    instance = app()
    task = TranslationTask(Path("a"), "nl", "1")
    instance.translator_available.set()
    requeue = worker_module.handle_failure(
        instance, task, RuntimeError("boom"), "worker", None
    )
    assert requeue is False
