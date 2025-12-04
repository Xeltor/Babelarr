"""Helpers for rendering the profiling dashboard."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from .profiling import WorkloadProfiler

logger = logging.getLogger(__name__)


class ProfilingDashboard:
    """Helper that keeps track of status providers and renders dashboard payloads."""

    def __init__(self, profiler: WorkloadProfiler) -> None:
        self.profiler = profiler
        self._status_providers: list[tuple[str, Callable[[], dict[str, object]]]] = []

    def register_status_provider(
        self, name: str, provider: Callable[[], dict[str, object]]
    ) -> None:
        self._status_providers.append((name, provider))

    def _collect_status(
        self,
    ) -> list[tuple[str, dict[str, object]]]:
        collected: list[tuple[str, dict[str, object]]] = []
        for name, provider in self._status_providers:
            try:
                data = provider() or {}
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("profiling_status_failed source=%s error=%s", name, exc)
                continue
            if not isinstance(data, dict):
                continue
            collected.append((name, data))
        return collected

    def render_page(self) -> str:
        metrics = self.profiler.metrics() if self.profiler else {}
        status = self._collect_status()
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
        status_sections = []
        for name, data in status:
            status_rows = "".join(
                f"<tr><td>{key}</td><td>{value}</td></tr>"
                for key, value in sorted(data.items())
            )
            empty_rows = "<tr><td colspan='2'>empty</td></tr>"
            status_sections.append(
                f"<section><h2>{name}</h2>"
                "<table><thead><tr><th>key</th><th>value</th></tr></thead>"
                f"<tbody>{status_rows or empty_rows}</tbody>"
                "</table></section>"
            )
        return (
            "<!DOCTYPE html><html><head><meta charset='UTF-8'><title>Profiling Dashboard</title>"
            "<style>body{font-family:system-ui,monospace;padding:1rem;}table{border-collapse:collapse;width:100%;}"
            "th,td{border:1px solid #aaa;padding:.4rem;text-align:right;}th{text-align:left;}"
            "tbody tr:nth-child(odd){background:#f7f7f7;} section{margin-top:1rem;}</style>"
            "<meta http-equiv='refresh' content='5'></head><body>"
            "<h1>Profiling Dashboard</h1>"
            f"{header}"
            "<section>"
            "</section>"
            f"{table if rows else '<p>no metrics collected yet</p>'}"
            f"{''.join(status_sections)}"
            "<p>JSON metrics feed: <a href='/metrics'>/metrics</a></p>"
            "</body></html>"
        )

    def metrics_payload(self) -> dict[str, object]:
        status = self._collect_status()
        return {
            "timings": self.profiler.metrics() if self.profiler else {},
            "status": {name: data for name, data in status},
        }
