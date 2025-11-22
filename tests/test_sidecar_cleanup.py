from __future__ import annotations

import logging
from pathlib import Path

import pytest

from babelarr.sidecar_cleanup import SidecarCleaner


def test_removes_orphan_sidecars(tmp_path: Path) -> None:
    orphan = tmp_path / "movie.en.srt"
    orphan.write_text("subtitle")
    cleaner = SidecarCleaner([str(tmp_path)])

    removed = cleaner.remove_orphans()

    assert removed == 1
    assert not orphan.exists()


def test_keeps_sidecars_with_parent_mkv(tmp_path: Path) -> None:
    mkv = tmp_path / "movie.mkv"
    mkv.write_bytes(b"video")
    subtitle = tmp_path / "movie.bs.srt"
    subtitle.write_text("subtitle")
    cleaner = SidecarCleaner([str(tmp_path)])

    removed = cleaner.remove_orphans()

    assert removed == 0
    assert subtitle.exists()


def test_respects_ignore_markers(tmp_path: Path) -> None:
    ignored_dir = tmp_path / "ignored"
    ignored_dir.mkdir()
    (ignored_dir / ".babelarr_ignore").touch()
    ignored_orphan = ignored_dir / "show.en.srt"
    ignored_orphan.write_text("subtitle")
    active_orphan = tmp_path / "orphan.srt"
    active_orphan.write_text("subtitle")
    cleaner = SidecarCleaner([str(tmp_path)])

    removed = cleaner.remove_orphans()

    assert removed == 1
    assert not active_orphan.exists()
    assert ignored_orphan.exists()


def test_skips_missing_root(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    missing_root = tmp_path / "missing"
    cleaner = SidecarCleaner([str(missing_root)])

    with caplog.at_level(logging.WARNING, logger="babelarr.sidecar_cleanup"):
        removed = cleaner.remove_orphans()

    assert removed == 0
    assert "sidecar_cleanup_skip" in caplog.text


def test_skips_root_with_marker(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ignored_root = tmp_path / "ignored"
    ignored_root.mkdir()
    (ignored_root / ".babelarr_ignore").touch()
    cleaner = SidecarCleaner([str(ignored_root)])

    with caplog.at_level(logging.INFO, logger="babelarr.sidecar_cleanup"):
        removed = cleaner.remove_orphans()

    assert removed == 0
    assert "reason=ignored_root" in caplog.text


def test_cleanup_failure_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    failing_subtitle = tmp_path / "fail.srt"
    failing_subtitle.write_text("subtitle")
    cleaner = SidecarCleaner([str(tmp_path)])
    original_unlink = Path.unlink

    def fake_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == failing_subtitle:
            raise PermissionError("blocked")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fake_unlink)
    with caplog.at_level(logging.WARNING, logger="babelarr.sidecar_cleanup"):
        removed = cleaner.remove_orphans()

    assert removed == 0
    assert failing_subtitle.exists()
    assert "sidecar_cleanup_failed" in caplog.text


def test_has_parent_mkv_handles_os_error() -> None:
    class FailingPath(Path):
        _flavour = Path(".")._flavour

        def with_suffix(self, suffix: str) -> FailingPath:  # type: ignore[override]
            return self

        def exists(self) -> bool:  # type: ignore[override]
            raise OSError("boom")

    failing_path = FailingPath("movie.srt")

    assert SidecarCleaner._has_parent_mkv(failing_path) is False
