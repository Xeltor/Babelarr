from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from babelarr.config import Config

if TYPE_CHECKING:
    from pytest import LogCaptureFixture, MonkeyPatch


def test_parse_ensure_languages_filters_and_normalizes(
    caplog: LogCaptureFixture,
) -> None:
    raw = "nl, , EN, xx1, nl"
    with caplog.at_level(logging.WARNING, logger="babelarr"):
        langs = Config._parse_ensure_langs(raw, default=["en"])
    assert langs == ["nl", "en"]
    assert "ignore empty language code" in caplog.text
    assert "ignore invalid language code 'xx1'" in caplog.text


def test_parse_ensure_languages_empty_raises() -> None:
    with pytest.raises(ValueError):
        Config._parse_ensure_langs("", default=["en"])


def test_parse_workers_caps_and_defaults(caplog: LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="babelarr"):
        workers = Config._parse_workers("20")
    assert workers == 10
    assert "cap workers" in caplog.text


def test_parse_int_and_float_defaults(caplog: LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="babelarr"):
        assert Config._parse_int("workers", "not-a-number", 2) == 2
        assert Config._parse_float("delay", "oops", 1.5) == 1.5
    assert "invalid workers" in caplog.text
    assert "invalid delay" in caplog.text


def test_parse_detection_concurrency_invalid(caplog: LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="babelarr"):
        assert Config._parse_detection_concurrency("detection", "abc", 3) == 3
    assert Config._parse_detection_concurrency("detection", "-1", 2) is None
    assert Config._parse_detection_concurrency("detection", None, None) is None


def test_parse_bool_handles_invalid(caplog: LogCaptureFixture) -> None:
    assert Config._parse_bool("flag", "true", False) is True
    assert Config._parse_bool("flag", "off", True) is False
    with caplog.at_level(logging.WARNING, logger="babelarr"):
        assert Config._parse_bool("flag", "maybe", True) is True
    assert "invalid flag" in caplog.text


def test_from_env_rejects_empty_ensure_langs(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("ENSURE_LANGS", "")
    with pytest.raises(ValueError):
        Config.from_env()


def test_from_env_defaults_to_builtin_list(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ENSURE_LANGS", raising=False)
    monkeypatch.setenv("WATCH_DIRS", str(tmp_path))
    cfg = Config.from_env()
    assert cfg.ensure_langs == ["en", "nl", "bs"]


def test_invalid_workers_falls_back_to_default(
    monkeypatch: MonkeyPatch, caplog: LogCaptureFixture
) -> None:
    monkeypatch.setenv("WORKERS", "nope")
    with caplog.at_level(logging.WARNING, logger="babelarr"):
        cfg = Config.from_env()
    assert cfg.workers == 1
    assert "invalid WORKERS" in caplog.text


@pytest.mark.parametrize(
    ("env_var", "value", "attr"),
    [
        ("RETRY_COUNT", "10", "retry_count"),
        ("BACKOFF_DELAY", "5", "backoff_delay"),
        ("AVAILABILITY_CHECK_INTERVAL", "99", "availability_check_interval"),
        ("DEBOUNCE_SECONDS", "9.9", "debounce"),
        ("STABILIZE_TIMEOUT", "999", "stabilize_timeout"),
        ("SCAN_INTERVAL_MINUTES", "999", "scan_interval_minutes"),
        ("HTTP_TIMEOUT", "1", "http_timeout"),
        ("TRANSLATION_TIMEOUT", "1", "translation_timeout"),
    ],
)
def test_internal_defaults_ignore_overrides(
    monkeypatch: MonkeyPatch,
    caplog: LogCaptureFixture,
    env_var: str,
    value: str,
    attr: str,
) -> None:
    monkeypatch.setenv(env_var, value)
    with caplog.at_level(logging.WARNING, logger="babelarr"):
        cfg = Config.from_env()
    assert getattr(cfg, attr) == getattr(Config, attr)
    assert env_var.lower() not in caplog.text.lower()


def test_persistent_sessions_flag(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("PERSISTENT_SESSIONS", "true")
    cfg = Config.from_env()
    assert cfg.persistent_sessions is True


def test_jellyfin_env_parsed(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JELLYFIN_URL", "http://jf")
    monkeypatch.setenv("JELLYFIN_TOKEN", "abc")
    cfg = Config.from_env()
    assert cfg.jellyfin_url == "http://jf"
    assert cfg.jellyfin_token == "abc"


def test_jellyfin_defaults(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("JELLYFIN_URL", raising=False)
    monkeypatch.delenv("JELLYFIN_TOKEN", raising=False)
    cfg = Config.from_env()
    assert cfg.jellyfin_url is None
    assert cfg.jellyfin_token is None
