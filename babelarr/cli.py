import argparse
import logging
import os
import signal
from pathlib import Path

import requests

from .app import Application
from .config import Config
from .jellyfin_api import JellyfinClient
from .mkv import MkvSubtitleExtractor, MkvSubtitleTagger
from .profiling import WorkloadProfiler
from .profiling_ui import ProfilingDashboard
from .translator import LibreTranslateClient

logger = logging.getLogger(__name__)


def _preferred_source_language(ensure_langs: list[str]) -> str:
    if "en" in ensure_langs:
        return "en"
    if ensure_langs:
        return ensure_langs[0]
    return "en"


def validate_environment(config: Config) -> None:
    """Validate MKV directories and translation service availability.

    Updates ``config.mkv_dirs`` to only include readable directories. If none
    remain, the process exits with a clear error. The translation service is
    probed with a ``HEAD`` request and any failure is logged, but startup
    continues and workers will wait until the service becomes reachable.
    """

    valid_dirs: list[str] = []
    mkv_dirs = config.mkv_dirs or []
    for d in mkv_dirs:
        path = Path(d)
        if not path.is_dir():
            logger.warning("missing_mkv_dir path=%s", path.name)
            continue
        if not os.access(path, os.R_OK):
            logger.warning("unreadable_mkv_dir path=%s", path.name)
            continue
        valid_dirs.append(d)

    if not valid_dirs:
        logger.error(
            "no_readable_mkv_dirs dirs=%s", [Path(d).name for d in mkv_dirs]
        )
        raise SystemExit("No valid MKV directories configured")

    config.mkv_dirs = valid_dirs
    config.root_dirs = list(valid_dirs)
    logger.info(
        "environment_ready mkv_dirs=%s",
        [Path(d).name for d in valid_dirs],
    )

    try:
        resp = requests.head(config.api_url, timeout=config.http_timeout)
        if resp.status_code >= 400:
            raise requests.RequestException(f"HTTP {resp.status_code}")
    except requests.RequestException as exc:
        logger.error("service_unreachable url=%s error=%s", config.api_url, exc)


def validate_ensure_languages(config: Config, translator: LibreTranslateClient) -> None:
    """Ensure every configured language can be produced by the service."""

    try:
        translator.ensure_languages()
    except ValueError as exc:
        logger.error("language_error detail=%s", exc)
        raise SystemExit(str(exc))
    except requests.RequestException as exc:  # pragma: no cover - network failure
        logger.error("fetch_languages_failed error=%s", exc)
        return

    original_langs = config.ensure_langs
    supported_langs: list[str] = []
    unsupported: list[str] = []
    for lang in original_langs:
        if translator.is_target_supported(lang):
            supported_langs.append(lang)
        else:
            unsupported.append(lang)

    if unsupported:
        logger.error(
            "unsupported_ensure_langs langs=%s",
            ", ".join(unsupported),
        )
    if not supported_langs:
        raise SystemExit("No supported languages left in ENSURE_LANGS")

    if supported_langs != original_langs:
        config.ensure_langs = supported_langs

    logger.info(
        "ensure_langs langs=%s preferred_source=%s",
        ", ".join(config.ensure_langs),
        _preferred_source_language(config.ensure_langs),
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="babelarr")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set logging level",
    )
    parser.add_argument("--log-file", help="Write logs to a file")
    args = parser.parse_args(argv)

    log_level = (args.log_level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    log_file = args.log_file or os.environ.get("LOG_FILE")
    logging.basicConfig(
        level=log_level,
        filename=log_file,
        format="%(asctime)s [%(levelname)s] [%(name)s] [%(threadName)s] %(message)s",
    )
    logging.getLogger("watchdog").setLevel(logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logger.info("start log_level=%s log_file=%s", log_level, log_file)

    config = Config.from_env()
    validate_environment(config)
    logger.info(
        "config_loaded api_url=%s ensure_langs=%s",
        config.api_url,
        config.ensure_langs,
    )
    profiler = WorkloadProfiler(enabled=config.profiling_enabled)
    dashboard = ProfilingDashboard(
        profiler,
        host=config.profiling_ui_host,
        port=config.profiling_ui_port,
    )
    preferred_source = _preferred_source_language(config.ensure_langs)
    translator = LibreTranslateClient(
        config.api_url,
        preferred_source,
        retry_count=config.retry_count,
        backoff_delay=config.backoff_delay,
        availability_check_interval=config.availability_check_interval,
        api_key=config.api_key,
        persistent_session=config.persistent_sessions,
        http_timeout=config.http_timeout,
        translation_timeout=config.translation_timeout,
        max_concurrent_requests=config.libretranslate_max_concurrent_requests,
        max_concurrent_detection_requests=config.libretranslate_max_concurrent_detection_requests,
        fallback_urls=config.libretranslate_fallback_urls,
        profiler=profiler,
    )
    validate_ensure_languages(config, translator)
    jellyfin_client = None
    if config.jellyfin_url and config.jellyfin_token:
        jellyfin_client = JellyfinClient(
            config.jellyfin_url, config.jellyfin_token, config.http_timeout
        )
    extractor = MkvSubtitleExtractor(
        temp_dir=Path(config.mkv_temp_dir),
        profiler=profiler,
    )
    mkv_tagger = MkvSubtitleTagger(
        extractor=extractor,
        translator=translator,
        min_confidence=config.mkv_min_confidence,
        profiler=profiler,
    )
    logger.info(
        "mkv_tagger_ready min_confidence=%.2f cache_path=%s",
        config.mkv_min_confidence,
        config.mkv_cache_path,
    )

    app = Application(
        config,
        translator,
        jellyfin_client,
        mkv_tagger,
        profiler=profiler,
        profiling_dashboard=dashboard,
    )

    def handle_signal(signum, frame):
        logger.info("received_signal signum=%s", signum)
        app.shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle_signal)

    app.run()


if __name__ == "__main__":
    main()
