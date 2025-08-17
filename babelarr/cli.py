import argparse
import logging
import os
import signal
from pathlib import Path

import requests

from .app import Application
from .config import Config
from .queue_db import QueueRepository
from .translator import LibreTranslateClient

logger = logging.getLogger("babelarr")


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
            logger.warning("Watch directory %s does not exist; ignoring", d)
            continue
        if not os.access(path, os.R_OK):
            logger.warning("Watch directory %s is not readable; ignoring", d)
            continue
        valid_dirs.append(d)

    if not valid_dirs:
        logger.error("No readable watch directories found: %s", config.root_dirs)
        raise SystemExit("No valid watch directories configured")

    config.root_dirs = valid_dirs

    try:
        resp = requests.head(config.api_url, timeout=900)
        if resp.status_code >= 400:
            raise requests.RequestException(f"HTTP {resp.status_code}")
    except requests.RequestException as exc:
        logger.error(
            "Translation service %s unreachable at startup: %s", config.api_url, exc
        )


def filter_target_languages(config: Config, translator: LibreTranslateClient) -> None:
    """Remove unsupported target languages from *config*.

    Fetches supported languages from *translator* and filters ``config.target_langs``
    accordingly. Logs a warning for any ignored languages. Exits the process if no
    supported languages remain.
    """

    try:
        translator.ensure_languages()
    except ValueError as exc:
        logger.error("%s", exc)
        raise SystemExit(str(exc))
    except requests.RequestException as exc:  # pragma: no cover - network failure
        logger.error("Failed to fetch supported languages: %s", exc)
        return

    supported = translator.supported_targets or set()
    unsupported = [lang for lang in config.target_langs if lang not in supported]
    if unsupported:
        logger.warning(
            "Ignoring unsupported target language%s: %s",
            "s" if len(unsupported) > 1 else "",
            ", ".join(unsupported),
        )
        config.target_langs = [
            lang for lang in config.target_langs if lang in supported
        ]

    if not config.target_langs:
        logger.error("No supported target languages configured")
        raise SystemExit("No supported target languages configured")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="babelarr")
    sub = parser.add_subparsers(dest="command")

    queue_parser = sub.add_parser("queue", help="Inspect the processing queue")
    queue_parser.add_argument("--list", action="store_true", help="List queued paths")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
    )
    logging.getLogger("watchdog").setLevel(logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if args.command == "queue":
        config = Config.from_env()
        repo = QueueRepository(config.queue_db)
        count = repo.count()
        print(f"{count} pending item{'s' if count != 1 else ''}")
        if args.list:
            for path, lang in repo.all():
                print(f"{path} [{lang}]")
        repo.close()
        return

    config = Config.from_env()
    validate_environment(config)
    translator = LibreTranslateClient(
        config.api_url,
        config.src_lang,
        config.retry_count,
        config.backoff_delay,
        config.availability_check_interval,
        api_key=config.api_key,
    )
    filter_target_languages(config, translator)
    app = Application(config, translator)

    def handle_signal(signum, frame):
        logger.info("Received signal %s", signum)
        app.shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle_signal)

    app.run()


if __name__ == "__main__":
    main()
