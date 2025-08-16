import logging
import os
import signal
from pathlib import Path

import requests

from .app import Application
from .config import Config
from .translator import LibreTranslateClient

logger = logging.getLogger("babelarr")


def validate_environment(config: Config) -> None:
    """Validate watch directories and translation service availability.

    Updates ``config.root_dirs`` to only include readable directories. If none
    remain, the process exits with a clear error. The translation service is
    checked with a simple ``HEAD`` request and the process aborts if it is
    unreachable.
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
        resp = requests.head(config.api_url, timeout=5)
        if resp.status_code >= 400:
            raise requests.RequestException(f"HTTP {resp.status_code}")

        languages_url = config.api_url.rstrip("/") + "/languages"
        lang_resp = requests.get(languages_url, timeout=5)
        lang_resp.raise_for_status()
        data = lang_resp.json()
    except requests.RequestException as exc:
        logger.error("Translation service %s unreachable: %s", config.api_url, exc)
        raise SystemExit(f"Translation service unreachable: {config.api_url}")

    supported = {item["code"]: set(item.get("targets", [])) for item in data}
    if config.src_lang not in supported:
        logger.error("Unsupported source language: %s", config.src_lang)
        raise SystemExit(f"Unsupported source language: {config.src_lang}")
    unsupported = [t for t in config.target_langs if t not in supported[config.src_lang]]
    if unsupported:
        logger.error(
            "Unsupported target languages for %s: %s", config.src_lang, unsupported
        )
        raise SystemExit(
            "Unsupported target languages: " + ", ".join(sorted(unsupported))
        )


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    config = Config.from_env()
    validate_environment(config)
    translator = LibreTranslateClient(
        config.api_url,
        config.src_lang,
        config.retry_count,
        config.backoff_delay,
        api_key=config.api_key,
    )
    app = Application(config, translator)

    def handle_signal(signum, frame):
        logger.info("Received signal %s", signum)
        app.shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle_signal)

    app.run()


if __name__ == "__main__":
    main()
