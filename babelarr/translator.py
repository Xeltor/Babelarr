"""Translation client abstractions."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Protocol

import requests

logger = logging.getLogger("babelarr")

ERROR_MESSAGES = {
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    429: "Too Many Requests",
    500: "Server Error",
}


class Translator(Protocol):
    """Protocol for translation clients."""

    def translate(self, path: Path, lang: str) -> bytes:
        """Translate the subtitle at *path* to *lang*.

        Returns the translated subtitle bytes or raises an exception.
        """


class LibreTranslateClient:
    """Translator implementation using the LibreTranslate API."""

    def __init__(
        self, api_url: str, retry_count: int = 3, backoff_delay: float = 1.0
    ) -> None:
        self.api_url = api_url
        self.retry_count = retry_count
        self.backoff_delay = backoff_delay

    def translate(self, path: Path, lang: str) -> bytes:
        attempt = 0
        while True:
            attempt += 1
            try:
                with open(path, "rb") as fh:
                    files = {"file": fh}
                    data = {"source": "en", "target": lang, "format": "srt"}
                    resp = requests.post(
                        self.api_url, files=files, data=data, timeout=60
                    )
                if resp.status_code != 200:
                    message = ERROR_MESSAGES.get(resp.status_code, "Unexpected error")
                    try:
                        err_json = resp.json()
                        detail = (
                            err_json.get("error")
                            or err_json.get("message")
                            or err_json.get("detail")
                        )
                        if detail:
                            message = f"{message}: {detail}"
                    except ValueError:
                        pass
                    logger.error(
                        "HTTP %s from LibreTranslate: %s", resp.status_code, message
                    )
                    logger.error("Headers: %s", resp.headers)
                    logger.error("Body: %s", resp.text)
                    if logger.isEnabledFor(logging.DEBUG):
                        import tempfile

                        tmp = tempfile.NamedTemporaryFile(
                            delete=False, prefix="babelarr-", suffix=".err"
                        )
                        try:
                            tmp.write(resp.content)
                            logger.debug("Saved failing response to %s", tmp.name)
                        finally:
                            tmp.close()
                    resp.raise_for_status()
                return resp.content
            except requests.RequestException as exc:
                if attempt >= self.retry_count:
                    logger.error(
                        "LibreTranslate request failed after %s attempts: %s",
                        attempt,
                        exc,
                    )
                    raise
                delay = self.backoff_delay * (2 ** (attempt - 1))
                logger.warning(
                    "Attempt %s failed: %s. Retrying in %s seconds",
                    attempt,
                    exc,
                    delay,
                )
                time.sleep(delay)
