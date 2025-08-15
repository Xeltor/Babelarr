import logging

import pytest
import requests

from babelarr.app import Application
from babelarr.config import Config
from babelarr.translator import LibreTranslateClient


class DummyTranslator:
    result = b"1\n00:00:00,000 --> 00:00:02,000\nHallo\n"

    def translate(self, path, lang):
        return self.result


def test_translate_file(tmp_path):
    # Create a dummy English subtitle file
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    translator = DummyTranslator()
    app = Application(
        Config(
            root_dirs=[str(tmp_path)],
            target_langs=["nl"],
            src_ext=".en.srt",
            api_url="http://example",
            workers=1,
            queue_db=str(tmp_path / "queue.db"),
            retry_count=2,
            backoff_delay=0,
        ),
        translator,
    )

    app.translate_file(tmp_file, "nl")
    output_file = tmp_file.with_suffix(".nl.srt")
    assert output_file.exists()
    assert output_file.read_bytes() == translator.result
    app.db.close()


@pytest.mark.parametrize("status", [400, 403, 404, 429, 500])
def test_translate_file_errors(tmp_path, status, caplog):
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    class ErrorTranslator:
        def __init__(self, status_code):
            self.status_code = status_code

        def translate(self, path, lang):
            logger = logging.getLogger("babelarr")
            logger.error("HTTP %s from LibreTranslate: boom", self.status_code)
            resp = requests.Response()
            resp.status_code = self.status_code
            raise requests.HTTPError(response=resp)

    translator = ErrorTranslator(status)
    app = Application(
        Config(
            root_dirs=[str(tmp_path)],
            target_langs=["nl"],
            src_ext=".en.srt",
            api_url="http://example",
            workers=1,
            queue_db=str(tmp_path / "queue.db"),
            retry_count=2,
            backoff_delay=0,
        ),
        translator,
    )
    try:
        with caplog.at_level(logging.ERROR):
            with pytest.raises(requests.HTTPError):
                app.translate_file(tmp_file, "nl")
            assert str(status) in caplog.text
    finally:
        app.db.close()


def test_retry_success(monkeypatch, tmp_path, caplog):
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    attempts = {"count": 0}

    def fake_post(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise requests.ConnectionError("boom")
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"ok"
        return resp

    monkeypatch.setattr(requests, "post", fake_post)

    translator = LibreTranslateClient("http://example", retry_count=3, backoff_delay=0)

    with caplog.at_level(logging.WARNING):
        result = translator.translate(tmp_file, "nl")

    assert result == b"ok"
    assert attempts["count"] == 3
    retry_logs = [r for r in caplog.records if "Attempt" in r.message]
    assert len(retry_logs) == 2


def test_retry_exhaustion(monkeypatch, tmp_path, caplog):
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    attempts = {"count": 0}

    def fake_post(*args, **kwargs):
        attempts["count"] += 1
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(requests, "post", fake_post)

    translator = LibreTranslateClient("http://example", retry_count=2, backoff_delay=0)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(requests.ConnectionError):
            translator.translate(tmp_file, "nl")

    assert attempts["count"] == 2
    assert "failed after 2 attempts" in caplog.text
