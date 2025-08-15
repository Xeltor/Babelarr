# Babelarr

A lightweight subtitle translator that watches directories for `.en.srt` files and uses [LibreTranslate](https://libretranslate.com/) to generate translations such as Dutch and Bosnian. Files are discovered through a watchdog and periodic scans, queued, and translated sequentially.

## Usage

Build the container:

```bash
docker build -t babelarr .
```

Run the container alongside a LibreTranslate instance:

```bash
docker run -d --name babelarr \
  --network subtitles \
  -v /path/to/subtitles:/data \
  -v /path/to/config:/config \
  -e WATCH_DIRS="/data" \
  -e TARGET_LANGS="nl,bs" \
  -e LIBRETRANSLATE_URL="http://libretranslate:5000" \
  -e LOG_LEVEL="INFO" \
  babelarr
```

`LIBRETRANSLATE_URL` should include only the protocol, hostname or IP, and port of your LibreTranslate instance. The `translate_file` API path is appended automatically.

The application scans for new `.en.srt` files on startup, upon file creation and every hour thereafter. Translated subtitles are saved beside the source file with language suffixes (e.g. `.nl.srt`, `.bs.srt`).

Existing subtitles that are modified or moved are re-queued for translation after a short debounce to ensure the file is fully written.

Queued translation tasks are stored in a small SQLite database (`/config/queue.db` by default) so that pending work survives
container recreations. Mount the `/config` directory to a persistent location on the host to retain the queue.

The container runs as a non-root user with UID and GID `1000`. Ensure the host paths mounted at `/data` and `/config` are writable by this user.

`LOG_LEVEL` controls the verbosity of console output and accepts standard logging levels such as `DEBUG`, `INFO`, `WARNING` and `ERROR`.

`WORKERS` sets the maximum number of concurrent translation threads. Values above 10 are capped to prevent LibreTranslate from becoming unstable due to excessive threading.

`DEBOUNCE_SECONDS` configures the delay used to verify that a file has finished writing before it is queued. Increase this value if subtitle files are large or written slowly.


## Testing

Run the test suite with [pytest](https://docs.pytest.org/):

```bash
pytest
```

## Development

This project uses [pre-commit](https://pre-commit.com/) to lint and type-check
the codebase.

Install the hooks:

```bash
pip install pre-commit
pre-commit install
```

Run all checks:

```bash
pre-commit run --all-files
```
