"""Translation client abstractions."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar

import requests

from .libretranslate_api import LibreTranslateAPI
from .profiling import WorkloadProfiler

logger = logging.getLogger(__name__)
TResponse = TypeVar("TResponse")

ERROR_MESSAGES = {
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    429: "Too Many Requests",
    500: "Server Error",
}


class Translator(Protocol):
    """Protocol for translation clients."""

    def translate(self, path: Path, lang: str, *, src_lang: str | None = None) -> bytes:
        """Translate the subtitle at *path* to *lang* using *src_lang* when provided.

        Returns the translated subtitle bytes or raises an exception.
        """

    def close(self) -> None:
        """Close any open resources."""

    def wait_until_available(self) -> None:  # pragma: no cover - optional
        """Block until the translation service becomes available."""

    def supports_translation(self, src_lang: str, target_lang: str) -> bool:
        """Return True when *src_lang* can be translated to *target_lang*."""

    def is_target_supported(self, target_lang: str) -> bool:
        """Return True if *target_lang* is supported by the service."""


@dataclass(frozen=True)
class DetectionResult:
    language: str
    confidence: float


class LibreTranslateClient:
    """Translator implementation using the LibreTranslate API."""

    _post_concurrency: threading.Semaphore | None
    _detection_concurrency: threading.Semaphore | None

    def __init__(
        self,
        api_url: str,
        src_lang: str,
        *,
        retry_count: int = 3,
        backoff_delay: float = 1.0,
        availability_check_interval: float = 30.0,
        api_key: str | None = None,
        persistent_session: bool = False,
        http_timeout: float = 180.0,
        translation_timeout: float = 3600.0,
        max_concurrent_requests: int | None = 10,
        max_concurrent_detection_requests: int | None = None,
        profiler: WorkloadProfiler | None = None,
    ) -> None:
        normalized_src = src_lang.strip().lower()
        if not normalized_src:
            raise ValueError("src_lang must be a non-empty language code")
        self.src_lang = normalized_src
        self.retry_count = retry_count
        self.backoff_delay = backoff_delay
        self.availability_check_interval = availability_check_interval
        self.api_key = api_key

        normalized_url = api_url.rstrip("/")
        if not normalized_url:
            raise ValueError("A LibreTranslate URL is required")
        self.api = LibreTranslateAPI(
            normalized_url,
            http_timeout=http_timeout,
            translation_timeout=translation_timeout,
            persistent_session=persistent_session,
        )
        self.languages: dict[str, set[str]] | None = None
        self.supported_targets: set[str] | None = None
        if isinstance(max_concurrent_requests, int) and max_concurrent_requests > 0:
            self._post_concurrency = threading.Semaphore(max_concurrent_requests)
        else:
            self._post_concurrency = None
        if isinstance(max_concurrent_detection_requests, int) and (
            max_concurrent_detection_requests > 0
        ):
            self._detection_concurrency = threading.Semaphore(
                max_concurrent_detection_requests
            )
        else:
            self._detection_concurrency = None

        self.profiler = profiler
        self._translation_profile_name = "translator.translate"
        self._detection_profile_name = "translator.detect"
        self._download_profile_name = "translator.download"

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
        resp = self._call_api_until_success(
            lambda api: api.fetch_languages(), "LibreTranslate fetch_languages"
        )
        if isinstance(resp, requests.Response):
            fetched = resp.json()
        else:
            fetched = resp
        if not isinstance(fetched, list):
            raise ValueError("Invalid languages payload")
        normalized: dict[str, set[str]] = {}
        for entry in fetched:
            if not isinstance(entry, dict):
                continue
            code = str(entry.get("code", "")).strip().lower()
            if not code:
                continue
            targets = entry.get("targets") or []
            normalized_targets: set[str] = set()
            for target in targets:
                t = str(target).strip().lower()
                if t:
                    normalized_targets.add(t)
            normalized[code] = normalized_targets
        self.languages = normalized
        if self.src_lang not in self.languages:
            logger.warning("default_source_unsupported source=%s", self.src_lang)
            self.supported_targets = set()
        else:
            self.supported_targets = self.languages[self.src_lang]
        logger.info(
            "languages_loaded sources=%d default_targets=%d",
            len(self.languages),
            len(self.supported_targets),
        )

    @contextmanager
    def _acquire_slot(self, *, detection: bool = False):
        semaphores: list[threading.Semaphore] = []
        if detection:
            if self._detection_concurrency is not None:
                semaphores.append(self._detection_concurrency)
            elif self._post_concurrency is not None:
                semaphores.append(self._post_concurrency)
        elif self._post_concurrency is not None:
            semaphores.append(self._post_concurrency)
        if not semaphores:
            yield
            return
        for semaphore in semaphores:
            semaphore.acquire()
        try:
            yield
        finally:
            for semaphore in reversed(semaphores):
                semaphore.release()

    def _profile(self, name: str):
        if not self.profiler:
            return nullcontext()
        return self.profiler.track(name)

    def _call_api_until_success(
        self,
        func: Callable[[LibreTranslateAPI], TResponse],
        context: str,
    ) -> TResponse:
        """Invoke *func* against the configured API and return the response."""
        try:
            resp = func(self.api)
        except requests.RequestException as exc:
            logger.debug(
                "api_request_failed url=%s context=%s error=%s",
                self.api.base_url,
                context,
                exc,
            )
            raise
        if isinstance(resp, requests.Response):
            self._handle_error_response(resp, context)
        return resp

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
        with self._profile(self._download_profile_name):
            download = self.api.download(download_url)
        self._handle_error_response(download, "LibreTranslate download")
        return download.content

    def _request_translation(
        self, path: Path, src_lang: str, target_lang: str
    ) -> bytes:
        """Send translation request and handle optional download flow."""
        resp = self._call_api_until_success(
            lambda api: api.translate_file(path, src_lang, target_lang, self.api_key),
            "LibreTranslate",
        )

        try:
            data = resp.json()
        except ValueError:
            return resp.content

        download_url = data.get("translatedFileUrl")
        if download_url:
            return self._retrieve_download(download_url)
        return resp.content

    def detect_language(
        self,
        text: str | bytes,
        *,
        min_confidence: float = 0.0,
    ) -> DetectionResult | None:
        """Detect the language for *text* and return the best match.

        Returns ``None`` if the sample is empty or no detection meets
        ``min_confidence``.
        """

        sample = (
            text.decode("utf-8", errors="ignore") if isinstance(text, bytes) else text
        )
        sample = sample.strip()
        if not sample:
            logger.debug("detect_skip reason=empty_sample")
            return None

        with self._acquire_slot(detection=True):
            with self._profile(self._detection_profile_name):
                resp = self._call_api_until_success(
                    lambda api: api.detect(sample), "LibreTranslate detect"
                )
        try:
            payload = resp.json()
        except ValueError as exc:  # pragma: no cover - API bug
            raise ValueError("Invalid detection response") from exc

        best: DetectionResult | None = None
        if isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                lang = str(item.get("language", "")).strip().lower()
                if not lang:
                    continue
                try:
                    confidence = float(item.get("confidence", 0.0))
                except (TypeError, ValueError):
                    continue
                normalized = self._normalize_confidence(confidence)
                candidate = DetectionResult(lang, normalized)
                if best is None or candidate.confidence > best.confidence:
                    best = candidate
        else:  # pragma: no cover - defensive
            raise ValueError("Unexpected detection payload")

        if best and best.confidence >= min_confidence:
            logger.debug(
                "detect_result language=%s confidence=%.3f",
                best.language,
                best.confidence,
            )
            return best

        logger.debug("detect_skip reason=no_match threshold=%.3f", min_confidence)
        return None

    def translate(self, path: Path, lang: str, *, src_lang: str | None = None) -> bytes:
        target = str(lang).strip().lower()
        if not target:
            raise ValueError("target language must be provided")
        source = (src_lang or self.src_lang).strip().lower()
        if not source:
            raise ValueError("source language must be provided")
        self.ensure_languages()
        if not self.supports_translation(source, target):
            raise ValueError(f"Unsupported translation {source} -> {target}")
        attempt = 0
        while True:
            attempt += 1
            try:
                with self._profile(self._translation_profile_name):
                    with self._acquire_slot():
                        return self._request_translation(path, source, target)
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

    def supported_targets_for(self, src_lang: str) -> set[str]:
        normalized = str(src_lang).strip().lower()
        self.ensure_languages()
        return self.languages.get(normalized, set()) if self.languages else set()

    def supports_translation(self, src_lang: str, target_lang: str) -> bool:
        normalized_target = str(target_lang).strip().lower()
        if not normalized_target:
            return False
        targets = self.supported_targets_for(src_lang)
        return normalized_target in targets

    def is_target_supported(self, target_lang: str) -> bool:
        normalized = str(target_lang).strip().lower()
        if not normalized:
            return False
        self.ensure_languages()
        if not self.languages:
            return False
        return any(normalized in targets for targets in self.languages.values())

    def close(self) -> None:
        self.api.close()

    @staticmethod
    def _normalize_confidence(raw: float) -> float:
        """Normalize API confidence to the 0..1 range."""

        if raw < 0.0:
            return 0.0
        if raw > 1.0:
            raw = raw / 100.0 if raw <= 100.0 else 1.0
        return min(raw, 1.0)
