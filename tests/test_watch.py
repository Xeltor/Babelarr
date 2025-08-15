from watchdog.events import FileCreatedEvent

from app import Application, SrtHandler
from config import Config


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
    app = Application(config)

    def fake_enqueue(p):
        called['path'] = p

    monkeypatch.setattr(app, "enqueue", fake_enqueue)

    handler = SrtHandler(app)
    event = FileCreatedEvent(str(path))
    handler.on_created(event)

    assert called['path'] == path
    app.db.close()
