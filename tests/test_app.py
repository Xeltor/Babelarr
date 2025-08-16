import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests

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
    monkeypatch.setattr(app_instance, "enqueue", lambda p: called.append(p))

    app_instance.full_scan()

    assert sorted(called) == sorted([first, second])


def test_db_persistence_across_restarts(tmp_path, app):
    src = tmp_path / "video.en.srt"
    src.write_text("hello")

    app1 = app()
    app1.enqueue(src)

    app2 = app()
    app2.load_pending()
    restored = app2.tasks.get_nowait()
    assert restored == src


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

        with caplog.at_level(logging.WARNING):
            app_instance.enqueue(src)
            app_instance.tasks.join()
            assert "disappeared" in caplog.text

        app_instance.shutdown_event.set()

    assert not app_instance.output_path(src, "nl").exists()
    assert app_instance.db.all() == []


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
    instance.shutdown_event.set()
    instance.run()

    assert called["interval"] == 5
    assert called["func"].__self__ is instance
    assert called["func"].__func__ is instance.full_scan.__func__


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
