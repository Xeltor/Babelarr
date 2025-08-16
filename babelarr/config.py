import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("babelarr")


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
        retry_count: Translation retry attempts.
        backoff_delay: Initial backoff delay between retries.
        debounce: Seconds to wait for file changes to settle before enqueueing.
    """

    root_dirs: list[str]
    target_langs: list[str]
    src_lang: str
    src_ext: str
    api_url: str
    workers: int
    queue_db: str
    api_key: str | None = None
    retry_count: int = 3
    backoff_delay: float = 1.0
    debounce: float = 0.1

    @classmethod
    def from_env(cls) -> "Config":
        root_dirs = [p for p in os.environ.get("WATCH_DIRS", "/data").split(":") if p]

        raw_langs = os.environ.get("TARGET_LANGS", "nl,bs").split(",")
        target_langs: list[str] = []
        seen = set()
        for lang in raw_langs:
            cleaned = lang.strip()
            if not cleaned:
                logger.warning("Empty language code in TARGET_LANGS; ignoring")
                continue
            if not cleaned.isalpha():
                logger.warning(
                    "Invalid language code '%s' in TARGET_LANGS; ignoring", cleaned
                )
                continue
            normalized = cleaned.lower()
            if normalized in seen:
                logger.debug(
                    "Duplicate language code '%s' in TARGET_LANGS; ignoring", cleaned
                )
                continue
            target_langs.append(normalized)
            seen.add(normalized)

        src_lang = os.environ.get("SRC_LANG", "en").strip().lower()
        if not src_lang.isalpha():
            logger.warning("Invalid SRC_LANG '%s'; defaulting to 'en'", src_lang)
            src_lang = "en"
        src_ext = f".{src_lang}.srt"
        api_url = os.environ.get("LIBRETRANSLATE_URL", "http://libretranslate:5000")

        MAX_WORKERS = 10
        requested = int(os.environ.get("WORKERS", "1"))
        workers = min(requested, MAX_WORKERS)
        if requested > MAX_WORKERS:
            logger.warning(
                "Requested %s workers, capping at %s to prevent instability",
                requested,
                MAX_WORKERS,
            )

        queue_db = "/config/queue.db"
        Path(queue_db).parent.mkdir(parents=True, exist_ok=True)

        api_key = os.environ.get("LIBRETRANSLATE_API_KEY") or None
        retry_count = int(os.environ.get("RETRY_COUNT", "3"))
        backoff_delay = float(os.environ.get("BACKOFF_DELAY", "1"))
        debounce = float(os.environ.get("DEBOUNCE_SECONDS", "0.1"))

        logger.debug(
            "Config: ROOT_DIRS=%s TARGET_LANGS=%s SRC_LANG=%s API_URL=%s "
            "WORKERS=%s QUEUE_DB=%s API_KEY_SET=%s RETRY_COUNT=%s "
            "BACKOFF_DELAY=%s DEBOUNCE=%s",
            root_dirs,
            target_langs,
            src_lang,
            api_url,
            workers,
            queue_db,
            bool(api_key),
            retry_count,
            backoff_delay,
            debounce,
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
            retry_count=retry_count,
            backoff_delay=backoff_delay,
            debounce=debounce,
        )
