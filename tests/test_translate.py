import logging

import pytest
import requests

from babelarr.translator import LibreTranslateClient


class DummyTranslator:
    result = b"1\n00:00:00,000 --> 00:00:02,000\nHallo\n"

    def translate(self, path, lang):
        return self.result

    def close(self):
        pass


def test_translate_file(tmp_path, app):
    # Create a dummy English subtitle file
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    translator = DummyTranslator()
    app_instance = app(translator=translator)

    app_instance.translate_file(tmp_file, "nl")
    output_file = app_instance.output_path(tmp_file, "nl")
    assert output_file.exists()
    assert output_file.read_bytes() == translator.result


@pytest.mark.parametrize("status", [400, 403, 404, 429, 500])
def test_translate_file_errors(tmp_path, status, caplog, app):
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
    app_instance = app(translator=translator)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(requests.HTTPError):
            app_instance.translate_file(tmp_file, "nl")
        assert str(status) in caplog.text


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

    monkeypatch.setattr(requests.Session, "post", fake_post)

    translator = LibreTranslateClient(
        "http://example", "en", retry_count=3, backoff_delay=0
    )

    with caplog.at_level(logging.WARNING):
        result = translator.translate(tmp_file, "nl")
    translator.close()

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

    monkeypatch.setattr(requests.Session, "post", fake_post)

    translator = LibreTranslateClient(
        "http://example", "en", retry_count=2, backoff_delay=0
    )

    with caplog.at_level(logging.ERROR):
        with pytest.raises(requests.ConnectionError):
            translator.translate(tmp_file, "nl")
    translator.close()

    assert attempts["count"] == 2
    assert "failed after 2 attempts" in caplog.text


def test_api_key_included(monkeypatch, tmp_path):
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    captured: dict[str, dict | None] = {"data": None}

    def fake_post(self, url, *, files=None, data=None, timeout=60):
        captured["data"] = data
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"ok"
        return resp

    monkeypatch.setattr(requests.Session, "post", fake_post)

    translator = LibreTranslateClient(
        "http://example",
        "en",
        retry_count=1,
        backoff_delay=0,
        api_key="secret",
    )

    result = translator.translate(tmp_file, "nl")
    translator.close()

    assert result == b"ok"
    assert captured["data"]["api_key"] == "secret"


def test_src_lang_included(monkeypatch, tmp_path):
    tmp_file = tmp_path / "sample.xx.srt"
    tmp_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    captured: dict[str, dict | None] = {"data": None}

    def fake_post(self, url, *, files=None, data=None, timeout=60):
        captured["data"] = data
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"ok"
        return resp

    monkeypatch.setattr(requests.Session, "post", fake_post)

    translator = LibreTranslateClient(
        "http://example",
        "xx",
        retry_count=1,
        backoff_delay=0,
    )

    result = translator.translate(tmp_file, "nl")
    translator.close()

    assert result == b"ok"
    assert captured["data"]["source"] == "xx"


def test_download_translated_file(monkeypatch, tmp_path):
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    def fake_post(self, url, *, files=None, data=None, timeout=60):
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b'{"translatedFileUrl": "http://example/translated.srt"}'
        return resp

    downloaded = {"url": None}

    def fake_get(self, url, *, timeout=60):
        downloaded["url"] = url
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"translated"
        return resp

    monkeypatch.setattr(requests.Session, "post", fake_post)
    monkeypatch.setattr(requests.Session, "get", fake_get)

    translator = LibreTranslateClient(
        "http://example", "en", retry_count=1, backoff_delay=0
    )

    result = translator.translate(tmp_file, "nl")
    translator.close()

    assert downloaded["url"] == "http://example/translated.srt"
    assert result == b"translated"
