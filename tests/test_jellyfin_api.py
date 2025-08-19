from pathlib import Path

import requests

from babelarr.jellyfin_api import JellyfinClient


def test_refresh_path(monkeypatch):
    calls: dict[str, object] = {}

    def fake_post(url, *, json=None, headers=None, timeout):
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
