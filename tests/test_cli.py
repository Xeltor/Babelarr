from __future__ import annotations

import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from babelarr.cli import (
    _preferred_source_language,
    main,
    validate_ensure_languages,
    validate_environment,
)
from babelarr.config import Config
from babelarr.translator import LibreTranslateClient

if TYPE_CHECKING:
    from pytest import LogCaptureFixture, MonkeyPatch


def test_validate_environment_filters_mkv_dirs(
    tmp_path: Path, monkeypatch: MonkeyPatch, caplog: LogCaptureFixture
) -> None:
    valid_dir = tmp_path / "valid"
    valid_dir.mkdir()
    missing_dir = tmp_path / "missing"
    config = Config(
        root_dirs=["/unused"],
        api_url="http://example",
        workers=1,
        ensure_langs=["en"],
        retry_count=1,
        backoff_delay=0,
        mkv_dirs=[str(valid_dir), str(missing_dir)],
    )
    monkeypatch.setattr(
        "babelarr.cli.requests.head",
        lambda url, timeout: SimpleNamespace(status_code=200),
    )
    with caplog.at_level(logging.WARNING, logger="babelarr.cli"):
        validate_environment(config)
    assert config.mkv_dirs == [str(valid_dir)]
    assert config.root_dirs == [str(valid_dir)]
    assert "missing_mkv_dir" in caplog.text


def test_preferred_source_language() -> None:
    assert _preferred_source_language(["en", "nl"]) == "en"
    assert _preferred_source_language(["nl", "fr"]) == "nl"
    assert _preferred_source_language([]) == "en"


class _FakeTranslator(LibreTranslateClient):
    def __init__(self, supported: set[str]) -> None:
        # Bypass parent init
        self.supported = supported

    def ensure_languages(self) -> None:
        return None

    def is_target_supported(self, target_lang: str) -> bool:
        return target_lang in self.supported


def test_validate_ensure_languages_filters_and_exits(monkeypatch: MonkeyPatch) -> None:
    config = Config(
        root_dirs=["."],
        api_url="http://example",
        workers=1,
        ensure_langs=["en", "xx"],
        retry_count=1,
        backoff_delay=0,
        mkv_dirs=["."],
    )
    translator = _FakeTranslator({"en"})

    validate_ensure_languages(config, translator)
    assert config.ensure_langs == ["en"]

    translator_none = _FakeTranslator(set())
    config.ensure_langs = ["zz"]
    with pytest.raises(SystemExit):
        validate_ensure_languages(config, translator_none)


def test_main_bootstraps_application(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    called: dict[str, object] = {}
    mkv_dir = tmp_path / "mkv"
    mkv_dir.mkdir()

    class DummyConfig(Config):
        profiling_enabled: bool = False
        profiling_ui_host: str = "127.0.0.1"
        profiling_ui_port: int = 0

    config = DummyConfig(
        root_dirs=[str(mkv_dir)],
        api_url="http://example",
        workers=1,
        ensure_langs=["en"],
        retry_count=1,
        backoff_delay=0,
        mkv_dirs=[str(mkv_dir)],
    )

    class DummyApp:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.shutdown_event = threading.Event()
            called["app_init"] = True

        def run(self) -> None:
            called["app_run"] = True

    class DummyTranslator(LibreTranslateClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            called["translator_init"] = True

        def ensure_languages(self) -> None:
            return None

        def is_target_supported(self, target_lang: str) -> bool:
            return True

    monkeypatch.setattr("babelarr.cli.Config", DummyConfig)
    monkeypatch.setattr("babelarr.cli.Config.from_env", lambda: config)
    monkeypatch.setattr("babelarr.cli.Application", DummyApp)
    monkeypatch.setattr("babelarr.cli.LibreTranslateClient", DummyTranslator)
    monkeypatch.setattr("babelarr.cli.validate_environment", lambda cfg: cfg)
    monkeypatch.setattr("babelarr.cli.validate_ensure_languages", lambda c, t: None)
    monkeypatch.setattr("babelarr.cli.logging.basicConfig", lambda **kwargs: None)
    monkeypatch.setattr("babelarr.cli.signal.signal", lambda *args, **kwargs: None)

    main([])

    assert called["translator_init"] is True
    assert called["app_init"] is True
    assert called["app_run"] is True
