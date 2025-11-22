"""Simple timing profiler for translation and scan workloads."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class TimingStats:
    """Aggregated timing statistics for a single metric."""

    count: int = 0
    total: float = 0.0
    maximum: float = 0.0
    minimum: float = float("inf")

    def record(self, duration: float) -> None:
        self.count += 1
        self.total += duration
        self.maximum = max(self.maximum, duration)
        self.minimum = min(self.minimum, duration)

    def average(self) -> float:
        return self.total / self.count if self.count else 0.0


class WorkloadProfiler:
    """Collect timing data for named events."""

    def __init__(self, enabled: bool = False, sample_limit: int = 256) -> None:
        self.enabled = enabled
        self.sample_limit = max(8, sample_limit)
        self._lock = threading.Lock()
        self._stats: dict[str, TimingStats] = {}
        self._samples: dict[str, deque[float]] = {}

    def record(self, name: str, duration: float) -> None:
        if not self.enabled:
            return
        if duration < 0:
            logger.debug("ignore_negative_duration name=%s duration=%s", name, duration)
            return
        with self._lock:
            stats = self._stats.get(name)
            if stats is None:
                stats = TimingStats()
                self._stats[name] = stats
            stats.record(duration)
            samples = self._samples.get(name)
            if samples is None:
                samples = deque(maxlen=self.sample_limit)
                self._samples[name] = samples
            samples.append(duration)

    def _snapshot(self):
        with self._lock:
            stats_copy = {
                name: TimingStats(
                    count=stats.count,
                    total=stats.total,
                    maximum=stats.maximum,
                    minimum=stats.minimum,
                )
                for name, stats in self._stats.items()
            }
            samples_copy = {
                name: list(samples) for name, samples in self._samples.items()
            }
        return stats_copy, samples_copy

    @staticmethod
    def _percentile(values: Iterable[float], percentile: float) -> float:
        values = [value for value in values]
        if not values:
            return 0.0
        values.sort()
        k = max(
            0, min(len(values) - 1, math.ceil((percentile / 100) * len(values)) - 1)
        )
        return values[k]

    def metrics(self) -> dict[str, dict[str, float]]:
        stats, samples = self._snapshot()
        metrics: dict[str, dict[str, float]] = {}
        for name, stat in stats.items():
            values = samples.get(name, [])
            metrics[name] = {
                "count": stat.count,
                "total": stat.total,
                "average": stat.average(),
                "min": stat.minimum if stat.minimum != float("inf") else 0.0,
                "max": stat.maximum,
                "p1": self._percentile(values, 1),
                "p5": self._percentile(values, 5),
                "p50": self._percentile(values, 50),
                "p95": self._percentile(values, 95),
                "p99": self._percentile(values, 99),
            }
        return metrics

    def report_lines(self) -> list[str]:
        metrics = self.metrics()
        if not metrics:
            return []
        lines: list[str] = []
        for name, stats in sorted(metrics.items()):
            lines.append(
                "%s count=%d avg=%.3fs max=%.3fs min=%.3fs total=%.3fs"
                % (
                    name,
                    stats["count"],
                    stats["average"],
                    stats["max"],
                    stats["min"],
                    stats["total"],
                )
            )
        return lines

    @contextmanager
    def track(self, name: str):
        if not self.enabled:
            yield
            return
        start = time.monotonic()
        try:
            yield
        finally:
            duration = time.monotonic() - start
            self.record(name, duration)
