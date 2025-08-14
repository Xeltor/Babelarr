import requests
from main import translate_file


def test_translate_file(tmp_path, monkeypatch):
    # Create a dummy English subtitle file
    tmp_file = tmp_path / 'sample.en.srt'
    tmp_file.write_text('1\n00:00:00,000 --> 00:00:02,000\nHello\n')

    # Prepare a fake response from the translation API
    class DummyResponse:
        content = b'1\n00:00:00,000 --> 00:00:02,000\nHallo\n'

        def raise_for_status(self):
            pass

    def fake_post(url, files, data, timeout):
        return DummyResponse()

    # Patch requests.post to use the fake response
    monkeypatch.setattr(requests, 'post', fake_post)

    # Invoke translation and verify output file
    translate_file(tmp_file, 'nl')
    output_file = tmp_file.with_suffix('.nl.srt')
    assert output_file.exists()
    assert output_file.read_bytes() == DummyResponse.content
