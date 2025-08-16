import logging
import types

from babelarr import cli
from babelarr.config import Config


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
    monkeypatch.setattr(cli, "LibreTranslateClient", lambda *a, **k: object())

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    logging.getLogger().handlers.clear()
    logging.getLogger().setLevel(logging.NOTSET)
    logging.getLogger("watchdog").setLevel(logging.NOTSET)

    cli.main()

    assert logging.getLogger("watchdog").level == logging.INFO
