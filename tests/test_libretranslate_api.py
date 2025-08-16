import asyncio

import pytest
import requests

from babelarr.libretranslate_api import LibreTranslateAPI


def test_fetch_languages(monkeypatch):
    def fake_get(self, url, *, timeout=60):
        assert url == "http://only/languages"
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"[]"
        return resp

    monkeypatch.setattr(requests.Session, "get", fake_get)

    api = LibreTranslateAPI("http://only")
    languages = api.fetch_languages()

    assert languages == []

    asyncio.run(api.close())


def test_fetch_languages_error(monkeypatch):
    def fake_get(self, url, *, timeout=60):
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(requests.Session, "get", fake_get)

    api = LibreTranslateAPI("http://only")

    with pytest.raises(requests.ConnectionError):
        api.fetch_languages()

    asyncio.run(api.close())


def test_translate_file_error(monkeypatch, tmp_path):
    tmp_file = tmp_path / "a.srt"
    tmp_file.write_text("dummy")

    def fake_post(self, url, *, files=None, data=None, timeout=60):
        raise requests.ConnectionError("fail")

    monkeypatch.setattr(requests.Session, "post", fake_post)

    api = LibreTranslateAPI("http://only")

    with pytest.raises(requests.ConnectionError):
        api.translate_file(tmp_file, "en", "nl")

    asyncio.run(api.close())


def test_translate_file(monkeypatch, tmp_path):
    tmp_file = tmp_path / "b.srt"
    tmp_file.write_text("dummy")

    calls: list[str] = []

    def fake_post(self, url, *, files=None, data=None, timeout=60):
        calls.append(url)
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"ok"
        return resp

    monkeypatch.setattr(requests.Session, "post", fake_post)

    api = LibreTranslateAPI("http://only")
    resp = api.translate_file(tmp_file, "en", "nl")

    assert calls == ["http://only/translate_file"]
    assert resp.content == b"ok"

    asyncio.run(api.close())
