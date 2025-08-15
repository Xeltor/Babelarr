from watchdog.events import FileCreatedEvent

import babelarr.app as app
from babelarr.app import Application, SrtHandler
from babelarr.config import Config


class DummyTranslator:
    def translate(self, path, lang):
        return b""


def test_srt_handler_enqueue(monkeypatch, tmp_path):
    path = tmp_path / "sample.en.srt"
    path.write_text("example")

    called = {}

    config = Config(
        root_dirs=[str(tmp_path)],
        target_langs=["nl"],
        src_ext=".en.srt",
        api_url="http://example",
        workers=1,
        queue_db=str(tmp_path / "queue.db"),
    )
    app = Application(config, DummyTranslator())

    def fake_enqueue(p):
        called["path"] = p

    monkeypatch.setattr(app, "enqueue", fake_enqueue)

    handler = SrtHandler(app)
    event = FileCreatedEvent(str(path))
    handler.on_created(event)

    assert called["path"] == path
    app.db.close()


def test_watch_lifecycle(monkeypatch, tmp_path):
    config = Config(
        root_dirs=[str(tmp_path)],
        target_langs=["nl"],
        src_ext=".en.srt",
        api_url="http://example",
        workers=1,
        queue_db=str(tmp_path / "queue.db"),
    )
    app_instance = Application(config, DummyTranslator())

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

    monkeypatch.setattr(app, "Observer", FakeObserver)

    def fake_sleep(seconds):
        app_instance.shutdown_event.set()

    monkeypatch.setattr(app.time, "sleep", fake_sleep)

    app_instance.watch()

    assert events["start"] and events["stop"] and events["join"]
    assert events["scheduled"] == [(str(tmp_path), True)]
    app_instance.db.close()
