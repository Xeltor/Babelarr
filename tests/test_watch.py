import main
from watchdog.events import FileCreatedEvent


def test_srt_handler_enqueue(monkeypatch, tmp_path):
    path = tmp_path / "sample.en.srt"
    path.write_text("example")

    called = {}

    def fake_enqueue(p):
        called['path'] = p

    monkeypatch.setattr(main, "enqueue", fake_enqueue)

    handler = main.SrtHandler()
    event = FileCreatedEvent(str(path))
    handler.on_created(event)

    assert called['path'] == path
