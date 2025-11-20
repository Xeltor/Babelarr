import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Config:
    """Application configuration.

    Attributes:
        root_dirs: Legacy list of watch directories (mirrors mkv_dirs for compatibility).
        ensure_langs: Ordered languages that should exist for each MKV.
        api_url: Base URL of the translation API.
        workers: Number of translation worker threads.
        api_key: Optional API key for authenticated requests.
        jellyfin_url: Base URL of the Jellyfin server.
        jellyfin_token: API token for Jellyfin.
        retry_count: Translation retry attempts.
        backoff_delay: Initial backoff delay between retries.
        debounce: Seconds to wait for file changes to settle before enqueueing.
        stabilize_timeout: Max seconds to wait for file size to stabilize.
        scan_interval_minutes: Interval between periodic full scans.
        mkv_scan_interval_minutes: Interval between MKV rescans.
        mkv_min_confidence: Minimum confidence required for tagging.
        mkv_cache_path: Path to persisted MKV processing state.
        mkv_dirs: Directories to scan/watch for MKV files.
    """

    root_dirs: list[str]
    api_url: str
    workers: int
    ensure_langs: list[str]
    api_key: str | None = None
    jellyfin_url: str | None = None
    jellyfin_token: str | None = None
    retry_count: int = 3
    backoff_delay: float = 1.0
    availability_check_interval: float = 30.0
    debounce: float = 0.1
    stabilize_timeout: float = 30.0
    scan_interval_minutes: int = 60
    http_timeout: float = 180.0
    translation_timeout: float = 3600.0
    libretranslate_max_concurrent_requests: int = 10
    libretranslate_max_concurrent_detection_requests: int | None = 2
    persistent_sessions: bool = False
    mkv_scan_interval_minutes: int = 180
    mkv_min_confidence: float = 0.85
    mkv_cache_path: str = "/config/cache.db"
    mkv_dirs: list[str] | None = None
    mkv_cache_enabled: bool = True
    mkv_temp_dir: str = "/tmp/libretranslate-files-translate"
    profiling_enabled: bool = False
    profiling_ui_host: str = "0.0.0.0"
    profiling_ui_port: int = 0

    @staticmethod
    def _parse_ensure_langs(raw: str | None, default: list[str]) -> list[str]:
        raw_langs = raw.split(",") if raw is not None else list(default)
        ensure_langs: list[str] = []
        seen: set[str] = set()
        for lang in raw_langs:
            cleaned = lang.strip()
            if not cleaned:
                logger.warning("ignore empty language code in ENSURE_LANGS")
                continue
            if not cleaned.isalpha():
                logger.warning(
                    "ignore invalid language code '%s' in ENSURE_LANGS", cleaned
                )
                continue
            normalized = cleaned.lower()
            if normalized in seen:
                logger.debug(
                    "ignore duplicate language code '%s' in ENSURE_LANGS", cleaned
                )
                continue
            ensure_langs.append(normalized)
            seen.add(normalized)
        if not ensure_langs:
            logger.error("found no valid languages in ENSURE_LANGS")
            raise ValueError(
                "ENSURE_LANGS must contain at least one valid language code",
            )
        return ensure_langs

    @staticmethod
    def _parse_workers(raw: str | None) -> int:
        MAX_WORKERS = 10
        default_workers = 1
        raw_workers = raw or str(default_workers)
        try:
            requested = int(raw_workers)
        except ValueError:
            logger.warning(
                "use default %s for invalid WORKERS '%s'", default_workers, raw_workers
            )
            requested = default_workers
        workers = min(requested, MAX_WORKERS)
        if requested > MAX_WORKERS:
            logger.warning(
                "cap workers at %s to prevent instability (requested %s)",
                MAX_WORKERS,
                requested,
            )
        return workers

    @staticmethod
    def _parse_int(name: str, raw: str | None, default: int) -> int:
        raw_val = raw or str(default)
        try:
            return int(raw_val)
        except ValueError:
            logger.warning(
                "use default %s for invalid %s '%s'",
                default,
                name,
                raw_val,
            )
            return default

    @staticmethod
    def _parse_float(name: str, raw: str | None, default: float) -> float:
        raw_val = raw or str(default)
        try:
            return float(raw_val)
        except ValueError:
            logger.warning(
                "use default %s for invalid %s '%s'",
                default,
                name,
                raw_val,
            )
            return default

    @staticmethod
    def _parse_bool(name: str, raw: str | None, default: bool) -> bool:
        raw_val = raw or str(default)
        if isinstance(raw_val, str):
            lowered = raw_val.lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
        logger.warning(
            "use default %s for invalid %s '%s'",
            default,
            name,
            raw_val,
        )
        return default

    @staticmethod
    def _parse_detection_concurrency(
        name: str, raw: str | None, default: int | None
    ) -> int | None:
        if raw is None:
            return default
        try:
            parsed = int(raw)
        except ValueError:
            logger.warning(
                "use default %s for invalid %s '%s'",
                default,
                name,
                raw,
            )
            return default
        if parsed <= 0:
            return None
        return parsed

    @classmethod
    def from_env(cls) -> "Config":
        api_url = os.environ.get("LIBRETRANSLATE_URL", "http://libretranslate:5000")
        ensure_langs = cls._parse_ensure_langs(
            os.environ.get("ENSURE_LANGS"),
            default=["en", "nl", "bs"],
        )

        default_mkv_cache = Path("/config/cache.db")
        mkv_cache_raw = os.environ.get("MKV_CACHE_PATH")
        mkv_cache_path = Path(mkv_cache_raw) if mkv_cache_raw else default_mkv_cache
        try:
            mkv_cache_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "mkv_cache_dir_unavailable path=%s error=%s",
                mkv_cache_path.parent,
                exc,
            )
        mkv_dirs_raw = os.environ.get("MKV_DIRS") or os.environ.get("WATCH_DIRS", "/data")
        mkv_dirs = [p for p in mkv_dirs_raw.split(":") if p]
        root_dirs = list(mkv_dirs)
        mkv_temp_dir = os.environ.get("MKV_TEMP_DIR", "/tmp/libretranslate-files-translate")
        try:
            Path(mkv_temp_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "mkv_temp_dir_unavailable path=%s error=%s",
                mkv_temp_dir,
                exc,
            )

        api_key = os.environ.get("LIBRETRANSLATE_API_KEY") or None
        jellyfin_url = os.environ.get("JELLYFIN_URL") or None
        jellyfin_token = os.environ.get("JELLYFIN_TOKEN") or None

        parsers: dict[str, Callable[[str | None], Any]] = {
            "WORKERS": cls._parse_workers,
            "LIBRETRANSLATE_MAX_CONCURRENT_REQUESTS": lambda v: cls._parse_int(
                "LIBRETRANSLATE_MAX_CONCURRENT_REQUESTS", v, 10
            ),
            "LIBRETRANSLATE_MAX_CONCURRENT_DETECTION_REQUESTS": lambda v: cls._parse_detection_concurrency(
                "LIBRETRANSLATE_MAX_CONCURRENT_DETECTION_REQUESTS",
                v,
                2,
            ),
            "PERSISTENT_SESSIONS": lambda v: cls._parse_bool(
                "PERSISTENT_SESSIONS", v, False
            ),
            "MKV_SCAN_INTERVAL_MINUTES": lambda v: cls._parse_int(
                "MKV_SCAN_INTERVAL_MINUTES", v, 180
            ),
            "MKV_MIN_CONFIDENCE": lambda v: cls._parse_float(
                "MKV_MIN_CONFIDENCE", v, 0.85
            ),
            "MKV_CACHE_ENABLED": lambda v: cls._parse_bool(
                "MKV_CACHE_ENABLED", v, True
            ),
            "PROFILING_ENABLED": lambda v: cls._parse_bool(
                "PROFILING_ENABLED", v, False
            ),
            "PROFILING_UI_HOST": lambda v: v or "0.0.0.0",
            "PROFILING_UI_PORT": lambda v: cls._parse_int(
                "PROFILING_UI_PORT", v, 0
            ),
        }

        parsed = {
            name: parser(os.environ.get(name)) for name, parser in parsers.items()
        }

        workers = parsed["WORKERS"]
        retry_count = cls.retry_count
        backoff_delay = cls.backoff_delay
        availability_check_interval = cls.availability_check_interval
        debounce = cls.debounce
        stabilize_timeout = cls.stabilize_timeout
        scan_interval_minutes = cls.scan_interval_minutes
        http_timeout = cls.http_timeout
        translation_timeout = cls.translation_timeout
        libretranslate_max_concurrent_requests = parsed[
            "LIBRETRANSLATE_MAX_CONCURRENT_REQUESTS"
        ]
        libretranslate_max_concurrent_detection_requests = parsed[
            "LIBRETRANSLATE_MAX_CONCURRENT_DETECTION_REQUESTS"
        ]
        persistent_sessions = parsed["PERSISTENT_SESSIONS"]
        mkv_scan_interval_minutes = parsed["MKV_SCAN_INTERVAL_MINUTES"]
        mkv_min_confidence = parsed["MKV_MIN_CONFIDENCE"]
        mkv_cache_enabled = parsed["MKV_CACHE_ENABLED"]
        profiling_enabled = parsed["PROFILING_ENABLED"]
        profiling_ui_host = parsed["PROFILING_UI_HOST"]
        profiling_ui_port = parsed["PROFILING_UI_PORT"]

        logger.info(
            "loaded config mkv_dirs=%s ensure_langs=%s api_url=%s "
            "workers=%s api_key_set=%s jellyfin_url=%s jellyfin_token_set=%s "
            "retry_count=%s backoff_delay=%s availability_check_interval=%s debounce=%s scan_interval_minutes=%s "
            "stabilize_timeout=%s persistent_sessions=%s http_timeout=%s translation_timeout=%s "
            "libretranslate_max_concurrent_requests=%s mkv_scan_interval_minutes=%s "
            "mkv_min_confidence=%s mkv_cache_path=%s mkv_cache_enabled=%s "
            "libretranslate_max_concurrent_detection_requests=%s mkv_temp_dir=%s profiling_enabled=%s "
            "profiling_ui_host=%s profiling_ui_port=%s",
            mkv_dirs,
            ensure_langs,
            api_url,
            workers,
            bool(api_key),
            jellyfin_url,
            bool(jellyfin_token),
            retry_count,
            backoff_delay,
            availability_check_interval,
            debounce,
            stabilize_timeout,
            scan_interval_minutes,
            persistent_sessions,
            http_timeout,
            translation_timeout,
            libretranslate_max_concurrent_requests,
            mkv_scan_interval_minutes,
            mkv_min_confidence,
            str(mkv_cache_path),
            mkv_cache_enabled,
            libretranslate_max_concurrent_detection_requests,
            mkv_temp_dir,
            profiling_enabled,
            profiling_ui_host,
            profiling_ui_port,
        )

        return cls(
            root_dirs=root_dirs,
            ensure_langs=ensure_langs,
            api_url=api_url,
            workers=workers,
            api_key=api_key,
            jellyfin_url=jellyfin_url,
            jellyfin_token=jellyfin_token,
            retry_count=retry_count,
            backoff_delay=backoff_delay,
            availability_check_interval=availability_check_interval,
            debounce=debounce,
            stabilize_timeout=stabilize_timeout,
            scan_interval_minutes=scan_interval_minutes,
            http_timeout=http_timeout,
            translation_timeout=translation_timeout,
            libretranslate_max_concurrent_requests=libretranslate_max_concurrent_requests,
            libretranslate_max_concurrent_detection_requests=libretranslate_max_concurrent_detection_requests,
            persistent_sessions=persistent_sessions,
            mkv_scan_interval_minutes=mkv_scan_interval_minutes,
            mkv_min_confidence=mkv_min_confidence,
            mkv_cache_path=str(mkv_cache_path),
            mkv_dirs=mkv_dirs,
            mkv_cache_enabled=mkv_cache_enabled,
            mkv_temp_dir=mkv_temp_dir,
            profiling_enabled=profiling_enabled,
            profiling_ui_host=profiling_ui_host,
            profiling_ui_port=profiling_ui_port,
        )
