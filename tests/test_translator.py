import logging
import threading

import pytest
import requests

from babelarr.libretranslate_api import LibreTranslateAPI
from babelarr.translator import LibreTranslateClient


def test_translate_file_thread_safety(monkeypatch, tmp_path):
    tmp_file = tmp_path / "a.srt"
    tmp_file.write_text("dummy")

    calls: dict[int, dict | None] = {}
    lock = threading.Lock()

    def fake_post(url, *, files=None, data=None, timeout=900, headers=None):
        assert timeout == 900
        with lock:
            calls[id(threading.current_thread())] = headers
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"ok"
        return resp

    monkeypatch.setattr(requests, "post", fake_post)

    api = LibreTranslateAPI("http://only")

    results: list[bytes] = []
    errors: list[Exception] = []

    def worker():
        try:
            resp = api.translate_file(tmp_file, "en", "nl")
            results.append(resp.content)
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert results == [b"ok"] * 5
    assert len(calls) == 5
    assert all(h == {"Connection": "close"} for h in calls.values())

    api.close()


def test_is_available_uses_http_timeout(monkeypatch):
    client = LibreTranslateClient("http://only", "en")

    class DummyResp:
        status_code = 200

    def fake_head(self, url, *, timeout):
        assert timeout == 30
        return DummyResp()

    monkeypatch.setattr(requests.Session, "head", fake_head)

    assert client.is_available() is True
    client.api.close()


def _prepared_client():
    client = LibreTranslateClient("http://example", "en")
    client.languages = {"en": {"nl"}}
    client.supported_targets = {"nl"}
    return client


def test_translate_success(monkeypatch, tmp_path):
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("dummy")

    client = _prepared_client()
    client.retry_count = 1

    def fake_translate_file(path, src, dst, api_key):
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"ok"
        return resp

    monkeypatch.setattr(client.api, "translate_file", fake_translate_file)
    called = {"download": False}

    def fake_download(url):
        called["download"] = True
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"translated"
        return resp

    monkeypatch.setattr(client.api, "download", fake_download)

    result = client.translate(tmp_file, "nl")
    client.close()

    assert result == b"ok"
    assert called["download"] is False


def test_translate_error(monkeypatch, tmp_path, caplog):
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("dummy")

    client = _prepared_client()

    def fake_translate_file(path, src, dst, api_key):
        resp = requests.Response()
        resp.status_code = 400
        resp._content = b'{"error": "boom"}'
        resp.headers = {"X-Test": "1"}
        return resp

    monkeypatch.setattr(client.api, "translate_file", fake_translate_file)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(requests.HTTPError):
            client.translate(tmp_file, "nl")

    client.close()

    error_logs = [r for r in caplog.records if "status=400" in r.getMessage()]
    assert error_logs
    msg = error_logs[0].getMessage()
    assert "http_error context=LibreTranslate" in msg
    assert "status=400" in msg
    assert "detail=Bad Request: boom" in msg
    assert "headers={'X-Test': '1'}" in msg
    assert 'body={"error": "boom"}' in msg


def test_translate_download(monkeypatch, tmp_path):
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("dummy")

    client = _prepared_client()

    def fake_translate_file(path, src, dst, api_key):
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b'{"translatedFileUrl": "http://download"}'
        return resp

    def fake_download(url):
        assert url == "http://download"
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"translated"
        return resp

    monkeypatch.setattr(client.api, "translate_file", fake_translate_file)
    monkeypatch.setattr(client.api, "download", fake_download)

    result = client.translate(tmp_file, "nl")
    client.close()

    assert result == b"translated"


def test_ensure_languages_logs_count(monkeypatch, caplog):
    client = LibreTranslateClient("http://example", "en")
    monkeypatch.setattr(
        client.api,
        "fetch_languages",
        lambda: [{"code": "en", "targets": ["nl", "es"]}],
    )
    with caplog.at_level(logging.INFO):
        client.ensure_languages()
    assert "languages_loaded count=2" in caplog.text


def test_wait_until_available_logs_service_available(monkeypatch, caplog):
    client = LibreTranslateClient(
        "http://example", "en", availability_check_interval=0.1
    )
    monkeypatch.setattr(client, "is_available", lambda: True)
    monkeypatch.setattr(client, "ensure_languages", lambda: None)
    with caplog.at_level(logging.INFO):
        client.wait_until_available()
    assert "service_available" in caplog.text
