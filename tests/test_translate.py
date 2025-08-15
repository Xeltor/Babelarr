import logging

import pytest
import requests

from babelarr.app import Application
from babelarr.config import Config


class DummyTranslator:
    result = b"1\n00:00:00,000 --> 00:00:02,000\nHallo\n"

    def translate(self, path, lang):
        return self.result


def test_translate_file(tmp_path):
    # Create a dummy English subtitle file
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    translator = DummyTranslator()
    app = Application(
        Config(
            root_dirs=[str(tmp_path)],
            target_langs=["nl"],
            src_ext=".en.srt",
            api_url="http://example",
            workers=1,
            queue_db=str(tmp_path / "queue.db"),
        ),
        translator,
    )

    app.translate_file(tmp_file, "nl")
    output_file = tmp_file.with_suffix(".nl.srt")
    assert output_file.exists()
    assert output_file.read_bytes() == translator.result
    app.db.close()


@pytest.mark.parametrize("status", [400, 403, 404, 429, 500])
def test_translate_file_errors(tmp_path, status, caplog):
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    class ErrorTranslator:
        def __init__(self, status_code):
            self.status_code = status_code

        def translate(self, path, lang):
            logger = logging.getLogger("babelarr")
            logger.error("HTTP %s from LibreTranslate: boom", self.status_code)
            resp = requests.Response()
            resp.status_code = self.status_code
            raise requests.HTTPError(response=resp)

    translator = ErrorTranslator(status)
    app = Application(
        Config(
            root_dirs=[str(tmp_path)],
            target_langs=["nl"],
            src_ext=".en.srt",
            api_url="http://example",
            workers=1,
            queue_db=str(tmp_path / "queue.db"),
        ),
        translator,
    )
    try:
        with caplog.at_level(logging.ERROR):
            with pytest.raises(requests.HTTPError):
                app.translate_file(tmp_file, "nl")
            assert str(status) in caplog.text
    finally:
        app.db.close()
