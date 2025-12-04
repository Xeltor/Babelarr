from __future__ import annotations

from pathlib import Path

from babelarr.ignore import MARKER_FILENAME, _resolve_root, is_path_ignored


def test_is_path_ignored_detects_marker(tmp_path: Path) -> None:
    marker = tmp_path / MARKER_FILENAME
    marker.touch()
    nested = tmp_path / "nested"
    nested.mkdir()

    assert is_path_ignored(nested)


def test_is_path_ignored_stops_at_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    deeper = root / "child"
    deeper.mkdir(parents=True)
    (tmp_path / MARKER_FILENAME).touch()

    assert is_path_ignored(deeper, root=root) is False


def test_resolve_root_handles_errors() -> None:
    class FailingPath(Path):
        def resolve(self) -> Path:  # type: ignore[override]
            raise OSError("fail")

    broken = FailingPath("missing")

    assert _resolve_root(None) is None
    resolved = _resolve_root(Path("relative"))
    assert resolved is not None and resolved.is_absolute()
    assert _resolve_root(broken) is broken
