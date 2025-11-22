from __future__ import annotations

import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import Application

logger = logging.getLogger(__name__)


class TdarrWebhookServer:
    """Minimal HTTP server that lets Tdarr trigger MKV processing."""

    def __init__(
        self,
        app: Application,
        host: str,
        port: int,
        token: str | None = None,
    ) -> None:
        self.app = app
        self.host = host
        self.port = port
        self.token = token
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def server_port(self) -> int | None:
        if not self._server:
            return None
        return self._server.server_port

    def start(self) -> None:
        if self.port < 0:
            return
        handler = self._make_handler()
        try:
            server = ThreadingHTTPServer((self.host, self.port), handler)
        except OSError as exc:
            logger.warning(
                "tdarr_webhook_listen_failed host=%s port=%s error=%s",
                self.host,
                self.port,
                exc,
            )
            return
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        logger.info(
            "tdarr_webhook_started host=%s port=%s", self.host, server.server_port
        )

    def stop(self) -> None:
        if not self._server:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=3)
        logger.info(
            "tdarr_webhook_stopped host=%s port=%s",
            self.host,
            self._server.server_port,
        )
        self._server = None
        self._thread = None

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        app = self.app
        token = self.token

        class TdarrWebhookHandler(BaseHTTPRequestHandler):
            server_version = "BabelarrTdarrWebhook/1.0"

            def log_message(
                self, format: str, *args: object
            ) -> None:  # pragma: no cover - noisy
                return

            def do_POST(self) -> None:
                if self.path not in ("/webhook/tdarr", "/tdarr"):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if not self._authorized():
                    return
                payload = self._parse_json()
                if payload is None:
                    return
                paths = self._parse_paths(payload)
                if not paths:
                    self._json_response(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "missing path", "queued": [], "skipped": []},
                    )
                    return
                priority = self._parse_priority(payload)
                accepted, skipped = app.enqueue_webhook_paths(paths, priority=priority)
                body = {
                    "queued": [str(p) for p in accepted],
                    "skipped": [
                        {"path": str(path), "reason": reason}
                        for path, reason in skipped
                    ],
                    "priority": priority,
                }
                status = HTTPStatus.ACCEPTED if accepted else HTTPStatus.OK
                logger.info(
                    "tdarr_webhook_received total=%d queued=%d priority=%d",
                    len(paths),
                    len(accepted),
                    priority,
                )
                self._json_response(status, body)

            def _parse_json(self) -> dict[str, object] | None:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._json_response(
                        HTTPStatus.BAD_REQUEST, {"error": "invalid content-length"}
                    )
                    return None
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    parsed = json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError:
                    self._json_response(
                        HTTPStatus.BAD_REQUEST, {"error": "invalid json"}
                    )
                    return None
                if not isinstance(parsed, dict):
                    self._json_response(
                        HTTPStatus.BAD_REQUEST, {"error": "expected JSON object"}
                    )
                    return None
                return parsed

            def _parse_paths(self, payload: dict[str, object]) -> list[Path]:
                paths: list[Path] = []
                raw_path = payload.get("path")
                if isinstance(raw_path, str):
                    paths.append(Path(raw_path))
                raw_paths = payload.get("paths")
                if isinstance(raw_paths, list):
                    for item in raw_paths:
                        if isinstance(item, str):
                            paths.append(Path(item))
                deduped: list[Path] = []
                seen: set[str] = set()
                for path in paths:
                    key = str(path)
                    if key in seen:
                        continue
                    seen.add(key)
                    deduped.append(path)
                return deduped

            def _parse_priority(self, payload: dict[str, object]) -> int:
                raw = payload.get("priority")
                if isinstance(raw, bool):
                    priority = 0
                elif isinstance(raw, (int, str)):
                    try:
                        priority = int(raw)
                    except ValueError:
                        priority = 0
                else:
                    priority = 0
                return 0 if priority <= 0 else 1

            def _authorized(self) -> bool:
                if not token:
                    return True
                provided = self.headers.get("Authorization")
                if provided and provided.lower().startswith("bearer "):
                    provided = provided.split(" ", 1)[1].strip()
                else:
                    provided = self.headers.get("X-Webhook-Token") or self.headers.get(
                        "X-Babelarr-Token"
                    )
                if provided != token:
                    self._json_response(
                        HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"}
                    )
                    return False
                return True

            def _json_response(self, status: int, payload: dict[str, object]) -> None:
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return TdarrWebhookHandler
