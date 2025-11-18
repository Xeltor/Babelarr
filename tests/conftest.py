import pytest

from babelarr.app import Application
from babelarr.config import Config


class _DummyTranslator:
    def translate(self, path, lang):
        return b""

    def close(self):
        pass

    def wait_until_available(self):
        return None


@pytest.fixture
def config(tmp_path):
    return Config(
        root_dirs=[str(tmp_path)],
        target_langs=["nl"],
        src_lang="en",
        src_ext=".en.srt",
        api_url="http://example",
        workers=1,
        retry_count=2,
        backoff_delay=0,
    )


@pytest.fixture
def app(config):
    instances = []

    def _create_app(*, translator=None, cfg=None, jellyfin=None):
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
