from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import requests

import babelarr.translator as translator
from babelarr.translator import LibreTranslateClient

if TYPE_CHECKING:
    from pytest import LogCaptureFixture, MonkeyPatch

    from babelarr.app import Application

pytest.skip(
    "Queue-based translation tests are obsolete under the MKV-first pipeline",
    allow_module_level=True,
)


class DummyTranslator:
    result = b"1\n00:00:00,000 --> 00:00:02,000\nHallo\n"

    def translate(self, path: Path, lang: str) -> bytes:
        return self.result

    def close(self) -> None:
        pass

    def wait_until_available(self) -> None:
        return None


def test_translate_file(tmp_path: Path, app: Callable[..., Application]) -> None:
    # Create a dummy English subtitle file
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    translator = DummyTranslator()
    app_instance = app(translator=translator)

    assert app_instance.translate_file(tmp_file, "nl") is True
    output_file = app_instance.output_path(tmp_file, "nl")
    assert output_file.exists()
    assert output_file.read_bytes() == translator.result


@pytest.mark.parametrize("status", [400, 403, 404, 429, 500])
def test_translate_file_errors(
    tmp_path: Path,
    status: int,
    caplog: LogCaptureFixture,
    app: Callable[..., Application],
) -> None:
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    class ErrorTranslator:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

        def translate(self, path: Path, lang: str) -> bytes:
            logger = logging.getLogger("babelarr")
            logger.error(
                "HTTP error from LibreTranslate status=%s detail=boom headers=%s body=%s",
                self.status_code,
                {},
                "",
            )
            resp = requests.Response()
            resp.status_code = self.status_code
            raise requests.HTTPError(response=resp)

    translator = ErrorTranslator(status)
    app_instance = app(translator=translator)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(requests.HTTPError):
            app_instance.translate_file(tmp_file, "nl")
        assert f"status={status}" in caplog.text
        assert "detail=boom" in caplog.text


def test_retry_success(
    monkeypatch: MonkeyPatch, tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    attempts = {"count": 0}

    def fake_post(*args: object, **kwargs: object) -> requests.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise requests.ConnectionError("boom")
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"ok"
        return resp

    def fake_get(
        url: str, *, timeout: int, headers: dict[str, str] | None = None
    ) -> requests.Response:
        assert url == "http://example/languages"
        assert timeout == 180
        resp = requests.Response()
        resp.status_code = 200
        resp._content = (
            b'[{"code": "en", "targets": ["en", "nl"]},'
            b'{"code": "xx", "targets": ["nl"]}]'
        )
        return resp

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)

    translator = LibreTranslateClient(
        "http://example", "en", retry_count=3, backoff_delay=0
    )

    with caplog.at_level(logging.WARNING):
        result = translator.translate(tmp_file, "nl")
    translator.close()

    assert result == b"ok"
    assert attempts["count"] == 3
    retry_logs = [r for r in caplog.records if "attempt_failed" in r.message]
    assert len(retry_logs) == 2


def test_retry_exhaustion(
    monkeypatch: MonkeyPatch, tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    attempts = {"count": 0}

    def fake_post(*args: object, **kwargs: object) -> requests.Response:
        attempts["count"] += 1
        raise requests.ConnectionError("boom")

    def fake_get(
        url: str, *, timeout: int, headers: dict[str, str] | None = None
    ) -> requests.Response:
        assert url == "http://example/languages"
        assert timeout == 180
        resp = requests.Response()
        resp.status_code = 200
        resp._content = (
            b'[{"code": "en", "targets": ["en", "nl"]},'
            b'{"code": "xx", "targets": ["nl"]}]'
        )
        return resp

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)

    translator = LibreTranslateClient(
        "http://example", "en", retry_count=2, backoff_delay=0
    )

    with caplog.at_level(logging.ERROR):
        with pytest.raises(requests.ConnectionError):
            translator.translate(tmp_file, "nl")
    translator.close()

    assert attempts["count"] == 2
    assert "request_failed attempts=2" in caplog.text


