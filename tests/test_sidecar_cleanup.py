from pathlib import Path

from babelarr.sidecar_cleanup import SidecarCleaner


def test_removes_orphan_sidecars(tmp_path):
    orphan = tmp_path / "movie.en.srt"
    orphan.write_text("subtitle")
    cleaner = SidecarCleaner([str(tmp_path)])

    removed = cleaner.remove_orphans()

    assert removed == 1
    assert not orphan.exists()


def test_keeps_sidecars_with_parent_mkv(tmp_path):
    mkv = tmp_path / "movie.mkv"
    mkv.write_bytes(b"video")
    subtitle = tmp_path / "movie.bs.srt"
    subtitle.write_text("subtitle")
    cleaner = SidecarCleaner([str(tmp_path)])

    removed = cleaner.remove_orphans()

    assert removed == 0
    assert subtitle.exists()


def test_respects_ignore_markers(tmp_path):
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
