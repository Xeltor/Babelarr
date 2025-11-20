import logging
from types import SimpleNamespace

from babelarr.cli import validate_environment
from babelarr.config import Config


def test_validate_environment_filters_mkv_dirs(tmp_path, monkeypatch, caplog):
    valid_dir = tmp_path / "valid"
    valid_dir.mkdir()
    missing_dir = tmp_path / "missing"
    config = Config(
        root_dirs=["/unused"],
        target_langs=["nl"],
        src_lang="en",
        src_ext=".en.srt",
        api_url="http://example",
        workers=1,
        retry_count=1,
        backoff_delay=0,
        mkv_dirs=[str(valid_dir), str(missing_dir)],
    )
    monkeypatch.setattr(
        "babelarr.cli.requests.head",
        lambda url, timeout: SimpleNamespace(status_code=200),
    )
    with caplog.at_level(logging.WARNING, logger="babelarr.cli"):
        validate_environment(config)
    assert config.mkv_dirs == [str(valid_dir)]
    assert config.root_dirs == [str(valid_dir)]
    assert "missing_mkv_dir" in caplog.text
