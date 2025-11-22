from collections.abc import Callable, Generator
from pathlib import Path

import pytest

from babelarr.app import Application
from babelarr.config import Config
from babelarr.jellyfin_api import JellyfinClient
from babelarr.translator import Translator


class _DummyTranslator(Translator):
    def translate(self, path: Path, lang: str, *, src_lang: str | None = None) -> bytes:
        return b""

    def close(self) -> None:
        pass

    def wait_until_available(self) -> None:
        return None

    def supports_translation(self, src_lang: str, target_lang: str) -> bool:
        return True

    def is_target_supported(self, target_lang: str) -> bool:
        return True


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        root_dirs=[str(tmp_path)],
        api_url="http://example",
        workers=1,
        ensure_langs=["en"],
        retry_count=2,
        backoff_delay=0,
        mkv_dirs=[str(tmp_path)],
    )


@pytest.fixture
def app(
    config: Config,
) -> Generator[Callable[..., Application], None, None]:
    instances: list[Application] = []

    def _create_app(
        *,
        translator: Translator | None = None,
        cfg: Config | None = None,
        jellyfin: JellyfinClient | None = None,
    ) -> Application:
        cfg = cfg or config
        translator = translator or _DummyTranslator()
        instance = Application(cfg, translator, jellyfin)
        instances.append(instance)
        return instance

    yield _create_app

    for inst in instances:
        close = getattr(inst.translator, "close", None)
        if callable(close):
            close()
