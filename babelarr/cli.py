import argparse
import logging
import os
import signal
from pathlib import Path

import requests

from .app import Application
from .config import Config
from .jellyfin_api import JellyfinClient
from .queue_db import QueueRepository
from .translator import LibreTranslateClient

logger = logging.getLogger(__name__)


def validate_environment(config: Config) -> None:
    """Validate watch directories and translation service availability.

    Updates ``config.root_dirs`` to only include readable directories. If none
    remain, the process exits with a clear error. The translation service is
    probed with a ``HEAD`` request and any failure is logged, but startup
    continues and workers will wait until the service becomes reachable.
    """

    valid_dirs: list[str] = []
    for d in config.root_dirs:
        path = Path(d)
        if not path.is_dir():
            logger.warning("missing_watch_dir path=%s", path.name)
            continue
        if not os.access(path, os.R_OK):
            logger.warning("unreadable_watch_dir path=%s", path.name)
            continue
        valid_dirs.append(d)

    if not valid_dirs:
        logger.error(
            "no_readable_dirs dirs=%s", [Path(d).name for d in config.root_dirs]
        )
        raise SystemExit("No valid watch directories configured")

    config.root_dirs = valid_dirs
    logger.info(
        "cli: environment_ready dirs=%s",
        [Path(d).name for d in valid_dirs],
    )

    try:
        resp = requests.head(config.api_url, timeout=900)
        if resp.status_code >= 400:
            raise requests.RequestException(f"HTTP {resp.status_code}")
    except requests.RequestException as exc:
        logger.error("service_unreachable url=%s error=%s", config.api_url, exc)


def filter_target_languages(config: Config, translator: LibreTranslateClient) -> None:
    """Remove unsupported target languages from *config*.

    Fetches supported languages from *translator* and filters ``config.target_langs``
    accordingly. Logs a warning for any ignored languages. Exits the process if no
    supported languages remain.
    """

    try:
        translator.ensure_languages()
    except ValueError as exc:
        logger.error("language_error detail=%s", exc)
        raise SystemExit(str(exc))
    except requests.RequestException as exc:  # pragma: no cover - network failure
        logger.error("fetch_languages_failed error=%s", exc)
        return

    supported = translator.supported_targets or set()
    unsupported = [lang for lang in config.target_langs if lang not in supported]
    if unsupported:
        logger.warning(
            "unsupported_targets langs=%s",
            ", ".join(unsupported),
        )
        config.target_langs = [
            lang for lang in config.target_langs if lang in supported
        ]

    logger.info("cli: target_langs langs=%s", ", ".join(config.target_langs))
    if not config.target_langs:
        logger.error("no_supported_targets")
        raise SystemExit("No supported target languages configured")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="babelarr")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set logging level",
    )
    parser.add_argument("--log-file", help="Write logs to a file")
    sub = parser.add_subparsers(dest="command")

    queue_parser = sub.add_parser("queue", help="Inspect the processing queue")
    queue_parser.add_argument("--list", action="store_true", help="List queued paths")

    args = parser.parse_args(argv)

    log_level = (args.log_level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    log_file = args.log_file or os.environ.get("LOG_FILE")
    logging.basicConfig(
        level=log_level,
        filename=log_file,
        format="%(asctime)s [%(levelname)s] [%(name)s] [%(threadName)s] %(message)s",
    )
    logging.getLogger("watchdog").setLevel(logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logger.info("cli: start log_level=%s log_file=%s", log_level, log_file)

    if args.command == "queue":
        config = Config.from_env()
        repo = QueueRepository(config.queue_db)
        count = repo.count()
        print(f"{count} pending item{'s' if count != 1 else ''}")
        if args.list:
            for path, lang, _ in repo.all():
                print(f"{path} [{lang}]")
        repo.close()
        return

    config = Config.from_env()
    validate_environment(config)
    logger.info(
        "cli: config_loaded api_url=%s targets=%s",
        config.api_url,
        config.target_langs,
    )
    translator = LibreTranslateClient(
        config.api_url,
        config.src_lang,
        config.retry_count,
        config.backoff_delay,
        config.availability_check_interval,
        api_key=config.api_key,
        persistent_session=config.persistent_sessions,
    )
    filter_target_languages(config, translator)
    jellyfin_client = None
    if config.jellyfin_url and config.jellyfin_token:
        jellyfin_client = JellyfinClient(config.jellyfin_url, config.jellyfin_token)
    app = Application(config, translator, jellyfin_client)

    def handle_signal(signum, frame):
        logger.info("received_signal signum=%s", signum)
        app.shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle_signal)

    app.run()


if __name__ == "__main__":
    main()
