"""Thin wrapper around the LibreTranslate HTTP API."""

from __future__ import annotations

import threading
from pathlib import Path

import requests


class LibreTranslateAPI:
    """HTTP helper for a single LibreTranslate endpoint.

    The client maintains a *per-thread* :class:`requests.Session` for all
    requests to ``base_url`` when ``persistent_session`` is ``True``. By
    default, a fresh connection is created for each request to avoid sticky
    connections when multiple workers are used behind a load balancer.
    """

    def __init__(
        self,
        base_url: str,
        *,
        http_timeout: float = 30.0,
        translation_timeout: float = 900.0,
        persistent_session: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.http_timeout = http_timeout
        self.translation_timeout = translation_timeout
        self._local = threading.local()
        self.persistent_session = persistent_session

    def _get_session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            self._local.session = session
        return session

    @property
    def session(self) -> requests.Session:
        """Return the thread-local :class:`requests.Session`."""

        return self._get_session()

    def fetch_languages(self) -> list[dict]:
        """Return the languages supported by the server."""

        url = self.base_url + "/languages"
        resp = self.session.get(url, timeout=self.http_timeout)
        resp.raise_for_status()
        return resp.json()

    def translate_file(
        self,
        path: Path,
        src_lang: str,
        target_lang: str,
        api_key: str | None = None,
    ) -> requests.Response:
        """Translate *path* from *src_lang* into *target_lang*."""

        data = {"source": src_lang, "target": target_lang, "format": "srt"}
        if api_key:
            data["api_key"] = api_key

        url = self.base_url + "/translate_file"
        with open(path, "rb") as fh:
            files = {"file": fh}
            if self.persistent_session:
                return self.session.post(
                    url, files=files, data=data, timeout=self.translation_timeout
                )
            headers = {"Connection": "close"}
            return requests.post(
                url,
                files=files,
                data=data,
                timeout=self.translation_timeout,
                headers=headers,
            )

    def download(self, url: str) -> requests.Response:
        """Download *url* using a fresh connection by default."""

        if self.persistent_session:
            return self.session.get(url, timeout=self.http_timeout)
        headers = {"Connection": "close"}
        return requests.get(url, timeout=self.http_timeout, headers=headers)

    async def close(self) -> None:
        """Asynchronously close the thread-local session for this thread."""

        session = getattr(self._local, "session", None)
        if session:
            session.close()
