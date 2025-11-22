from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import requests

from babelarr.jellyfin_api import JellyfinClient

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def test_refresh_path(monkeypatch: MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def fake_post(
        url: str,
        *,
        json: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int,
    ) -> requests.Response:
        calls["url"] = url
        calls["json"] = json
        calls["headers"] = headers
        calls["timeout"] = timeout
        resp = requests.Response()
        resp.status_code = 204
        return resp

    monkeypatch.setattr(requests, "post", fake_post)

    client = JellyfinClient("http://jf", "tok")
    client.refresh_path(Path("/data/file.srt"))

    assert calls["url"] == "http://jf/Library/Media/Updated"
    assert calls["json"] == {"Updates": [{"Path": "/data/file.srt"}]}
    assert calls["headers"] == {"X-Emby-Token": "tok"}
    assert calls["timeout"] == 30
