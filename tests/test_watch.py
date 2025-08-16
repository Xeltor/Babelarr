import logging

from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileMovedEvent

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
    event = FileCreatedEvent(str(path))
    handler.on_created(event)

    assert called["path"] == path


def test_srt_handler_enqueue_modified(monkeypatch, tmp_path, app):
    path = tmp_path / "sample.en.srt"
    path.write_text("example")
    (tmp_path / "sample.en.nl.srt").write_text("old")

    called = {}

    app_instance = app()

    def fake_enqueue(p):
        called["path"] = p

    monkeypatch.setattr(app_instance, "enqueue", fake_enqueue)

    handler = SrtHandler(app_instance)
    event = FileModifiedEvent(str(path))
    handler.on_modified(event)

    assert called["path"] == path
    assert not (tmp_path / "sample.en.nl.srt").exists()


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
    event = FileMovedEvent(str(src), str(dest))
    handler.on_moved(event)

    assert called["path"] == dest


def test_srt_handler_debounce(monkeypatch, tmp_path, app):
    path = tmp_path / "partial.en.srt"
    path.write_text("part1")

    called = {}
    app_instance = app()
    app_instance.config.debounce = 0.01

    def fake_enqueue(p):
        called["path"] = p

    monkeypatch.setattr(app_instance, "enqueue", fake_enqueue)

    handler = SrtHandler(app_instance)

    import threading
    import time

    def append_later():
        time.sleep(0.005)
        with path.open("a") as fh:
            fh.write("part2")

    threading.Thread(target=append_later).start()
    event = FileCreatedEvent(str(path))
    handler.on_created(event)

    assert called["path"] == path
    assert path.read_text() == "part1part2"


def test_srt_handler_timeout(monkeypatch, tmp_path, app, caplog):
    path = tmp_path / "waiting.en.srt"
    path.write_text("initial")

    called = {}
    app_instance = app()
    app_instance.config.debounce = 0.01

    def fake_enqueue(p):
        called["path"] = p

    monkeypatch.setattr(app_instance, "enqueue", fake_enqueue)

    handler = SrtHandler(app_instance)
    handler._max_wait = 0.05

    from itertools import count
    from types import SimpleNamespace

    sizes = count(1)

    def fake_stat(self):
        return SimpleNamespace(st_size=next(sizes))

    monkeypatch.setattr(type(path), "stat", fake_stat)

    event = FileCreatedEvent(str(path))
    with caplog.at_level(logging.WARNING):
        handler.on_created(event)

    assert "path" not in called
    assert "timeout" in caplog.text.lower()


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
