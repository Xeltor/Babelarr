from __future__ import annotations

import http.client
import json
from pathlib import Path

from babelarr.config import Config
from babelarr.tdarr_webhook import TdarrWebhookServer


class _WorkflowRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, int]] = []

    def enqueue_translation(
        self,
        path: Path,
        priority: int = 1,
        *,
        position: int | None = None,
        total_paths: int | None = None,
    ) -> None:
        self.calls.append((path, priority))


def test_enqueue_webhook_paths_filters_and_prioritizes(tmp_path: Path, app) -> None:
    mkv_dir = tmp_path / "mkv"
    mkv_dir.mkdir()
    valid = mkv_dir / "movie.mkv"
    valid.write_bytes(b"ok")
    ignored = mkv_dir / "ignored.srt"
    ignored.write_bytes(b"skip")
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"skip")

    cfg = Config(
        root_dirs=[str(mkv_dir)],
        api_url="http://example",
        workers=1,
        ensure_langs=["en"],
        retry_count=1,
        backoff_delay=0,
        mkv_dirs=[str(mkv_dir)],
    )
    workflow = _WorkflowRecorder()
    application = app(cfg=cfg)
    application.workflow = workflow  # type: ignore[assignment]

    queued, skipped = application.enqueue_webhook_paths(
        [valid, ignored, outside], priority=0
    )

    assert queued == [valid.resolve()]
    assert (ignored.resolve(), "not_mkv") in skipped
    assert (outside.resolve(), "outside_mkv_dirs") in skipped
    assert workflow.calls == [(valid.resolve(), 0)]


def test_tdarr_webhook_server_accepts_request(tmp_path: Path, app) -> None:
    mkv_dir = tmp_path / "mkv"
    mkv_dir.mkdir()
    target = mkv_dir / "movie.mkv"
    target.write_bytes(b"ok")

    cfg = Config(
        root_dirs=[str(mkv_dir)],
        api_url="http://example",
        workers=1,
        ensure_langs=["en"],
        retry_count=1,
        backoff_delay=0,
        mkv_dirs=[str(mkv_dir)],
        webhook_host="127.0.0.1",
        webhook_port=0,
        webhook_token="secret",
    )
    workflow = _WorkflowRecorder()
    application = app(cfg=cfg)
    application.workflow = workflow  # type: ignore[assignment]

    server = TdarrWebhookServer(
        application, cfg.webhook_host, cfg.webhook_port, token=cfg.webhook_token
    )
    server.start()
    try:
        port = server.server_port
        assert port
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        payload = json.dumps({"path": str(target)})
        conn.request(
            "POST",
            "/webhook/tdarr",
            body=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer secret",
            },
        )
        response = conn.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        assert response.status == 202
        assert body["queued"] == [str(target.resolve())]
        assert workflow.calls == [(target.resolve(), 0)]
    finally:
        server.stop()
