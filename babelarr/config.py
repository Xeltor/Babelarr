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
        root_dirs: Directories to watch for subtitle files.
        target_langs: Languages to translate into.
        src_lang: Source subtitle language.
        src_ext: Source subtitle file extension derived from src_lang.
        api_url: Base URL of the translation API.
        workers: Number of translation worker threads.
        queue_db: Path to the SQLite queue database.
        api_key: Optional API key for authenticated requests.
        jellyfin_url: Base URL of the Jellyfin server.
        jellyfin_token: API token for Jellyfin.
        retry_count: Translation retry attempts.
        backoff_delay: Initial backoff delay between retries.
        debounce: Seconds to wait for file changes to settle before enqueueing.
        stabilize_timeout: Max seconds to wait for file size to stabilize.
        scan_interval_minutes: Interval between periodic full scans.
    """

    root_dirs: list[str]
    target_langs: list[str]
    src_lang: str
    src_ext: str
    api_url: str
    workers: int
    queue_db: str
    api_key: str | None = None
    jellyfin_url: str | None = None
    jellyfin_token: str | None = None
    retry_count: int = 3
    backoff_delay: float = 1.0
    availability_check_interval: float = 30.0
    debounce: float = 0.1
    stabilize_timeout: float = 30.0
    scan_interval_minutes: int = 60
    http_timeout: float = 30.0
    translation_timeout: float = 900.0
    persistent_sessions: bool = False

    @staticmethod
    def _parse_target_languages(raw: str | None) -> list[str]:
        raw_langs = (raw if raw is not None else "nl,bs").split(",")
        target_langs: list[str] = []
        seen: set[str] = set()
        for lang in raw_langs:
            cleaned = lang.strip()
            if not cleaned:
                logger.warning("ignore empty language code in TARGET_LANGS")
                continue
            if not cleaned.isalpha():
                logger.warning(
                    "ignore invalid language code '%s' in TARGET_LANGS", cleaned
                )
                continue
            normalized = cleaned.lower()
            if normalized in seen:
                logger.debug(
                    "ignore duplicate language code '%s' in TARGET_LANGS", cleaned
                )
                continue
            target_langs.append(normalized)
            seen.add(normalized)
        if not target_langs:
            logger.error("found no valid languages in TARGET_LANGS")
            raise ValueError(
                "TARGET_LANGS must contain at least one valid language code",
            )
        return target_langs

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
    def _parse_scan_interval(raw: str | None) -> int:
        default_scan_interval = 60
        raw_scan = raw or str(default_scan_interval)
        try:
            return int(raw_scan)
        except ValueError:
            logger.warning(
                "use default %s for invalid SCAN_INTERVAL_MINUTES '%s'",
                default_scan_interval,
                raw_scan,
            )
            return default_scan_interval

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

    @classmethod
    def from_env(cls) -> "Config":
        root_dirs = [p for p in os.environ.get("WATCH_DIRS", "/data").split(":") if p]

        src_lang = os.environ.get("SRC_LANG", "en").strip().lower()
        if not src_lang.isalpha():
            logger.warning("use default 'en' for invalid SRC_LANG '%s'", src_lang)
            src_lang = "en"
        src_ext = f".{src_lang}.srt"
        api_url = os.environ.get("LIBRETRANSLATE_URL", "http://libretranslate:5000")

        queue_db_path = Path(os.environ.get("QUEUE_DB", "/config/queue.db"))
        queue_db_path.parent.mkdir(parents=True, exist_ok=True)
        queue_db = str(queue_db_path)

        api_key = os.environ.get("LIBRETRANSLATE_API_KEY") or None
        jellyfin_url = os.environ.get("JELLYFIN_URL") or None
        jellyfin_token = os.environ.get("JELLYFIN_TOKEN") or None

        parsers: dict[str, Callable[[str | None], Any]] = {
            "TARGET_LANGS": cls._parse_target_languages,
            "WORKERS": cls._parse_workers,
            "RETRY_COUNT": lambda v: cls._parse_int("RETRY_COUNT", v, 3),
            "BACKOFF_DELAY": lambda v: cls._parse_float("BACKOFF_DELAY", v, 1.0),
            "AVAILABILITY_CHECK_INTERVAL": lambda v: cls._parse_float(
                "AVAILABILITY_CHECK_INTERVAL", v, 30.0
            ),
            "DEBOUNCE_SECONDS": lambda v: cls._parse_float("DEBOUNCE_SECONDS", v, 0.1),
            "STABILIZE_TIMEOUT": lambda v: cls._parse_float(
                "STABILIZE_TIMEOUT", v, 30.0
            ),
            "SCAN_INTERVAL_MINUTES": cls._parse_scan_interval,
            "HTTP_TIMEOUT": lambda v: cls._parse_float("HTTP_TIMEOUT", v, 30.0),
            "TRANSLATION_TIMEOUT": lambda v: cls._parse_float(
                "TRANSLATION_TIMEOUT", v, 900.0
            ),
            "PERSISTENT_SESSIONS": lambda v: cls._parse_bool(
                "PERSISTENT_SESSIONS", v, False
            ),
        }

        parsed = {
            name: parser(os.environ.get(name)) for name, parser in parsers.items()
        }

        target_langs = parsed["TARGET_LANGS"]
        workers = parsed["WORKERS"]
        retry_count = parsed["RETRY_COUNT"]
        backoff_delay = parsed["BACKOFF_DELAY"]
        availability_check_interval = parsed["AVAILABILITY_CHECK_INTERVAL"]
        debounce = parsed["DEBOUNCE_SECONDS"]
        stabilize_timeout = parsed["STABILIZE_TIMEOUT"]
        scan_interval_minutes = parsed["SCAN_INTERVAL_MINUTES"]
        http_timeout = parsed["HTTP_TIMEOUT"]
        translation_timeout = parsed["TRANSLATION_TIMEOUT"]
        persistent_sessions = parsed["PERSISTENT_SESSIONS"]

        logger.info(
            "loaded config root_dirs=%s target_langs=%s src_lang=%s api_url=%s "
            "workers=%s queue_db=%s api_key_set=%s jellyfin_url=%s jellyfin_token_set=%s "
            "retry_count=%s backoff_delay=%s availability_check_interval=%s debounce=%s scan_interval_minutes=%s "
            "stabilize_timeout=%s persistent_sessions=%s http_timeout=%s translation_timeout=%s",
            root_dirs,
            target_langs,
            src_lang,
            api_url,
            workers,
            queue_db,
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
        )

        return cls(
            root_dirs=root_dirs,
            target_langs=target_langs,
            src_lang=src_lang,
            src_ext=src_ext,
            api_url=api_url,
            workers=workers,
            queue_db=queue_db,
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
            persistent_sessions=persistent_sessions,
        )
