import importlib
import threading


def test_enqueue_and_worker(tmp_path, monkeypatch):
    # Prepare temporary database and subtitle file
    db_path = tmp_path / "queue.db"
    sub_file = tmp_path / "video.en.srt"
    sub_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    # Set environment for the module under test
    monkeypatch.setenv("QUEUE_DB", str(db_path))
    monkeypatch.setenv("TARGET_LANGS", "nl")
    monkeypatch.setenv("SRC_EXT", ".srt")

    # Reload module so configuration picks up environment overrides
    import main
    importlib.reload(main)

    # Stub out network translation with a simple file write
    def fake_translate_file(src, lang):
        src.with_suffix(f".{lang}.srt").write_text("Hallo")

    monkeypatch.setattr(main, "translate_file", fake_translate_file)

    # Enqueue the sample file and process the queue
    main.enqueue(sub_file)
    worker = threading.Thread(target=main.worker)
    worker.start()
    main.tasks.join()
    main.shutdown_event.set()
    worker.join(timeout=3)

    # Verify translation saved and task removed from DB
    assert sub_file.with_suffix(".nl.srt").read_text() == "Hallo"
    with main.db_lock:
        rows = main.conn.execute("SELECT path FROM queue").fetchall()
    assert rows == []

    main.conn.close()
