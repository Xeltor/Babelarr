import threading


def test_enqueue_and_worker(tmp_path, monkeypatch, app, config):
    sub_file = tmp_path / "video.en.srt"
    sub_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    config.src_ext = ".srt"
    app_instance = app(cfg=config)

    def fake_translate_file(src, lang):
        src.with_suffix(f".{lang}.srt").write_text("Hallo")

    monkeypatch.setattr(app_instance, "translate_file", fake_translate_file)

    app_instance.enqueue(sub_file)
    worker = threading.Thread(target=app_instance.worker)
    worker.start()
    app_instance.tasks.join()
    app_instance.shutdown_event.set()
    worker.join(timeout=3)

    assert sub_file.with_suffix(".nl.srt").read_text() == "Hallo"
    rows = app_instance.db.all()
    assert rows == []


def test_enqueue_skips_when_translated(tmp_path, app, config):
    sub_file = tmp_path / "video.en.srt"
    sub_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")
    sub_file.with_suffix(".nl.srt").write_text("Hallo")

    app_instance = app(cfg=config)

    app_instance.enqueue(sub_file)

    assert app_instance.tasks.empty()
    rows = app_instance.db.all()
    assert rows == []
