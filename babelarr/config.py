import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("babelarr")


@dataclass
class Config:
    root_dirs: list[str]
    target_langs: list[str]
    src_ext: str
    api_url: str
    workers: int
    queue_db: str

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

        src_ext = os.environ.get("SRC_EXT", ".en.srt")
        api_url = os.environ.get(
            "LIBRETRANSLATE_URL", "http://libretranslate:5000/translate_file"
        )

        MAX_WORKERS = 10
        requested = int(os.environ.get("WORKERS", "1"))
        workers = min(requested, MAX_WORKERS)
        if requested > MAX_WORKERS:
            logger.warning(
                "Requested %s workers, capping at %s to prevent instability",
                requested,
                MAX_WORKERS,
            )

        queue_db = os.environ.get("QUEUE_DB", "/config/queue.db")
        Path(queue_db).parent.mkdir(parents=True, exist_ok=True)

        logger.debug(
            "Config: ROOT_DIRS=%s TARGET_LANGS=%s SRC_EXT=%s API_URL=%s "
            "WORKERS=%s QUEUE_DB=%s",
            root_dirs,
            target_langs,
            src_ext,
            api_url,
            workers,
            queue_db,
        )

        return cls(
            root_dirs=root_dirs,
            target_langs=target_langs,
            src_ext=src_ext,
            api_url=api_url,
            workers=workers,
            queue_db=queue_db,
        )
