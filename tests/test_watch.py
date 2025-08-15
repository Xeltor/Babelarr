from watchdog.events import FileCreatedEvent

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
