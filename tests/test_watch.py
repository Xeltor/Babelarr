import logging

from watchdog import events

import babelarr.app as app_module
from babelarr.app import SrtHandler


def test_srt_handler_enqueue(monkeypatch, tmp_path, app):
    path = tmp_path / "sample.en.srt"
    path.write_text("example")

    called = {}

    app_instance = app()

    def fake_enqueue(p):
        called["path"] = p

    monkeypatch.setattr(app_instance, "enqueue", fake_enqueue)

    handler = SrtHandler(app_instance)
    event = events.FileClosedEvent(str(path))
    handler.on_closed(event)

    assert called["path"] == path


def test_srt_handler_enqueue_modified(monkeypatch, tmp_path, app):
    path = tmp_path / "sample.en.srt"
    path.write_text("example")
    app_instance = app()
    app_instance.output_path(path, "nl").write_text("old")

    called = {}

    def fake_enqueue(p):
        called["path"] = p

    monkeypatch.setattr(app_instance, "enqueue", fake_enqueue)

    handler = SrtHandler(app_instance)
    event = events.FileClosedEvent(str(path))
    handler.on_closed(event)

    assert called["path"] == path
    assert not app_instance.output_path(path, "nl").exists()


def test_srt_handler_ignore_closed_nowrite(monkeypatch, tmp_path, app):
    path = tmp_path / "sample.en.srt"
    path.write_text("example")

    app_instance = app()
    called = {}

    def fake_enqueue(p):
        called["path"] = p

    monkeypatch.setattr(app_instance, "enqueue", fake_enqueue)

    handler = SrtHandler(app_instance)
    event = events.FileClosedNoWriteEvent(str(path))
    handler.on_closed(event)

    assert "path" not in called


def test_srt_handler_enqueue_moved(monkeypatch, tmp_path, app):
    src = tmp_path / "old.en.srt"
    dest = tmp_path / "new.en.srt"
    dest.write_text("example")

    called = {}

    app_instance = app()

    def fake_enqueue(p):
        called["path"] = p

    monkeypatch.setattr(app_instance, "enqueue", fake_enqueue)

    handler = SrtHandler(app_instance)
    event = events.FileMovedEvent(str(src), str(dest))
    handler.on_moved(event)

    assert called["path"] == dest


def test_watch_lifecycle(monkeypatch, tmp_path, app):
    app_instance = app()

    events = {"start": False, "stop": False, "join": False, "scheduled": []}

    class FakeObserver:
        def schedule(self, handler, path, recursive):
            events["scheduled"].append((path, recursive))

        def start(self):
            events["start"] = True

        def stop(self):
            events["stop"] = True

        def join(self):
            events["join"] = True

    monkeypatch.setattr(app_module, "Observer", FakeObserver)

    def fake_sleep(seconds):
        app_instance.shutdown_event.set()

    monkeypatch.setattr(app_module.time, "sleep", fake_sleep)

    app_instance.watch()

    assert events["start"] and events["stop"] and events["join"]
    assert events["scheduled"] == [(str(tmp_path), True)]


def test_watch_missing_directory(monkeypatch, tmp_path, app, caplog):
    existing = tmp_path / "existing"
    existing.mkdir()
    missing = tmp_path / "missing"

    cfg = app_module.Config(
        root_dirs=[str(existing), str(missing)],
        target_langs=["nl"],
        src_lang="en",
        src_ext=".en.srt",
        api_url="http://example",
        workers=1,
        queue_db=str(tmp_path / "queue.db"),
        retry_count=2,
        backoff_delay=0,
    )

    app_instance = app(cfg=cfg)
    app_instance.shutdown_event.set()

    events = {"scheduled": []}

    class FakeObserver:
        def schedule(self, handler, path, recursive):
            events["scheduled"].append(path)

        def start(self):
            pass

        def stop(self):
            pass

        def join(self):
            pass

    monkeypatch.setattr(app_module, "Observer", FakeObserver)

    with caplog.at_level(logging.WARNING):
        app_instance.watch()

    assert events["scheduled"] == [str(existing)]
    assert str(missing) in caplog.text
