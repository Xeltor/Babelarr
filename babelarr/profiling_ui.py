"""Minimal HTTP dashboard exposing profiler metrics."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .profiling import WorkloadProfiler

logger = logging.getLogger(__name__)


class ProfilingHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler that renders profiler metrics."""

    profiler: WorkloadProfiler | None = None

    def log_message(self, format: str, *args) -> None:  # pragma: no cover - noise
        return

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            content = self._render_page()
            self._write_response(200, content.encode("utf-8"), "text/html")
            return
        if self.path == "/metrics":
            payload = self.profiler.metrics() if self.profiler else {}
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self._write_response(200, body, "application/json")
            return
        self.send_error(404, "Not Found")

    def _render_page(self) -> str:
        metrics = self.profiler.metrics() if self.profiler else {}
        header = f"<p>Last updated {datetime.utcnow().isoformat()} UTC</p>"
        rows = []
        for name in sorted(metrics):
            metric = metrics[name]
            rows.append(
                "<tr>"
                f"<td>{name}</td>"
                f"<td>{metric['count']}</td>"
                f"<td>{metric['average']:.3f}</td>"
                f"<td>{metric['min']:.3f}</td>"
                f"<td>{metric['max']:.3f}</td>"
                f"<td>{metric['total']:.3f}</td>"
                f"<td>{metric['p1']:.3f}</td>"
                f"<td>{metric['p5']:.3f}</td>"
                f"<td>{metric['p50']:.3f}</td>"
                f"<td>{metric['p95']:.3f}</td>"
                f"<td>{metric['p99']:.3f}</td>"
                "</tr>"
        )
        table = (
            "<table>"
            "<thead><tr>"
            "<th>Metric</th><th>Count</th><th>Avg</th><th>Min</th><th>Max</th><th>Total</th>"
            "<th>P1</th><th>P5</th><th>P50</th><th>P95</th><th>P99</th>"
            "</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody>"
            "</table>"
        )
        return (
            "<!DOCTYPE html><html><head><meta charset='UTF-8'><title>Profiling Dashboard</title>"
            "<style>body{font-family:system-ui,monospace;padding:1rem;}table{border-collapse:collapse;width:100%;}"
            "th,td{border:1px solid #aaa;padding:.4rem;text-align:right;}th{text-align:left;}"
            "tbody tr:nth-child(odd){background:#f7f7f7;}</style>"
            "<meta http-equiv='refresh' content='5'></head><body>"
            "<h1>Profiling Dashboard</h1>"
            f"{header}"
            "<section>"
            "</section>"
            f"{table if rows else '<p>no metrics collected yet</p>'}"
            "<p>JSON metrics feed: <a href='/metrics'>/metrics</a></p>"
            "</body></html>"
        )

    def _write_response(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ProfilingDashboard:
    """Threaded HTTP server for profiler metrics."""

    def __init__(
        self,
        profiler: WorkloadProfiler,
        host: str = "0.0.0.0",
        port: int | None = None,
    ):
        self.profiler = profiler
        self.host = host
        self.port = port or 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.profiler.enabled or not self.port:
            return
        handler = self._make_handler()
        try:
            server = ThreadingHTTPServer((self.host, self.port), handler)
        except OSError as exc:
            logger.warning(
                "profiling_ui_listen_failed host=%s port=%s error=%s",
                self.host,
                self.port,
                exc,
            )
            return
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        logger.info("profiling_ui_started host=%s port=%s", self.host, server.server_port)

    def stop(self) -> None:
        if not self._server:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=3)
        logger.info("profiling_ui_stopped host=%s port=%s", self.host, self._server.server_port)
        self._server = None
        self._thread = None

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        handler = type(
            "ProfilingRequestHandler",
            (ProfilingHandler,),
            {"profiler": self.profiler},
        )
        return handler
