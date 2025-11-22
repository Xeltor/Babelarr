from __future__ import annotations

import logging
from pathlib import Path

from .ignore import is_path_ignored

logger = logging.getLogger(__name__)


class SidecarCleaner:
    """Remove subtitle sidecars that no longer have an MKV parent."""

    def __init__(self, directories: list[str]) -> None:
        self.directories = directories

    def remove_orphans(self) -> int:
        removed = 0
        for root in self.directories:
            root_path = Path(root)
            if not root_path.is_dir():
                logger.warning(
                    "sidecar_cleanup_skip path=%s reason=missing_root", root_path
                )
                continue
            if is_path_ignored(root_path, root=root_path):
                logger.info(
                    "sidecar_cleanup_skip path=%s reason=ignored_root", root_path
                )
                continue
            for subtitle in root_path.rglob("*.srt"):
                if is_path_ignored(subtitle, root=root_path):
                    continue
                if self._has_parent_mkv(subtitle):
                    continue
                try:
                    subtitle.unlink()
                except Exception as exc:
                    logger.warning(
                        "sidecar_cleanup_failed path=%s error=%s",
                        subtitle,
                        exc,
                    )
                else:
                    removed += 1
                    logger.info("sidecar_orphan_removed path=%s", subtitle)
        logger.info("sidecar_cleanup_complete removed=%d", removed)
        return removed

    @staticmethod
    def _has_parent_mkv(subtitle: Path) -> bool:
        base = subtitle.with_suffix("")
        parent = base.with_suffix(".mkv")
        try:
            return parent.exists()
        except OSError:
            return False
