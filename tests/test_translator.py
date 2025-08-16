import asyncio
import threading

import requests

from babelarr.libretranslate_api import LibreTranslateAPI


def test_translate_file_thread_safety(monkeypatch, tmp_path):
    tmp_file = tmp_path / "a.srt"
    tmp_file.write_text("dummy")

    sessions: dict[int, requests.Session] = {}
    lock = threading.Lock()

    def fake_post(self, url, *, files=None, data=None, timeout=60):
        with lock:
            sessions[id(threading.current_thread())] = self
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"ok"
        return resp

    monkeypatch.setattr(requests.Session, "post", fake_post)

    api = LibreTranslateAPI("http://only")

    results: list[bytes] = []
    errors: list[Exception] = []

    def worker():
        try:
            resp = api.translate_file(tmp_file, "en", "nl")
            results.append(resp.content)
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert results == [b"ok"] * 5
    assert len({id(s) for s in sessions.values()}) == 5

    asyncio.run(api.close())
