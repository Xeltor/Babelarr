import logging
import threading

import requests

import app
from app import Application
from config import Config


def make_config(tmp_path):
    return Config(
        root_dirs=[str(tmp_path)],
        target_langs=["nl"],
        src_ext=".en.srt",
        api_url="http://example",
        workers=1,
        queue_db=str(tmp_path / "queue.db"),
    )


def test_full_scan(tmp_path, monkeypatch):
    first = tmp_path / "one.en.srt"
    first.write_text("a")
    subdir = tmp_path / "sub"
    subdir.mkdir()
    second = subdir / "two.en.srt"
    second.write_text("b")

    app_instance = Application(make_config(tmp_path))
    called = []
    monkeypatch.setattr(app_instance, "enqueue", lambda p: called.append(p))

    app_instance.full_scan()

    assert sorted(called) == sorted([first, second])
    app_instance.db.close()


def test_db_persistence_across_restarts(tmp_path):
    src = tmp_path / "video.en.srt"
    src.write_text("hello")
    config = make_config(tmp_path)

    app1 = Application(config)
    app1.enqueue(src)
    app1.db.close()

    app2 = Application(config)
    app2.load_pending()
    restored = app2.tasks.get_nowait()
    assert restored == src
    app2.db.close()


def test_worker_retry_on_network_failure(tmp_path, monkeypatch, caplog):
    src = tmp_path / "fail.en.srt"
    src.write_text("hello")
    config = make_config(tmp_path)
    app_instance = Application(config)

    attempts = {"count": 0}

    def fake_post(url, files, data, timeout):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise requests.ConnectionError("boom")

        class Resp:
            status_code = 200
            content = b"ok"

            def raise_for_status(self):
                pass

            headers = {}
            text = ""

        return Resp()

    monkeypatch.setattr(requests, "post", fake_post)

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
