import time
from pathlib import Path

from babelarr.app import Application
from babelarr.config import Config
from babelarr.sidecar_cleanup import SidecarCleaner
from babelarr.translator import Translator


class _FakeTranslator(Translator):
    def translate(self, path: Path, lang: str, *, src_lang: str | None = None) -> bytes:
        return b""

    def close(self) -> None:
        return None

    def wait_until_available(self) -> None:
        return None

    def supports_translation(self, src_lang: str, target_lang: str) -> bool:
        return True

    def is_target_supported(self, target_lang: str) -> bool:
        return True


def _make_config(tmp_path):
    return Config(
        root_dirs=[],
        api_url="http://example",
        workers=1,
        ensure_langs=["en"],
        mkv_dirs=[str(tmp_path)],
    )


def test_sidecar_cleanup_runs_in_background(tmp_path):
    class SlowCleaner(SidecarCleaner):
        def remove_orphans(self) -> int:
            time.sleep(0.2)
            return 0

    app = Application(_make_config(tmp_path), translator=_FakeTranslator())
    app.sidecar_cleaner = SlowCleaner([str(tmp_path)])

    start = time.monotonic()
    app._clean_orphaned_sidecars()
    elapsed = time.monotonic() - start

    assert elapsed < 0.1
    thread = app._sidecar_cleanup_thread
    assert thread is not None
    thread.join(timeout=1)
    assert not thread.is_alive()
