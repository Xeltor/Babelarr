from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from watchdog.events import FileCreatedEvent

from babelarr.watch import MkvHandler


class _RecordingHandler(MkvHandler):
    def __init__(self, app, root: Path | str | None = None):
        super().__init__(app, root=root)
        self.handled: list[Path] = []
        app.handle_new_mkv = self.handled.append

    def _wait_for_complete(self, path: Path) -> bool:
        return True


def _make_handler(root: Path | None = None) -> _RecordingHandler:
    config = SimpleNamespace(debounce=0.01, stabilize_timeout=1.0)
    app = SimpleNamespace(config=config)
    return _RecordingHandler(app, root=root)


def test_uppercase_mkv_path_triggers_handler(tmp_path: Path) -> None:
    handler = _make_handler(root=tmp_path)
    event_path = tmp_path / "movie.MKV"
    handler.dispatch(FileCreatedEvent(str(event_path)))
    assert handler.handled == [event_path]


def test_ignore_marker_skips_handler(tmp_path: Path) -> None:
    handler = _make_handler(root=tmp_path)
    (tmp_path / ".babelarr_ignore").write_text("")
    event_path = tmp_path / "movie.mkv"
    handler.dispatch(FileCreatedEvent(str(event_path)))
    assert handler.handled == []
