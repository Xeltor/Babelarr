from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_CPU_CORES = 4
MIN_CPU_CORES = 1


def clamp(value: int, minimum: int, maximum: int) -> int:
    """Bound value between minimum and maximum inclusive."""
    return max(minimum, min(value, maximum))


@dataclass(frozen=True)
class ConcurrencyConfig:
    cpu_cores: int
    workers: int
    argos_inter_threads: int
    lt_threads: int

    @property
    def argos_intra_threads(self) -> int:
        return 0


def parse_cpu_cores(raw: str | None) -> int:
    """Parse CPU_CORES into an integer with sane defaults."""
    if not raw:
        return DEFAULT_CPU_CORES
    cleaned = raw.strip()
    try:
        parsed = int(cleaned)
    except ValueError:
        logger.warning(
            "use default %s for invalid CPU_CORES '%s'",
            DEFAULT_CPU_CORES,
            cleaned,
        )
        return DEFAULT_CPU_CORES
    return max(MIN_CPU_CORES, parsed)


def derive_concurrency(cpu_cores: int) -> ConcurrencyConfig:
    cores = max(MIN_CPU_CORES, cpu_cores)
    workers = clamp(cores // 4, 1, 8)
    lt_threads = clamp(cores + 4, 8, 32)
    return ConcurrencyConfig(
        cpu_cores=cores,
        workers=workers,
        argos_inter_threads=workers,
        lt_threads=lt_threads,
    )


def from_env(raw: str | None) -> ConcurrencyConfig:
    """Convenience helper for deriving concurrency from CPU_CORES env."""
    cpu = parse_cpu_cores(raw)
    return derive_concurrency(cpu)
