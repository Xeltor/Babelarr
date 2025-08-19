import logging

import pytest

from babelarr.config import Config


@pytest.fixture(autouse=True)
def temp_queue_db(monkeypatch, tmp_path):
    monkeypatch.setenv("QUEUE_DB", str(tmp_path / "queue.db"))


def test_parse_target_languages_filters_and_normalizes(caplog):
    raw = "nl, , EN, xx1, nl"
    with caplog.at_level(logging.WARNING, logger="babelarr"):
        langs = Config._parse_target_languages(raw)
    assert langs == ["nl", "en"]
    assert "Empty language code" in caplog.text
    assert "Invalid language code 'xx1'" in caplog.text


def test_parse_target_languages_empty_raises():
    with pytest.raises(ValueError):
        Config._parse_target_languages("")


def test_parse_workers_caps_and_defaults(caplog):
    with caplog.at_level(logging.WARNING, logger="babelarr"):
        workers = Config._parse_workers("20")
    assert workers == 10
    assert "capping" in caplog.text


def test_parse_scan_interval_invalid(caplog):
    with caplog.at_level(logging.WARNING, logger="babelarr"):
        interval = Config._parse_scan_interval("bad")
    assert interval == 60
    assert "Invalid SCAN_INTERVAL_MINUTES" in caplog.text


def test_from_env_rejects_empty_target_langs(monkeypatch):
    monkeypatch.setenv("TARGET_LANGS", "")
    with pytest.raises(ValueError):
        Config.from_env()


def test_invalid_workers_falls_back_to_default(monkeypatch, caplog):
    monkeypatch.setenv("WORKERS", "nope")
    with caplog.at_level(logging.WARNING, logger="babelarr"):
        cfg = Config.from_env()
    assert cfg.workers == 1
    assert "Invalid WORKERS" in caplog.text


def test_invalid_retry_count_falls_back_to_default(monkeypatch, caplog):
    monkeypatch.setenv("RETRY_COUNT", "bad")
    with caplog.at_level(logging.WARNING, logger="babelarr"):
        cfg = Config.from_env()
    assert cfg.retry_count == 3
    assert "Invalid RETRY_COUNT" in caplog.text


def test_invalid_backoff_delay_falls_back_to_default(monkeypatch, caplog):
    monkeypatch.setenv("BACKOFF_DELAY", "bad")
    with caplog.at_level(logging.WARNING, logger="babelarr"):
        cfg = Config.from_env()
    assert cfg.backoff_delay == 1.0
    assert "Invalid BACKOFF_DELAY" in caplog.text


def test_invalid_debounce_falls_back_to_default(monkeypatch, caplog):
    monkeypatch.setenv("DEBOUNCE_SECONDS", "bad")
    with caplog.at_level(logging.WARNING, logger="babelarr"):
        cfg = Config.from_env()
    assert cfg.debounce == 0.1
    assert "Invalid DEBOUNCE_SECONDS" in caplog.text


def test_invalid_scan_interval_falls_back_to_default(monkeypatch, caplog):
    monkeypatch.setenv("SCAN_INTERVAL_MINUTES", "bad")
    with caplog.at_level(logging.WARNING, logger="babelarr"):
        cfg = Config.from_env()
    assert cfg.scan_interval_minutes == 60
    assert "Invalid SCAN_INTERVAL_MINUTES" in caplog.text


def test_persistent_sessions_flag(monkeypatch):
    monkeypatch.setenv("PERSISTENT_SESSIONS", "true")
    cfg = Config.from_env()
    assert cfg.persistent_sessions is True


def test_jellyfin_env_parsed(monkeypatch, tmp_path):
    monkeypatch.setenv("QUEUE_DB", str(tmp_path / "queue.db"))
    monkeypatch.setenv("JELLYFIN_URL", "http://jf")
    monkeypatch.setenv("JELLYFIN_TOKEN", "abc")
    cfg = Config.from_env()
    assert cfg.jellyfin_url == "http://jf"
    assert cfg.jellyfin_token == "abc"


def test_jellyfin_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("QUEUE_DB", str(tmp_path / "queue.db"))
    monkeypatch.delenv("JELLYFIN_URL", raising=False)
    monkeypatch.delenv("JELLYFIN_TOKEN", raising=False)
    cfg = Config.from_env()
    assert cfg.jellyfin_url is None
    assert cfg.jellyfin_token is None
