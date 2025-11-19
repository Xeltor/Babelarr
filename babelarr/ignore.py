from __future__ import annotations

from pathlib import Path

MARKER_FILENAME = ".babelarr_ignore"


def is_path_ignored(path: Path, root: Path | None = None) -> bool:
    """Return True if `path` or any ancestor up to `root` contains the ignore marker."""
    current = path if path.is_dir() else path.parent
    limit = _resolve_root(root)
    while True:
        if (current / MARKER_FILENAME).exists():
            return True
        if limit is not None and current == limit:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return False


def _resolve_root(root: Path | None) -> Path | None:
    if root is None:
        return None
    try:
        return root.resolve()
    except OSError:
        return root
