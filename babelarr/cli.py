import logging
import os
import signal

from .app import Application
from .config import Config
from .translator import LibreTranslateClient

logger = logging.getLogger("babelarr")


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    config = Config.from_env()
    translator = LibreTranslateClient(
        config.api_url, config.retry_count, config.backoff_delay
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
