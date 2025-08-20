import logging
import threading
from pathlib import Path

import pytest
from watchdog.events import (
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

import babelarr.watch as watch_module
from babelarr.config import Config
from babelarr.watch import SrtHandler

pytestmark = pytest.mark.integration


def test_srt_handler_patterns(app):
    app_instance = app()
    handler = SrtHandler(app_instance)
    assert handler.patterns == [f"*{app_instance.config.src_ext}"]
    assert handler.ignore_directories


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
    handler.dispatch(event)

    assert called["path"] == path


def test_srt_handler_ignore_modified(monkeypatch, tmp_path, app):
    path = tmp_path / "sample.en.srt"
    path.write_text("example")
    app_instance = app()
    output = app_instance.output_path(path, "nl")
    output.write_text("old")

    called = {}

    def fake_enqueue(p):
        called["path"] = p

    monkeypatch.setattr(app_instance, "enqueue", fake_enqueue)

    handler = SrtHandler(app_instance)
    event = FileModifiedEvent(str(path))
    handler.dispatch(event)

    assert "path" not in called
    assert output.exists() and output.read_text() == "old"


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
    handler.dispatch(event)

    assert called["path"] == dest


def test_srt_handler_rapid_events(monkeypatch, tmp_path, app):
    path = tmp_path / "fast.en.srt"
    path.write_text("example")

    calls: list[Path] = []

    app_instance = app()

    def fake_enqueue(p):
        calls.append(p)

    monkeypatch.setattr(app_instance, "enqueue", fake_enqueue)

    handler = SrtHandler(app_instance)
    monkeypatch.setattr(handler, "_wait_for_complete", lambda p: True)

    event = FileCreatedEvent(str(path))
    handler.dispatch(event)
    handler.dispatch(event)

    assert calls == [path]


def test_srt_handler_prunes_periodically(monkeypatch, tmp_path, app):
    path = tmp_path / "again.en.srt"
    path.write_text("example")

    calls: list[Path] = []

    app_instance = app()
    app_instance.config.debounce = 0.01

    monkeypatch.setattr(app_instance, "enqueue", lambda p: calls.append(p))

    handler = SrtHandler(app_instance)
    monkeypatch.setattr(handler, "_wait_for_complete", lambda p: True)

    times = iter([0.02, 0.025, 0.04])
    monkeypatch.setattr(watch_module.time, "monotonic", lambda: next(times))

    event = FileCreatedEvent(str(path))

    handler.dispatch(event)
    first = handler._last_prune
    handler.dispatch(event)
    second = handler._last_prune
    handler.dispatch(event)

    assert first == 0.02
    assert second == 0.02
    assert handler._last_prune == 0.04
    assert calls == [path, path]


def test_srt_handler_delete_removes_from_queue(tmp_path, app):
    path = tmp_path / "sample.en.srt"
    path.write_text("example")

    class SlowTranslator:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()

        def translate(self, path, lang):
            self.started.set()
            self.release.wait()
            return b""

        def wait_until_available(self):
            return None

    translator = SlowTranslator()
    app_instance = app(translator=translator)
    app_instance.enqueue(path)
    translator.started.wait()
    assert app_instance.db.all() == [(path, "nl", 0)]

    path.unlink()
    handler = SrtHandler(app_instance)
    event = FileDeletedEvent(str(path))
    handler.dispatch(event)

    translator.release.set()
    app_instance.tasks.join()
    assert app_instance.db.all() == []


def test_srt_handler_debounce(monkeypatch, tmp_path, app):
    path = tmp_path / "partial.en.srt"
    path.write_text("part1")

    called = {}
    app_instance = app()
    app_instance.config.debounce = 0.01

    def fake_enqueue(p):
        called["path"] = p
        called["content"] = p.read_text()

    monkeypatch.setattr(app_instance, "enqueue", fake_enqueue)

    handler = SrtHandler(app_instance)

    appended = False

    def fake_sleep(seconds):
        nonlocal appended
        if not appended:
            with path.open("a") as fh:
                fh.write("part2")
            appended = True

    monkeypatch.setattr(watch_module.time, "sleep", fake_sleep)

    event = FileCreatedEvent(str(path))
    handler.dispatch(event)

    assert called["path"] == path
    assert called["content"] == "part1part2"


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
        handler.dispatch(event)

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

    monkeypatch.setattr(watch_module, "Observer", FakeObserver)

    def fake_sleep(seconds):
        app_instance.shutdown_event.set()

    monkeypatch.setattr(watch_module.time, "sleep", fake_sleep)

    watch_module.watch(app_instance)

    assert events["start"] and events["stop"] and events["join"]
    assert events["scheduled"] == [(str(tmp_path), True)]


def test_watch_missing_directory(monkeypatch, tmp_path, app, caplog):
    existing = tmp_path / "existing"
    existing.mkdir()
    missing = tmp_path / "missing"

    cfg = Config(
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

    monkeypatch.setattr(watch_module, "Observer", FakeObserver)

    with caplog.at_level(logging.WARNING):
        watch_module.watch(app_instance)

    assert events["scheduled"] == [str(existing)]
    assert missing.name in caplog.text
