from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from watchdog.events import FileCreatedEvent, FileDeletedEvent

from babelarr import watch as watch_module
from babelarr.watch import MkvHandler

if TYPE_CHECKING:
    from babelarr.app import Application


class _RecordingHandler(MkvHandler):
    def __init__(self, app: Application, root: Path | str | None = None) -> None:
        super().__init__(app, root=root)
        self.handled: list[Path] = []
        app.handle_new_mkv = self.handled.append

    def _wait_for_complete(self, path: Path) -> bool:
        return True


def _make_handler(root: Path | None = None) -> _RecordingHandler:
    config = SimpleNamespace(debounce=0.01, stabilize_timeout=0.05)
    app = SimpleNamespace(
        config=config,
        shutdown_event=threading.Event(),
        invalidate_mkv_cache_state=lambda path: None,
    )
    return _RecordingHandler(app, root=root)


def test_uppercase_mkv_path_triggers_handler(tmp_path: Path) -> None:
    handler = _make_handler(root=tmp_path)
    event_path = tmp_path / "movie.MKV"
    handler.dispatch(FileCreatedEvent(str(event_path)))
    assert handler.wait_until_idle(timeout=1.0)
    assert handler.handled == [event_path]
    handler.stop()


def test_wait_for_complete_times_out(tmp_path: Path) -> None:
    config = SimpleNamespace(debounce=0.005, stabilize_timeout=0.01)
    app = SimpleNamespace(
        config=config,
        shutdown_event=threading.Event(),
        invalidate_mkv_cache_state=lambda path: None,
        handle_new_mkv=lambda path: None,
    )
    handler = MkvHandler(app, root=tmp_path)
    target = tmp_path / "missing.mkv"
    target.write_bytes(b"a")
    target.unlink()

    assert handler._wait_for_complete(target) is False
    handler.stop()
    handler.stop()


def test_on_deleted_invalidates_cache(tmp_path: Path) -> None:
    invalidated: list[Path] = []

    def invalidate(path: Path) -> None:
        invalidated.append(path)

    config = SimpleNamespace(debounce=0.01, stabilize_timeout=0.1)
    app = SimpleNamespace(
        config=config,
        shutdown_event=threading.Event(),
        invalidate_mkv_cache_state=invalidate,
        handle_new_mkv=lambda path: None,
    )
    handler = _RecordingHandler(app, root=tmp_path)
    event = FileDeletedEvent(str(tmp_path / "gone.mkv"))

    handler.on_deleted(event)

    assert invalidated == [Path(event.src_path)]
    handler.stop()


def test_watch_sets_up_observer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduled: list[tuple[object, str, bool]] = []

    class FakeObserver:
        def __init__(self) -> None:
            self.stopped = False
            self.started = False
            self.joined = False
            self.name = None

        def schedule(self, handler: object, path: str, recursive: bool) -> None:
            scheduled.append((handler, path, recursive))

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            self.stopped = True

        def join(self) -> None:
            self.joined = True

    monkeypatch.setattr(watch_module, "Observer", FakeObserver)
    mkv_dir = tmp_path / "watch"
    mkv_dir.mkdir()
    config = SimpleNamespace(
        debounce=0.01,
        stabilize_timeout=0.01,
        mkv_dirs=[str(mkv_dir)],
    )
    app = SimpleNamespace(
        config=config,
        shutdown_event=threading.Event(),
        handle_new_mkv=lambda path: None,
        invalidate_mkv_cache_state=lambda path: None,
    )
    app.shutdown_event.set()  # exit loop immediately

    watch_module.watch(app)

    assert scheduled and scheduled[0][1] == str(mkv_dir)


def test_watch_skips_ignored_and_missing_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    scheduled: list[tuple[object, str, bool]] = []

    class FakeObserver:
        def __init__(self) -> None:
            self.name = None

        def schedule(self, handler: object, path: str, recursive: bool) -> None:
            scheduled.append((handler, path, recursive))

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def join(self) -> None:
            return None

    monkeypatch.setattr(watch_module, "Observer", FakeObserver)
    ignored_dir = tmp_path / "ignored"
    ignored_dir.mkdir()
    (ignored_dir / ".babelarr_ignore").touch()
    missing_dir = tmp_path / "missing"
    config = SimpleNamespace(
        debounce=0.01,
        stabilize_timeout=0.01,
        mkv_dirs=[str(ignored_dir), str(missing_dir)],
    )
    app = SimpleNamespace(
        config=config,
        shutdown_event=threading.Event(),
        handle_new_mkv=lambda path: None,
        invalidate_mkv_cache_state=lambda path: None,
    )
    app.shutdown_event.set()

    with caplog.at_level("INFO", logger="babelarr.watch"):
        watch_module.watch(app)

    assert not scheduled
    assert "watch_skip_ignored" in caplog.text


def test_ignore_marker_skips_handler(tmp_path: Path) -> None:
    handler = _make_handler(root=tmp_path)
    (tmp_path / ".babelarr_ignore").write_text("")
    event_path = tmp_path / "movie.mkv"
    handler.dispatch(FileCreatedEvent(str(event_path)))
    assert handler.wait_until_idle(timeout=1.0)
    assert handler.handled == []
    handler.stop()
