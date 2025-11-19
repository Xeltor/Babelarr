from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from watchdog.events import FileCreatedEvent

from babelarr.watch import MkvHandler


class _RecordingHandler(MkvHandler):
    def __init__(self, app):
        super().__init__(app)
        self.handled: list[Path] = []

    def _handle(self, path: Path) -> None:
        self.handled.append(path)


def _make_handler() -> _RecordingHandler:
    config = SimpleNamespace(debounce=0.01, stabilize_timeout=1.0)
    app = SimpleNamespace(config=config)
    return _RecordingHandler(app)


def test_uppercase_mkv_path_triggers_handler(tmp_path: Path) -> None:
    handler = _make_handler()
    event_path = tmp_path / "movie.MKV"
    handler.dispatch(FileCreatedEvent(str(event_path)))
    assert handler.handled == [event_path]
