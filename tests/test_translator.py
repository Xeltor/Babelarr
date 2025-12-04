from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import requests

from babelarr.libretranslate_api import LibreTranslateAPI
from babelarr.translator import LibreTranslateClient

LIVE_LT_URL = "http://192.168.1.200:5000"

if TYPE_CHECKING:
    from pytest import LogCaptureFixture, MonkeyPatch


class _DummyLock:
    def __init__(self) -> None:
        self.acquired = False

    def acquire(self) -> None:
        assert not self.acquired
        self.acquired = True

    def release(self) -> None:
        assert self.acquired
        self.acquired = False


def test_translate_file_thread_safety(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    tmp_file = tmp_path / "a.srt"
    tmp_file.write_text("dummy")

    calls: dict[int, dict[str, str] | None] = {}
    lock = threading.Lock()

    api = LibreTranslateAPI("http://only")

    def fake_post(
        url: str,
        *,
        files: dict[str, object] | None = None,
        data: dict[str, object] | None = None,
        timeout: int = 3600,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        assert timeout == api.translation_timeout
        with lock:
            calls[id(threading.current_thread())] = headers
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"ok"
        return resp

    monkeypatch.setattr(requests, "post", fake_post)

    results: list[bytes] = []
    errors: list[Exception] = []

    def worker() -> None:
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


def test_post_concurrency_limits_apply_when_detection_concurrency_disabled() -> None:
    client = LibreTranslateClient(
        "http://only",
        "en",
        max_concurrent_requests=1,
    )
    post_lock = _DummyLock()
    client._post_concurrency = cast(threading.Semaphore, post_lock)

    with client._acquire_slot():
        assert post_lock.acquired
    assert not post_lock.acquired

    client.close()


def test_is_available_uses_http_timeout(monkeypatch: MonkeyPatch) -> None:
    client = LibreTranslateClient("http://only", "en")

    class DummyResp:
        status_code: int = 200

    def fake_head(self, url: str, *, timeout: int) -> DummyResp:
        assert timeout == 180
        return DummyResp()

    monkeypatch.setattr(requests.Session, "head", fake_head)

    assert client.is_available() is True
    client.api.close()


def test_handle_error_response_logs_and_raises(caplog: LogCaptureFixture) -> None:
    client = LibreTranslateClient("http://only", "en")
    resp = requests.Response()
    resp.status_code = 500
    resp._content = b'{"error":"boom"}'
    resp.url = "http://only/error"

    with caplog.at_level(logging.ERROR):
        with pytest.raises(requests.HTTPError):
            client._handle_error_response(resp, "context")
    assert "http_error" in caplog.text
    client.api.close()


def test_detect_language_empty_sample_logs(
    monkeypatch: MonkeyPatch, caplog: LogCaptureFixture
) -> None:
    client = LibreTranslateClient("http://only", "en")
    with caplog.at_level(logging.DEBUG):
        assert client.detect_language("") is None
    assert "detect_skip" in caplog.text
    client.api.close()


@pytest.mark.integration
def test_detect_language_live() -> None:
    client = LibreTranslateClient(LIVE_LT_URL, "en")
    detection = client.detect_language("hello world", min_confidence=0.1)
    assert detection is not None
    assert detection.language
    client.close()


def test_request_translation_download_flow(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    client = LibreTranslateClient("http://only", "en")
    dummy = requests.Response()
    dummy.status_code = 200
    dummy._content = b'{"translatedFileUrl": "http://only/download"}'
    download = requests.Response()
    download.status_code = 200
    download._content = b"translated"

    monkeypatch.setattr(client.api, "translate_file", lambda *args, **kwargs: dummy)
    monkeypatch.setattr(client.api, "download", lambda url: download)
    monkeypatch.setattr(client, "_handle_error_response", lambda *args, **kwargs: None)

    src = tmp_path / "src.srt"
    src.write_text("hello")
    result = client._request_translation(src, "en", "es")

    assert result == b"translated"
    client.api.close()


@pytest.mark.integration
def test_translate_live(tmp_path: Path) -> None:
    src = tmp_path / "sample.en.srt"
    src.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
    client = LibreTranslateClient(LIVE_LT_URL, "en")
    translated = client.translate(src, "es")
    assert translated
    client.close()


def _prepared_client() -> LibreTranslateClient:
    client = LibreTranslateClient("http://example", "en")
    client.languages = {"en": {"nl"}}
    client.supported_targets = {"nl"}
    return client


def test_translate_success(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("dummy")

    client = _prepared_client()
    client.retry_count = 1

    def fake_translate_file(
        path: Path, src: str, dst: str, api_key: str | None
    ) -> requests.Response:
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"ok"
        return resp

    monkeypatch.setattr(client.api, "translate_file", fake_translate_file)
    called = {"download": False}

    def fake_download(url: str) -> requests.Response:
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


def test_translate_error(
    monkeypatch: MonkeyPatch, tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("dummy")

    client = _prepared_client()

    def fake_translate_file(
        path: Path, src: str, dst: str, api_key: str | None
    ) -> requests.Response:
        resp = requests.Response()
        resp.status_code = 400
        resp._content = b'{"error": "boom"}'
        resp.headers["X-Test"] = "1"
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


def test_translate_download(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("dummy")

    client = _prepared_client()

    def fake_translate_file(
        path: Path, src: str, dst: str, api_key: str | None
    ) -> requests.Response:
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b'{"translatedFileUrl": "http://download"}'
        return resp

    def fake_download(url: str) -> requests.Response:
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


def test_ensure_languages_logs_count(
    monkeypatch: MonkeyPatch, caplog: LogCaptureFixture
) -> None:
    client = LibreTranslateClient("http://example", "en")
    monkeypatch.setattr(
        client.api,
        "fetch_languages",
        lambda: [{"code": "en", "targets": ["nl", "es"]}],
    )
    with caplog.at_level(logging.INFO):
        client.ensure_languages()
    assert "languages_loaded sources=1 default_targets=2" in caplog.text


def test_wait_until_available_logs_service_available(
    monkeypatch: MonkeyPatch, caplog: LogCaptureFixture
) -> None:
    client = LibreTranslateClient(
        "http://example", "en", availability_check_interval=0.1
    )
    monkeypatch.setattr(client, "is_available", lambda: True)
    monkeypatch.setattr(client, "ensure_languages", lambda: None)
    with caplog.at_level(logging.INFO):
        client.wait_until_available()
    assert "service_available" in caplog.text


def test_detect_language_returns_best_match(monkeypatch: MonkeyPatch) -> None:
    client = LibreTranslateClient("http://example", "en")
    payload = [
        {"language": "de", "confidence": 0.4},
        {"language": "en", "confidence": 0.9},
    ]

    def fake_detect(sample: str) -> requests.Response:
        assert sample == "sample text"
        resp = requests.Response()
        resp.status_code = 200
        resp._content = json.dumps(payload).encode()
        return resp

    monkeypatch.setattr(client.api, "detect", fake_detect)

    result = client.detect_language(" sample text ", min_confidence=0.5)
    assert result is not None
    assert result.language == "en"
    assert result.confidence == pytest.approx(0.9)


def test_detect_language_requires_threshold(monkeypatch: MonkeyPatch) -> None:
    client = LibreTranslateClient("http://example", "en")

    def fake_detect(sample: str) -> requests.Response:
        resp = requests.Response()
        resp.status_code = 200
        resp._content = json.dumps([{"language": "es", "confidence": 0.2}]).encode()
        return resp

    monkeypatch.setattr(client.api, "detect", fake_detect)

    result = client.detect_language("hola", min_confidence=0.5)
    assert result is None


def test_detect_language_normalizes_percentage_confidence(
    monkeypatch: MonkeyPatch,
) -> None:
    client = LibreTranslateClient("http://example", "en")
    payload = [{"language": "en", "confidence": 14}]

    def fake_detect(sample: str) -> requests.Response:
        resp = requests.Response()
        resp.status_code = 200
        resp._content = json.dumps(payload).encode()
        return resp

    monkeypatch.setattr(client.api, "detect", fake_detect)

    high_threshold = client.detect_language("text", min_confidence=0.5)
    assert high_threshold is None

    low_threshold = client.detect_language("text", min_confidence=0.1)
    assert low_threshold is not None
    assert low_threshold.language == "en"
    assert low_threshold.confidence == pytest.approx(0.14)


def test_detect_language_uses_detection_concurrency(
    monkeypatch: MonkeyPatch,
) -> None:
    client = LibreTranslateClient(
        "http://example",
        "en",
        max_concurrent_requests=1,
    )
    detection_started = threading.Event()

    def fake_detect(sample: str) -> requests.Response:
        detection_started.set()
        resp = requests.Response()
        resp.status_code = 200
        resp._content = json.dumps([{"language": "en", "confidence": 1.0}]).encode()
        return resp

    monkeypatch.setattr(client.api, "detect", fake_detect)

    with client._acquire_slot():
        thread = threading.Thread(
            target=client.detect_language,
            args=("text",),
            kwargs={"min_confidence": 0.0},
        )
        thread.start()
        assert not detection_started.wait(0.1)

    assert detection_started.wait(0.5)

    thread.join()
    client.close()
