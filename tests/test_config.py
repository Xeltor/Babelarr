import logging
import pytest

from babelarr.config import Config


def test_from_env_rejects_empty_target_langs(monkeypatch):
    monkeypatch.setenv("TARGET_LANGS", "")
    with pytest.raises(ValueError):
        Config.from_env()


def test_invalid_workers_defaults(monkeypatch, caplog):
    monkeypatch.setenv("WORKERS", "bad")
    with caplog.at_level(logging.WARNING):
        cfg = Config.from_env()
    assert cfg.workers == 1
    assert any("Invalid WORKERS" in r.message for r in caplog.records)


def test_invalid_retry_count(monkeypatch, caplog):
    monkeypatch.setenv("RETRY_COUNT", "bad")
    with caplog.at_level(logging.WARNING):
        cfg = Config.from_env()
    assert cfg.retry_count == 3
    assert any("Invalid RETRY_COUNT" in r.message for r in caplog.records)


def test_invalid_backoff_delay(monkeypatch, caplog):
    monkeypatch.setenv("BACKOFF_DELAY", "bad")
    with caplog.at_level(logging.WARNING):
        cfg = Config.from_env()
    assert cfg.backoff_delay == 1.0
    assert any("Invalid BACKOFF_DELAY" in r.message for r in caplog.records)


def test_invalid_debounce(monkeypatch, caplog):
    monkeypatch.setenv("DEBOUNCE_SECONDS", "bad")
    with caplog.at_level(logging.WARNING):
        cfg = Config.from_env()
    assert cfg.debounce == 0.1
    assert any("Invalid DEBOUNCE_SECONDS" in r.message for r in caplog.records)


def test_invalid_scan_interval(monkeypatch, caplog):
    monkeypatch.setenv("SCAN_INTERVAL_MINUTES", "bad")
    with caplog.at_level(logging.WARNING):
        cfg = Config.from_env()
    assert cfg.scan_interval_minutes == 60
    assert any("Invalid SCAN_INTERVAL_MINUTES" in r.message for r in caplog.records)
