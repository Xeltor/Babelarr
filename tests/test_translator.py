import asyncio
import threading

import pytest
import requests

from babelarr.libretranslate_api import LibreTranslateAPI
from babelarr.translator import LibreTranslateClient


def test_translate_file_thread_safety(monkeypatch, tmp_path):
    tmp_file = tmp_path / "a.srt"
    tmp_file.write_text("dummy")

    sessions: dict[int, requests.Session] = {}
    lock = threading.Lock()

    def fake_post(self, url, *, files=None, data=None, timeout=900):
        assert timeout == 900
        with lock:
            sessions[id(threading.current_thread())] = self
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"ok"
        return resp

    monkeypatch.setattr(requests.Session, "post", fake_post)

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
    assert len({id(s) for s in sessions.values()}) == 5

    asyncio.run(api.close())


def _prepared_client():
    client = LibreTranslateClient("http://example", "en")
    client.languages = {"en": {"nl"}}
    client.supported_targets = {"nl"}
    return client


def test_translate_success(monkeypatch, tmp_path):
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("dummy")

    client = _prepared_client()

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


def test_translate_error(monkeypatch, tmp_path):
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("dummy")

    client = _prepared_client()

    def fake_translate_file(path, src, dst, api_key):
        resp = requests.Response()
        resp.status_code = 400
        resp._content = b'{"error": "boom"}'
        return resp

    monkeypatch.setattr(client.api, "translate_file", fake_translate_file)

    with pytest.raises(requests.HTTPError):
        client.translate(tmp_file, "nl")
    client.close()


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
