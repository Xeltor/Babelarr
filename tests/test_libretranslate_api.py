import pytest
import requests

from babelarr.libretranslate_api import LibreTranslateAPI


def test_fetch_languages(monkeypatch):
    calls: list[dict | None] = []

    def fake_get(url, *, timeout, headers=None):
        assert url == "http://only/languages"
        assert timeout == 180
        calls.append(headers)
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"[]"
        return resp

    monkeypatch.setattr(requests, "get", fake_get)

    api = LibreTranslateAPI("http://only")
    languages = api.fetch_languages()

    assert languages == []
    assert calls == [{"Connection": "close"}]

    api.close()


def test_fetch_languages_error(monkeypatch):
    def fake_get(url, *, timeout, headers=None):
        assert timeout == 180
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(requests, "get", fake_get)

    api = LibreTranslateAPI("http://only")

    with pytest.raises(requests.ConnectionError):
        api.fetch_languages()

    api.close()


def test_fetch_languages_persistent_session(monkeypatch):
    sessions = []

    def fake_get(self, url, *, timeout):
        assert url == "http://only/languages"
        assert timeout == 180
        sessions.append(id(self))
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"[]"
        return resp

    monkeypatch.setattr(requests.Session, "get", fake_get)

    api = LibreTranslateAPI("http://only", persistent_session=True)
    api.fetch_languages()
    api.fetch_languages()

    assert len(set(sessions)) == 1

    api.close()


def test_translate_file_error(monkeypatch, tmp_path):
    tmp_file = tmp_path / "a.srt"
    tmp_file.write_text("dummy")

    def fake_post(url, *, files=None, data=None, timeout, headers=None):
        assert timeout == 3600
        raise requests.ConnectionError("fail")

    monkeypatch.setattr(requests, "post", fake_post)

    api = LibreTranslateAPI("http://only")

    with pytest.raises(requests.ConnectionError):
        api.translate_file(tmp_file, "en", "nl")

    api.close()


def test_translate_file(monkeypatch, tmp_path):
    tmp_file = tmp_path / "b.srt"
    tmp_file.write_text("dummy")
    headers_seen: list[dict | None] = []

    def fake_post(url, *, files=None, data=None, timeout, headers=None):
        headers_seen.append(headers)
        assert timeout == 3600
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"ok"
        return resp

    monkeypatch.setattr(requests, "post", fake_post)

    api = LibreTranslateAPI("http://only")

    import threading

    def worker():
        api.translate_file(tmp_file, "en", "nl")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert headers_seen == [{"Connection": "close"}, {"Connection": "close"}]

    api.close()


def test_download_uses_connection_close(monkeypatch):
    calls: list[dict | None] = []

    def fake_get(url, *, timeout, headers=None):
        assert timeout == 180
        calls.append(headers)
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"data"
        return resp

    monkeypatch.setattr(requests, "get", fake_get)

    api = LibreTranslateAPI("http://only")
    resp = api.download("http://only/file")
    assert resp.content == b"data"
    assert calls == [{"Connection": "close"}]

    api.close()


def test_detect_uses_connection_close(monkeypatch):
    calls: list[dict | None] = []

    def fake_post(url, *, data=None, timeout, headers=None):
        assert url == "http://only/detect"
        assert timeout == 180
        assert data == {"q": "hello"}
        calls.append(headers)
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"[]"
        return resp

    monkeypatch.setattr(requests, "post", fake_post)

    api = LibreTranslateAPI("http://only")
    resp = api.detect("hello")
    assert resp.status_code == 200
    assert calls == [{"Connection": "close"}]

    api.close()


def test_translate_file_persistent_session(monkeypatch, tmp_path):
    tmp_file = tmp_path / "c.srt"
    tmp_file.write_text("dummy")

    sessions = []

    def fake_post(self, url, *, files=None, data=None, timeout, headers=None):
        assert timeout == 3600
        sessions.append(id(self))
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"ok"
        return resp

    monkeypatch.setattr(requests.Session, "post", fake_post)

    api = LibreTranslateAPI("http://only", persistent_session=True)
    api.translate_file(tmp_file, "en", "nl")
    api.translate_file(tmp_file, "en", "nl")

    assert len(set(sessions)) == 1

    api.close()


def test_detect_persistent_session(monkeypatch):
    sessions = []

    def fake_post(self, url, *, data=None, timeout, headers=None):
        assert url == "http://only/detect"
        assert timeout == 180
        sessions.append(id(self))
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"[]"
        return resp

    monkeypatch.setattr(requests.Session, "post", fake_post)

    api = LibreTranslateAPI("http://only", persistent_session=True)
    api.detect("hello")
    api.detect("world")

    assert len(set(sessions)) == 1

    api.close()