def test_api_key_included(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    captured: dict[str, dict[str, object] | None] = {"data": None}

    def fake_post(
        url: str,
        *,
        files: dict[str, object] | None = None,
        data: dict[str, object] | None = None,
        timeout: int,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        assert timeout == 3600
        captured["data"] = data or {}
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"ok"
        return resp

    def fake_get(
        url: str, *, timeout: int, headers: dict[str, str] | None = None
    ) -> requests.Response:
        assert url == "http://example/languages"
        assert timeout == 180
        resp = requests.Response()
        resp.status_code = 200
        resp._content = (
            b'[{"code": "en", "targets": ["en", "nl"]},'
            b'{"code": "xx", "targets": ["nl"]}]'
        )
        return resp

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)

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
    assert captured["data"] is not None
    assert captured["data"]["api_key"] == "secret"


def test_src_lang_included(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    tmp_file = tmp_path / "sample.xx.srt"
    tmp_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    captured: dict[str, dict[str, object] | None] = {"data": None}

    def fake_post(
        url: str,
        *,
        files: dict[str, object] | None = None,
        data: dict[str, object] | None = None,
        timeout: int,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        assert timeout == 3600
        captured["data"] = data or {}
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"ok"
        return resp

    def fake_get(
        url: str, *, timeout: int, headers: dict[str, str] | None = None
    ) -> requests.Response:
        assert url == "http://example/languages"
        assert timeout == 180
        resp = requests.Response()
        resp.status_code = 200
        resp._content = (
            b'[{"code": "en", "targets": ["en", "nl"]},'
            b'{"code": "xx", "targets": ["nl"]}]'
        )
        return resp

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)

    translator = LibreTranslateClient(
        "http://example",
        "xx",
        retry_count=1,
        backoff_delay=0,
    )

    result = translator.translate(tmp_file, "nl")
    translator.close()

    assert result == b"ok"
    assert captured["data"] is not None
    assert captured["data"]["source"] == "xx"


def test_download_translated_file(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    def fake_post(
        url: str,
        *,
        files: dict[str, object] | None = None,
        data: dict[str, object] | None = None,
        timeout: int,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        assert timeout == 3600
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b'{"translatedFileUrl": "http://example/translated.srt"}'
        return resp

    downloaded: dict[str, str | None] = {"url": None}
    calls: list[tuple[str, int]] = []

    def fake_get(
        url: str, *, timeout: int, headers: dict[str, str] | None = None
    ) -> requests.Response:
        calls.append((url, timeout))
        assert timeout == 180
        resp = requests.Response()
        if url == "http://example/languages":
            resp.status_code = 200
            resp._content = (
                b'[{"code": "en", "targets": ["en", "nl"]},'
                b'{"code": "xx", "targets": ["nl"]}]'
            )
        else:
            downloaded["url"] = url
            resp.status_code = 200
            resp._content = b"translated"
        return resp

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)

    translator = LibreTranslateClient(
        "http://example", "en", retry_count=1, backoff_delay=0
    )

    result = translator.translate(tmp_file, "nl")
    translator.close()

    assert downloaded["url"] == "http://example/translated.srt"
    assert result == b"translated"
    assert calls == [
        ("http://example/languages", 30),
        ("http://example/translated.srt", 30),
    ]


def test_unsupported_source_language(monkeypatch: MonkeyPatch) -> None:
    def fake_get(
        url: str, *, timeout: int, headers: dict[str, str] | None = None
    ) -> requests.Response:
        assert url == "http://example/languages"
        assert timeout == 180
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b'[{"code": "en", "targets": ["en", "nl"]}]'
        return resp

    monkeypatch.setattr(requests, "get", fake_get)
    client = LibreTranslateClient("http://example", "zz")
    with pytest.raises(ValueError, match="Unsupported source language"):
        client.ensure_languages()


def test_unsupported_target_language(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    def fake_get(
        url: str, *, timeout: int, headers: dict[str, str] | None = None
    ) -> requests.Response:
        assert url == "http://example/languages"
        assert timeout == 180
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b'[{"code": "en", "targets": ["en"]}]'
        return resp

    monkeypatch.setattr(requests, "get", fake_get)

    translator = LibreTranslateClient(
        "http://example", "en", retry_count=1, backoff_delay=0
    )
    with pytest.raises(ValueError, match="Unsupported target language"):
        translator.translate(tmp_file, "nl")


def test_wait_until_available_uses_interval(monkeypatch: MonkeyPatch) -> None:
    client = LibreTranslateClient(
        "http://example", "en", availability_check_interval=0.1
    )
    monkeypatch.setattr(client, "ensure_languages", lambda: None)
    states = iter([False, True])
    monkeypatch.setattr(client, "is_available", lambda: next(states))
    sleeps: list[float] = []
    monkeypatch.setattr(translator.time, "sleep", lambda s: sleeps.append(s))
    client.wait_until_available()
    assert sleeps == [0.1]
