import logging
import pytest
import requests

from babelarr.app import Application
from babelarr.config import Config


def test_translate_file(tmp_path, monkeypatch):
    # Create a dummy English subtitle file
    tmp_file = tmp_path / 'sample.en.srt'
    tmp_file.write_text('1\n00:00:00,000 --> 00:00:02,000\nHello\n')

    # Prepare a fake response from the translation API
    class DummyResponse:
        status_code = 200
        content = b'1\n00:00:00,000 --> 00:00:02,000\nHallo\n'

        def raise_for_status(self):
            pass

    def fake_post(url, files, data, timeout):
        return DummyResponse()

    # Patch requests.post to use the fake response
    monkeypatch.setattr(requests, 'post', fake_post)

    # Invoke translation and verify output file
    app = Application(
        Config(
            root_dirs=[str(tmp_path)],
            target_langs=["nl"],
            src_ext=".en.srt",
            api_url="http://example",
            workers=1,
            queue_db=str(tmp_path / "queue.db"),
        )
    )

    app.translate_file(tmp_file, 'nl')
    output_file = tmp_file.with_suffix('.nl.srt')
    assert output_file.exists()
    assert output_file.read_bytes() == DummyResponse.content
    app.db.close()


@pytest.mark.parametrize("status", [400, 403, 404, 429, 500])
def test_translate_file_errors(tmp_path, monkeypatch, status, caplog):
    tmp_file = tmp_path / "sample.en.srt"
    tmp_file.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")

    class DummyErrorResponse:
        def __init__(self, status_code):
            self.status_code = status_code
            self.headers = {}
            self.text = "error"
            self.content = b""

        def json(self):
            return {"error": "something"}

        def raise_for_status(self):
            raise requests.HTTPError(response=self)

    def fake_post(url, files, data, timeout):
        return DummyErrorResponse(status)

    monkeypatch.setattr(requests, "post", fake_post)
    app = Application(
        Config(
            root_dirs=[str(tmp_path)],
            target_langs=["nl"],
            src_ext=".en.srt",
            api_url="http://example",
            workers=1,
            queue_db=str(tmp_path / "queue.db"),
        )
    )
    try:
        with caplog.at_level(logging.ERROR):
            with pytest.raises(requests.HTTPError):
                app.translate_file(tmp_file, "nl")
            assert str(status) in caplog.text
    finally:
        app.db.close()
