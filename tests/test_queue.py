import threading

from babelarr.app import Application
from babelarr.config import Config


class DummyTranslator:
    def translate(self, path, lang):
        return b""


def make_config(tmp_path, db_path, src_ext):
    return Config(
        root_dirs=[str(tmp_path)],
        target_langs=["nl"],
        src_ext=src_ext,
        api_url="http://example",
        workers=1,
        queue_db=str(db_path),
        retry_count=2,
        backoff_delay=0,
    )


def test_enqueue_and_worker(tmp_path, monkeypatch):
    db_path = tmp_path / "queue.db"
    sub_file = tmp_path / "video.en.srt"
    sub_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    app = Application(make_config(tmp_path, db_path, ".srt"), DummyTranslator())

    def fake_translate_file(src, lang):
        src.with_suffix(f".{lang}.srt").write_text("Hallo")

    monkeypatch.setattr(app, "translate_file", fake_translate_file)

    app.enqueue(sub_file)
    worker = threading.Thread(target=app.worker)
    worker.start()
    app.tasks.join()
    app.shutdown_event.set()
    worker.join(timeout=3)

    assert sub_file.with_suffix(".nl.srt").read_text() == "Hallo"
    rows = app.db.all()
    assert rows == []

    app.db.close()


def test_enqueue_skips_when_translated(tmp_path):
    db_path = tmp_path / "queue.db"
    sub_file = tmp_path / "video.en.srt"
    sub_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")
    sub_file.with_suffix(".nl.srt").write_text("Hallo")

    app = Application(make_config(tmp_path, db_path, ".en.srt"), DummyTranslator())

    app.enqueue(sub_file)

    assert app.tasks.empty()
    rows = app.db.all()
    assert rows == []

    app.db.close()
