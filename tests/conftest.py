import pytest

from babelarr.app import Application
from babelarr.config import Config


class _DummyTranslator:
    def translate(self, path, lang):
        return b""

    def close(self):
        pass


@pytest.fixture
def config(tmp_path):
    return Config(
        root_dirs=[str(tmp_path)],
        target_langs=["nl"],
        src_ext=".en.srt",
        api_url="http://example",
        workers=1,
        queue_db=str(tmp_path / "queue.db"),
        retry_count=2,
        backoff_delay=0,
    )


@pytest.fixture
def app(config):
    instances = []

    def _create_app(*, translator=None, cfg=None):
        cfg = cfg or config
        translator = translator or _DummyTranslator()
        instance = Application(cfg, translator)
        instances.append(instance)
        return instance

    yield _create_app

    for inst in instances:
        try:
            inst.db.close()
        except Exception:
            pass
        close = getattr(inst.translator, "close", None)
        if callable(close):
            close()
