"""Translation client abstractions."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Protocol

import requests

from .libretranslate_api import LibreTranslateAPI

logger = logging.getLogger(__name__)

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

    def wait_until_available(self) -> None:  # pragma: no cover - optional
        """Block until the translation service becomes available."""


class LibreTranslateClient:
    """Translator implementation using the LibreTranslate API."""

    def __init__(
        self,
        api_url: str,
        src_lang: str,
        retry_count: int = 3,
        backoff_delay: float = 1.0,
        availability_check_interval: float = 30.0,
        api_key: str | None = None,
        persistent_session: bool = False,
        http_timeout: float = 30.0,
        translation_timeout: float = 900.0,
    ) -> None:
        self.src_lang = src_lang
        self.retry_count = retry_count
        self.backoff_delay = backoff_delay
        self.availability_check_interval = availability_check_interval
        self.api_key = api_key

        self.api = LibreTranslateAPI(
            api_url,
            http_timeout=http_timeout,
            translation_timeout=translation_timeout,
            persistent_session=persistent_session,
        )
        self.languages: dict[str, set[str]] | None = None
        self.supported_targets: set[str] | None = None

    def is_available(self) -> bool:
        """Return ``True`` if the service responds without error."""
        try:
            resp = self.api.session.head(
                self.api.base_url, timeout=self.api.http_timeout
            )
            return resp.status_code < 400
        except requests.RequestException:
            return False

    def wait_until_available(self) -> None:
        """Block until the translation service is reachable and languages loaded."""
        while True:
            if self.is_available():
                try:
                    self.ensure_languages()
                except requests.RequestException as exc:
                    logger.warning("fetch_languages_failed error=%s", exc)
                else:
                    logger.info("service_available")
                    return
            logger.warning(
                "service_unreachable retry=%s",
                self.availability_check_interval,
            )
            time.sleep(self.availability_check_interval)

    def ensure_languages(self) -> None:
        """Fetch and cache supported language mappings."""
        if self.languages is not None:
            return
        languages = self.api.fetch_languages()
        self.languages = {
            lang["code"]: set(lang.get("targets", [])) for lang in languages
        }
        if self.src_lang not in self.languages:
            raise ValueError(f"Unsupported source language: {self.src_lang}")
        self.supported_targets = self.languages[self.src_lang]
        logger.info(
            "languages_loaded count=%d",
            len(self.supported_targets),
        )

    def _handle_error_response(self, resp: requests.Response, context: str) -> None:
        """Log and raise for non-200 *resp* responses."""
        if resp.status_code == 200:
            return

        detail = ERROR_MESSAGES.get(resp.status_code, "Unexpected error")
        try:
            err_json = resp.json()
            extra = (
                err_json.get("error")
                or err_json.get("message")
                or err_json.get("detail")
            )
            if extra:
                detail = f"{detail}: {extra}"
        except ValueError:
            pass

        logger.error(
            "http_error context=%s status=%s detail=%s headers=%s body=%s",
            context,
            resp.status_code,
            detail,
            resp.headers,
            resp.text,
        )
        if logger.isEnabledFor(logging.DEBUG):
            import tempfile

            tmp = tempfile.NamedTemporaryFile(
                delete=False, prefix="babelarr-", suffix=".err"
            )
            try:
                tmp.write(resp.content)
                logger.debug("save_error_response path=%s", Path(tmp.name).name)
            finally:
                tmp.close()
        resp.raise_for_status()

    def _retrieve_download(self, download_url: str) -> bytes:
        """Fetch translated content from *download_url*."""
        download = self.api.download(download_url)
        self._handle_error_response(download, "LibreTranslate download")
        return download.content

    def _request_translation(self, path: Path, lang: str) -> bytes:
        """Send translation request and handle optional download flow."""
        resp = self.api.translate_file(path, self.src_lang, lang, self.api_key)
        self._handle_error_response(resp, "LibreTranslate")

        try:
            data = resp.json()
        except ValueError:
            return resp.content

        download_url = data.get("translatedFileUrl")
        if download_url:
            return self._retrieve_download(download_url)
        return resp.content

    def translate(self, path: Path, lang: str) -> bytes:
        self.ensure_languages()
        if self.supported_targets is None or lang not in self.supported_targets:
            raise ValueError(f"Unsupported target language: {lang}")
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._request_translation(path, lang)
            except requests.RequestException as exc:
                if attempt >= self.retry_count:
                    logger.error(
                        "request_failed attempts=%s error=%s",
                        attempt,
                        exc,
                    )
                    raise
                delay = self.backoff_delay * (2 ** (attempt - 1))
                logger.warning(
                    "attempt_failed attempt=%s error=%s delay=%s",
                    attempt,
                    exc,
                    delay,
                )
                time.sleep(delay)

    def close(self) -> None:
        asyncio.run(self.api.close())
