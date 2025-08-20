from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover
    from .app import Application


class TranslationLogger(logging.LoggerAdapter):
    """Logger adapter that appends translation context."""

    def __init__(
        self,
        path: Path | None = None,
        lang: str | None = None,
        task_id: str | None = None,
    ) -> None:
        path_name = None
        if isinstance(path, (str, Path)):
            path_name = Path(path).name
        extra = {"path": path_name, "lang": lang, "task_id": task_id}
        super().__init__(logger, extra)

    def process(
        self, msg: str, kwargs: MutableMapping[str, Any]
    ) -> tuple[str, MutableMapping[str, Any]]:
        source = self.extra or {}
        extra: dict[str, Any] = {k: v for k, v in source.items() if v is not None}
        supplied = kwargs.get("extra")
        if isinstance(supplied, Mapping):
            for k, v in supplied.items():
                if v is not None:
                    extra[k] = v
        path_val = extra.get("path")
        if isinstance(path_val, Path):
            extra["path"] = path_val.name
        elif isinstance(path_val, str):
            extra["path"] = Path(path_val).name
        kwargs["extra"] = extra
        if extra:
            msg = f"{msg} " + " ".join(f"{k}={v}" for k, v in extra.items())
        return msg, kwargs


@dataclass(frozen=True)
class TranslationTask:
    path: Path
    lang: str
    task_id: str
    priority: int = 0


def get_task(app: Application) -> TranslationTask | None:
    try:
        _, _, task = app.tasks.get(timeout=0.1)
        return task
    except queue.Empty:
        return None


def process_translation(
    app: Application, task: TranslationTask, worker_name: str
) -> bool:
    path, lang, task_id = task.path, task.lang, task.task_id
    tlog = TranslationLogger(path, lang, task_id)
    if path.exists():
        tlog.debug("worker_translate name=%s", worker_name)
        return app.translate_file(path, lang, task_id)
    tlog.warning("worker_missing name=%s", worker_name)
    return False


def handle_failure(
    app: Application,
    task: TranslationTask,
    exc: Exception,
    worker_name: str,
    wait: Callable[[], None] | None,
) -> bool:
    path, lang, task_id = task.path, task.lang, task.task_id
    tlog = TranslationLogger(path, lang, task_id)
    tlog.error("translation_failed name=%s error=%s", worker_name, exc)
    tlog.debug("traceback", exc_info=True)
    if isinstance(exc, requests.RequestException):
        app.translator_available.clear()
        if callable(wait):
            wait()
        app.translator_available.set()
        return True
    return False


def worker(app: Application, idle_timeout: float = 30 * 60) -> None:
    """Process translation tasks until shutdown or *idle_timeout* elapses."""

    name = threading.current_thread().name
    logger.debug("worker_start name=%s", name)
    wait = getattr(app.translator, "wait_until_available", None)
    if callable(wait):
        wait()
    app.translator_available.set()
    last_activity = time.monotonic()
    try:
        while not app.shutdown_event.is_set():
            if not app.translator_available.wait(timeout=1):
                if time.monotonic() - last_activity > idle_timeout:
                    break
                continue
            task = get_task(app)
            if task is None:
                if time.monotonic() - last_activity > idle_timeout:
                    break
                continue
            path, lang, task_id = task.path, task.lang, task.task_id
            tlog = TranslationLogger(path, lang, task_id)
            start_time = time.monotonic()
            tlog.debug("worker_pickup name=%s", name)
            try:
                success = process_translation(app, task, name)
                requeue = False
                outcome = "succeeded" if success else "skipped"
            except Exception as exc:  # noqa: BLE001
                requeue = handle_failure(app, task, exc, name, wait)
                success = False
                outcome = "failed"
            elapsed = time.monotonic() - start_time
            last_activity = time.monotonic()
            if requeue:
                app.tasks.put((task.priority, app._task_counter, task))
                app._task_counter += 1
                tlog.info(
                    "worker_requeue name=%s queue=%d",
                    name,
                    app.db.count(),
                )
            else:
                app.db.remove(path, lang)
                tlog.info(
                    "translation outcome=%s duration=%.2fs queue=%d",
                    outcome,
                    elapsed,
                    app.db.count(),
                )
                if app.jellyfin:
                    app.translation_done(path, lang)
            app.tasks.task_done()
            tlog.debug(
                "worker_finish name=%s duration=%.2fs",
                name,
                elapsed,
            )
    finally:
        with app._worker_lock:
            app._active_workers -= 1
            app._worker_threads.discard(threading.current_thread())
            logger.info(
                "worker_exit name=%s active=%d",
                name,
                app._active_workers,
            )
