import logging
import threading

import requests

from babelarr.app import Application
from babelarr.config import Config


class DummyTranslator:
    def translate(self, path, lang):
        return b"translated"


def make_config(tmp_path):
    return Config(
        root_dirs=[str(tmp_path)],
        target_langs=["nl"],
        src_ext=".en.srt",
        api_url="http://example",
        workers=1,
        queue_db=str(tmp_path / "queue.db"),
        retry_count=2,
        backoff_delay=0,
    )


def test_full_scan(tmp_path, monkeypatch):
    first = tmp_path / "one.en.srt"
    first.write_text("a")
    subdir = tmp_path / "sub"
    subdir.mkdir()
    second = subdir / "two.en.srt"
    second.write_text("b")

    app_instance = Application(make_config(tmp_path), DummyTranslator())
    called = []
    monkeypatch.setattr(app_instance, "enqueue", lambda p: called.append(p))

    app_instance.full_scan()

    assert sorted(called) == sorted([first, second])
    app_instance.db.close()


def test_db_persistence_across_restarts(tmp_path):
    src = tmp_path / "video.en.srt"
    src.write_text("hello")
    config = make_config(tmp_path)

    app1 = Application(config, DummyTranslator())
    app1.enqueue(src)
    app1.db.close()

    app2 = Application(config, DummyTranslator())
    app2.load_pending()
    restored = app2.tasks.get_nowait()
    assert restored == src
    app2.db.close()


def test_worker_retry_on_network_failure(tmp_path, caplog):
    src = tmp_path / "fail.en.srt"
    src.write_text("hello")
    config = make_config(tmp_path)

    class UnstableTranslator:
        def __init__(self):
            self.attempts = 0

        def translate(self, path, lang):
            self.attempts += 1
            if self.attempts == 1:
                raise requests.ConnectionError("boom")
            return b"ok"

    translator = UnstableTranslator()
    app_instance = Application(config, translator)

    worker = threading.Thread(target=app_instance.worker)
    worker.start()

    with caplog.at_level(logging.ERROR):
        app_instance.enqueue(src)
        app_instance.tasks.join()
        assert any("translation failed" in r.message for r in caplog.records)

    app_instance.enqueue(src)
    app_instance.tasks.join()
    app_instance.shutdown_event.set()
    worker.join()

    assert src.with_suffix(".nl.srt").exists()
    app_instance.db.close()
