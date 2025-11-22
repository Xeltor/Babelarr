from __future__ import annotations

import json
import socket
import urllib.request

import pytest

from babelarr.profiling import WorkloadProfiler
from babelarr.profiling_ui import ProfilingDashboard, ProfilingHandler


def _find_open_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_profiling_dashboard_serves_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    profiler = WorkloadProfiler(enabled=True)
    with profiler.track("demo"):
        pass
    dashboard = ProfilingDashboard(profiler, host="127.0.0.1", port=_find_open_port())
    dashboard.register_status_provider("queue", lambda: {"size": 1})

    dashboard.start()
    assert dashboard._server is not None
    base_url = f"http://{dashboard.host}:{dashboard._server.server_port}"

    with urllib.request.urlopen(base_url + "/metrics", timeout=5) as resp:
        assert resp.status == 200
        payload = json.loads(resp.read())
        assert "timings" in payload and "demo" in payload["timings"]
        assert payload["status"]["queue"]["size"] == 1

    with urllib.request.urlopen(base_url + "/", timeout=5) as resp:
        assert resp.status == 200
        body = resp.read().decode()
        assert "Profiling Dashboard" in body

    dashboard.stop()


def test_profiling_handler_collects_status_errors() -> None:
    calls = []

    def bad_provider() -> dict[str, object]:
        calls.append("called")
        raise RuntimeError("boom")

    handler_cls = type(
        "TestHandler",
        (ProfilingHandler,),
        {"profiler": None, "status_providers": [("bad", bad_provider)]},
    )

    collected = handler_cls._collect_status(handler_cls)  # type: ignore[arg-type]

    assert collected == []
    assert calls == ["called"]
