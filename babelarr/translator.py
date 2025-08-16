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

    def close(self) -> None:
        """Close any open resources."""


class LibreTranslateClient:
    """Translator implementation using the LibreTranslate API."""

    def __init__(
        self,
        api_url: str,
        src_lang: str,
        retry_count: int = 3,
        backoff_delay: float = 1.0,
        api_key: str | None = None,
    ) -> None:
        base_url = api_url.rstrip("/")
        self.translate_url = base_url + "/translate_file"
        self.languages_url = base_url + "/languages"
        self.src_lang = src_lang
        self.retry_count = retry_count
        self.backoff_delay = backoff_delay
        self.api_key = api_key
        self.session = requests.Session()
        self._supported: dict[str, set[str]] | None = None
        # Block until the languages endpoint is reachable so we don't start
        # working until LibreTranslate is available.
        self._ensure_supported()

    def _ensure_supported(self) -> dict[str, set[str]]:
        while self._supported is None:
            try:
                resp = self.session.get(self.languages_url, timeout=60)
                resp.raise_for_status()
                data = resp.json()
                self._supported = {
                    item["code"]: set(item.get("targets", [])) for item in data
                }
            except requests.RequestException as exc:
                logger.warning(
                    "LibreTranslate unavailable: %s. Waiting %s seconds", exc, self.backoff_delay
                )
                time.sleep(self.backoff_delay)
        return self._supported

    def translate(self, path: Path, lang: str) -> bytes:
        supported = self._ensure_supported()
        if self.src_lang not in supported:
            raise ValueError(f"Unsupported source language: {self.src_lang}")
        if lang not in supported[self.src_lang]:
            raise ValueError(
                f"Unsupported target language: {lang} for source {self.src_lang}"
            )

        attempt = 0
        while True:
            attempt += 1
            try:
                with open(path, "rb") as fh:
                    files = {"file": fh}
                    data = {"source": self.src_lang, "target": lang, "format": "srt"}
                    if self.api_key:
                        data["api_key"] = self.api_key
                    resp = self.session.post(
                        self.translate_url, files=files, data=data, timeout=60
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

    def close(self) -> None:
        self.session.close()
