import logging
from concurrent.futures import ThreadPoolExecutor

import requests


def test_full_scan(tmp_path, monkeypatch, app):
    first = tmp_path / "one.en.srt"
    first.write_text("a")
    subdir = tmp_path / "sub"
    subdir.mkdir()
    second = subdir / "two.en.srt"
    second.write_text("b")

    app_instance = app()
    called = []
    monkeypatch.setattr(app_instance, "enqueue", lambda p: called.append(p))

    app_instance.full_scan()

    assert sorted(called) == sorted([first, second])


def test_db_persistence_across_restarts(tmp_path, app):
    src = tmp_path / "video.en.srt"
    src.write_text("hello")

    app1 = app()
    app1.enqueue(src)

    app2 = app()
    app2.load_pending()
    restored = app2.tasks.get_nowait()
    assert restored == src


def test_worker_retry_on_network_failure(tmp_path, caplog, app):
    src = tmp_path / "fail.en.srt"
    src.write_text("hello")

    class UnstableTranslator:
        def __init__(self):
            self.attempts = 0

        def translate(self, path, lang):
            self.attempts += 1
            if self.attempts == 1:
                raise requests.ConnectionError("boom")
            return b"ok"

    translator = UnstableTranslator()
    app_instance = app(translator=translator)

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(app_instance.worker)

        with caplog.at_level(logging.ERROR):
            app_instance.enqueue(src)
            app_instance.tasks.join()
            assert any("translation failed" in r.message for r in caplog.records)

        app_instance.enqueue(src)
        app_instance.tasks.join()
        app_instance.shutdown_event.set()

    assert src.with_suffix(".nl.srt").exists()
