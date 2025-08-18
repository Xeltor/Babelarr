import logging
import types

import pytest

from babelarr import cli
from babelarr.config import Config
from babelarr.queue_db import QueueRepository


def test_main_sets_watchdog_logger_to_info(monkeypatch):
    config = Config(
        root_dirs=["/tmp"],
        target_langs=["nl"],
        src_lang="en",
        src_ext=".en.srt",
        api_url="http://example",
        workers=1,
        queue_db=":memory:",
    )

    monkeypatch.setattr(cli.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(cli, "validate_environment", lambda cfg: None)

    class DummyApp:
        def __init__(self, *args, **kwargs):
            self.shutdown_event = types.SimpleNamespace(set=lambda: None)

        def run(self) -> None:  # pragma: no cover - does nothing
            return None

    monkeypatch.setattr(cli, "Application", DummyApp)

    class DummyTranslator:
        def __init__(self, *args, **kwargs):
            self.supported_targets = {"nl"}

        def ensure_languages(self):
            return None

    monkeypatch.setattr(cli, "LibreTranslateClient", DummyTranslator)

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    logging.getLogger().handlers.clear()
    logging.getLogger().setLevel(logging.NOTSET)
    logging.getLogger("watchdog").setLevel(logging.NOTSET)

    cli.main([])

    assert logging.getLogger("watchdog").level == logging.INFO


def test_main_sets_urllib3_logger_to_warning(monkeypatch):
    config = Config(
        root_dirs=["/tmp"],
        target_langs=["nl"],
        src_lang="en",
        src_ext=".en.srt",
        api_url="http://example",
        workers=1,
        queue_db=":memory:",
    )

    monkeypatch.setattr(cli.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(cli, "validate_environment", lambda cfg: None)

    class DummyApp:
        def __init__(self, *args, **kwargs):
            self.shutdown_event = types.SimpleNamespace(set=lambda: None)

        def run(self) -> None:  # pragma: no cover - does nothing
            return None

    monkeypatch.setattr(cli, "Application", DummyApp)

    class DummyTranslator:
        def __init__(self, *args, **kwargs):
            self.supported_targets = {"nl"}

        def ensure_languages(self):
            return None

    monkeypatch.setattr(cli, "LibreTranslateClient", DummyTranslator)

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    logging.getLogger().handlers.clear()
    logging.getLogger().setLevel(logging.NOTSET)
    logging.getLogger("urllib3").setLevel(logging.NOTSET)

    cli.main([])

    assert logging.getLogger("urllib3").level == logging.WARNING


def test_log_level_debug_logs_configuration(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QUEUE_DB", str(tmp_path / "queue.db"))
    logging.getLogger().handlers.clear()
    logging.getLogger().setLevel(logging.NOTSET)
    cli.main(["--log-level", "DEBUG", "queue"])
    captured = capsys.readouterr()
    assert "loaded config" in captured.err


def test_log_file_option_creates_file(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_DB", str(tmp_path / "queue.db"))
    log_file = tmp_path / "test.log"
    logging.getLogger().handlers.clear()
    logging.getLogger().setLevel(logging.NOTSET)
    cli.main(["--log-level", "DEBUG", "--log-file", str(log_file), "queue"])
    assert log_file.exists()
    assert log_file.read_text() != ""


def test_queue_outputs_count_and_paths(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "queue.db"
    with QueueRepository(str(db_path)) as repo:
        repo.add(tmp_path / "one", "nl")
        repo.add(tmp_path / "two", "nl")

    config = Config(
        root_dirs=[],
        target_langs=[],
        src_lang="en",
        src_ext=".en.srt",
        api_url="http://example",
        workers=1,
        queue_db=str(db_path),
    )

    monkeypatch.setattr(cli.Config, "from_env", classmethod(lambda cls: config))
    cli.main(["queue", "--list"])
    out = capsys.readouterr().out.strip().splitlines()
    assert out[0] == "2 pending items"
    assert set(out[1:]) == {
        f"{tmp_path / 'one'} [nl]",
        f"{tmp_path / 'two'} [nl]",
    }


def test_queue_outputs_count(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "queue.db"
    with QueueRepository(str(db_path)) as repo:
        repo.add(tmp_path / "one", "nl")
        repo.add(tmp_path / "two", "nl")

    config = Config(
        root_dirs=[],
        target_langs=[],
        src_lang="en",
        src_ext=".en.srt",
        api_url="http://example",
        workers=1,
        queue_db=str(db_path),
    )

    monkeypatch.setattr(cli.Config, "from_env", classmethod(lambda cls: config))
    cli.main(["queue"])
    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["2 pending items"]


def test_filter_target_languages_removes_unsupported(caplog):
    config = Config(
        root_dirs=["/tmp"],
        target_langs=["nl", "xx"],
        src_lang="en",
        src_ext=".en.srt",
        api_url="http://example",
        workers=1,
        queue_db=":memory:",
    )

    class DummyTranslator:
        def __init__(self):
            self.supported_targets = {"nl"}

        def ensure_languages(self):
            return None

    translator = DummyTranslator()

    with caplog.at_level(logging.WARNING):
        cli.filter_target_languages(config, translator)
        assert config.target_langs == ["nl"]
        assert "unsupported_targets" in caplog.text


def test_filter_target_languages_exits_when_none_supported():
    config = Config(
        root_dirs=["/tmp"],
        target_langs=["xx"],
        src_lang="en",
        src_ext=".en.srt",
        api_url="http://example",
        workers=1,
        queue_db=":memory:",
    )

    class DummyTranslator:
        def __init__(self):
            self.supported_targets = set()

        def ensure_languages(self):
            return None

    translator = DummyTranslator()

    with pytest.raises(SystemExit):
        cli.filter_target_languages(config, translator)
