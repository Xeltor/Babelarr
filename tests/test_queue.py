from concurrent.futures import ThreadPoolExecutor


def test_enqueue_and_worker(tmp_path, monkeypatch, app, config):
    sub_file = tmp_path / "video.en.srt"
    sub_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    config.src_ext = ".srt"
    app_instance = app(cfg=config)

    def fake_translate_file(src, lang):
        app_instance.output_path(src, lang).write_text("Hallo")
        return True

    monkeypatch.setattr(app_instance, "translate_file", fake_translate_file)

    app_instance.enqueue(sub_file)
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(app_instance.worker)
        app_instance.tasks.join()
        app_instance.shutdown_event.set()

    assert app_instance.output_path(sub_file, "nl").read_text() == "Hallo"
    rows = app_instance.db.all()
    assert rows == []


def test_enqueue_uppercase_extension(tmp_path, monkeypatch, app, config):
    sub_file = tmp_path / "video.en.SRT"
    sub_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    config.src_ext = ".srt"
    app_instance = app(cfg=config)

    def fake_translate_file(src, lang):
        app_instance.output_path(src, lang).write_text("Hallo")
        return True

    monkeypatch.setattr(app_instance, "translate_file", fake_translate_file)

    app_instance.enqueue(sub_file)
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(app_instance.worker)
        app_instance.tasks.join()
        app_instance.shutdown_event.set()

    assert app_instance.output_path(sub_file, "nl").read_text() == "Hallo"
    rows = app_instance.db.all()
    assert rows == []


def test_enqueue_skips_when_translated(tmp_path, app, config):
    sub_file = tmp_path / "video.en.srt"
    sub_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    app_instance = app(cfg=config)
    app_instance.output_path(sub_file, "nl").write_text("Hallo")

    app_instance.enqueue(sub_file)

    assert app_instance.tasks.empty()
    rows = app_instance.db.all()
    assert rows == []
