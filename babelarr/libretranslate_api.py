"""Thin wrapper around the LibreTranslate HTTP API."""

from __future__ import annotations

from pathlib import Path

import requests


class LibreTranslateAPI:
    """HTTP helper for a single LibreTranslate endpoint.

    The client maintains a single :class:`requests.Session` shared across all
    requests to ``base_url``.
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

    def fetch_languages(self) -> list[dict]:
        """Return the languages supported by the server."""

        url = self.base_url + "/languages"
        resp = self.session.get(url, timeout=60)
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
            return self.session.post(url, files=files, data=data, timeout=60)

    def download(self, url: str) -> requests.Response:
        """Download *url* using the shared session."""

        return self.session.get(url, timeout=60)

    async def close(self) -> None:
        """Asynchronously close the underlying session."""

        self.session.close()
