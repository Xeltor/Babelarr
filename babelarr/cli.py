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
        resp = requests.head(config.api_url, timeout=5)
        if resp.status_code >= 400:
            raise requests.RequestException(f"HTTP {resp.status_code}")
    except requests.RequestException as exc:
        logger.error(
            "Translation service %s unreachable at startup: %s", config.api_url, exc
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
