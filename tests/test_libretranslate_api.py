from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import requests

from babelarr.libretranslate_api import LibreTranslateAPI

LIVE_LT_URL = "http://192.168.1.200:5000"

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def test_fetch_languages_live() -> None:
    api = LibreTranslateAPI(LIVE_LT_URL)
    languages = api.fetch_languages()
    assert languages, "Expected languages from live LibreTranslate"


def test_translate_and_detect_live(tmp_path: Path) -> None:
    api = LibreTranslateAPI(LIVE_LT_URL)
    source = tmp_path / "live.srt"
    source.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")

    translation = api.translate_file(source, "en", "es", None)
    assert translation.status_code == 200

    detection = api.detect("Hello world")
    assert detection.status_code == 200

    api.close()


def test_fetch_languages_error(monkeypatch: MonkeyPatch) -> None:
    def fake_get(
        url: str, *, timeout: int, headers: dict[str, str] | None = None
    ) -> requests.Response:
        assert timeout == 180
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(requests, "get", fake_get)

    api = LibreTranslateAPI("http://only")

    with pytest.raises(requests.ConnectionError):
        api.fetch_languages()

    api.close()


def test_fetch_languages_persistent_session(monkeypatch: MonkeyPatch) -> None:
    sessions: list[int] = []

    def fake_get(
        self: requests.Session, url: str, *, timeout: int
    ) -> requests.Response:
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


def test_translate_file_error(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    tmp_file = tmp_path / "a.srt"
    tmp_file.write_text("dummy")

    def fake_post(
        url: str,
        *,
        files: dict[str, object] | None = None,
        data: dict[str, object] | None = None,
        timeout: int,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        assert timeout == 3600
        raise requests.ConnectionError("fail")

    monkeypatch.setattr(requests, "post", fake_post)

    api = LibreTranslateAPI("http://only")

    with pytest.raises(requests.ConnectionError):
        api.translate_file(tmp_file, "en", "nl")

    api.close()


def test_translate_file(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    tmp_file = tmp_path / "b.srt"
    tmp_file.write_text("dummy")
    headers_seen: list[dict[str, str] | None] = []

    def fake_post(
        url: str,
        *,
        files: dict[str, object] | None = None,
        data: dict[str, object] | None = None,
        timeout: int,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        headers_seen.append(headers)
        assert timeout == 3600
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"ok"
        return resp

    monkeypatch.setattr(requests, "post", fake_post)

    api = LibreTranslateAPI("http://only")

    import threading

    def worker() -> None:
        api.translate_file(tmp_file, "en", "nl")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert headers_seen == [{"Connection": "close"}, {"Connection": "close"}]

    api.close()


def test_download_uses_connection_close(monkeypatch: MonkeyPatch) -> None:
    calls: list[dict[str, str] | None] = []

    def fake_get(
        url: str, *, timeout: int, headers: dict[str, str] | None = None
    ) -> requests.Response:
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


def test_detect_uses_connection_close(monkeypatch: MonkeyPatch) -> None:
    calls: list[dict[str, str] | None] = []

    def fake_post(
        url: str,
        *,
        data: dict[str, object] | None = None,
        timeout: int,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
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


def test_translate_file_persistent_session(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    tmp_file = tmp_path / "c.srt"
    tmp_file.write_text("dummy")

    sessions: list[int] = []

    def fake_post(
        self: requests.Session,
        url: str,
        *,
        files: dict[str, object] | None = None,
        data: dict[str, object] | None = None,
        timeout: int,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
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


def test_detect_persistent_session(monkeypatch: MonkeyPatch) -> None:
    sessions: list[int] = []

    def fake_post(
        self: requests.Session,
        url: str,
        *,
        data: dict[str, object] | None = None,
        timeout: int,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
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
