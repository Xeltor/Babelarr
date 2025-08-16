import pytest

from babelarr.config import Config


def test_from_env_rejects_empty_target_langs(monkeypatch):
    monkeypatch.setenv("TARGET_LANGS", "")
    with pytest.raises(ValueError):
        Config.from_env()
