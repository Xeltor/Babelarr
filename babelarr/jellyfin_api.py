from __future__ import annotations

from pathlib import Path

import requests


class JellyfinClient:
    """Minimal client for Jellyfin refresh API."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def refresh_path(self, path: Path) -> None:
        """Notify Jellyfin that *path* has been updated."""

        url = self.base_url + "/Library/Media/Updated"
        payload = {"Updates": [{"Path": str(path)}]}
        headers = {"X-Emby-Token": self.token}
        resp = requests.post(url, json=payload, headers=headers, timeout=900)
        resp.raise_for_status()
