from __future__ import annotations

import logging
import socket
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .profiling_ui import ProfilingDashboard

if TYPE_CHECKING:
    from .app import Application

logger = logging.getLogger(__name__)


class _WebhookPayload(BaseModel):
    path: str | None = None
    paths: list[str] | None = None
    priority: bool | int | None = None


def _gather_paths(payload: _WebhookPayload) -> list[Path]:
    collected: list[Path] = []
    if payload.path:
        collected.append(Path(payload.path))
    if payload.paths:
        collected.extend(Path(item) for item in payload.paths)
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in collected:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _normalize_webhook_priority(raw_priority: bool | int | None) -> int:
    if isinstance(raw_priority, bool):
        return 0 if raw_priority else 1
    if raw_priority is None:
        return 0
    return 0 if raw_priority <= 0 else 1


class BabelarrWebServer:
    """FastAPI-backed server for dashboards, metrics, webhook, and docs."""

    def __init__(
        self,
        app: "Application",
        dashboard: ProfilingDashboard | None,
        host: str,
        port: int,
    ) -> None:
        self._application = app
        self._dashboard = dashboard
        self.host = host
        self.port = port
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._actual_port: int | None = None

    @property
    def server_port(self) -> int | None:
        return self._actual_port

    def start(self) -> None:
        app = self._create_app()
        port = self.port if self.port > 0 else self._find_open_port(self.host)
        self._actual_port = port
        config = uvicorn.Config(
            app,
            host=self.host,
            port=port,
            log_level="warning",
            loop="asyncio",
            lifespan="on",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._run, args=(self._server,), daemon=True
        )
        self._thread.start()
        start_time = time.monotonic()
        while self._server.started is False and not self._server.should_exit:
            if time.monotonic() - start_time > 5:
                break
            time.sleep(0.01)

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)
        self._thread = None
        self._server = None
        self._actual_port = None

    def _run(self, server: uvicorn.Server) -> None:
        server.run()

    def _find_open_port(self, host: str) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, 0))
            return sock.getsockname()[1]

    def _create_app(self) -> FastAPI:
        app = FastAPI(
            title="Babelarr HTTP surface",
            version="1.0.0",
            description="Profiling dashboard, metrics, Tdarr webhook, and docs.",
            openapi_url="/openapi.json",
            docs_url="/docs",
            redoc_url=None,
        )

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        def dashboard() -> HTMLResponse:
            if not self._dashboard:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not found")
            return HTMLResponse(self._dashboard.render_page())

        @app.get("/metrics", include_in_schema=False)
        def metrics() -> JSONResponse:
            if not self._dashboard:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not found")
            payload = self._dashboard.metrics_payload()
            return JSONResponse(payload)

        @app.post("/webhook/tdarr")
        def webhook(payload: _WebhookPayload) -> JSONResponse:
            paths = _gather_paths(payload)
            if not paths:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail="missing path",
                )
            priority = _normalize_webhook_priority(payload.priority)
            accepted, skipped = self._application.enqueue_webhook_paths(
                paths, priority=priority
            )
            body = {
                "queued": [str(p) for p in accepted],
                "skipped": [
                    {"path": str(p), "reason": reason} for p, reason in skipped
                ],
                "priority": priority,
            }
            status_code = status.HTTP_202_ACCEPTED if accepted else status.HTTP_200_OK
            logger.info(
                "tdarr_webhook_received total=%d queued=%d priority=%d",
                len(paths),
                len(accepted),
                priority,
            )
            return JSONResponse(body, status_code=status_code)

        return app
