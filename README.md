# Babelarr

A lightweight subtitle translator that watches directories for subtitle files in a configurable source language (default `.en.srt`) and uses [LibreTranslate](https://libretranslate.com/) to generate translations such as Dutch and Bosnian. Files are discovered through a watchdog and periodic scans, queued, and translated sequentially.

## Usage and Installation

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
  -e SRC_LANG="en" \
  -e TARGET_LANGS="nl,bs" \
  -e LIBRETRANSLATE_URL="http://libretranslate:5000" \
  -e LOG_LEVEL="INFO" \
  babelarr
```

### Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `WATCH_DIRS` | `/data` | Colon-separated directories to scan for subtitles. |
| `TARGET_LANGS` | `nl,bs` | Comma-separated language codes to translate into. Must include at least one valid code; startup fails if none remain after filtering. |
| `SRC_LANG` | `en` | Two-letter source language of existing subtitles; files matching `*.LANG.srt` are processed. |
| `LIBRETRANSLATE_URL` | `http://libretranslate:5000` | Base URL of the LibreTranslate instance (no path). |
| `LIBRETRANSLATE_API_KEY` | *(unset)* | API key for authenticated LibreTranslate instances. |
| `LOG_LEVEL` | `INFO` | Controls verbosity of console output. |
| `WORKERS` | `1` | Number of translation worker threads (maximum 10). |
| `RETRY_COUNT` | `3` | Translation retry attempts. |
| `BACKOFF_DELAY` | `1` | Initial delay between retries in seconds. |
| `DEBOUNCE_SECONDS` | `0.1` | Wait time to ensure files have finished writing before enqueueing. |
| `SCAN_INTERVAL_MINUTES` | `60` | Minutes between full directory scans. |

If `TARGET_LANGS` is empty or only contains invalid entries, the application raises a `ValueError` during startup.

`LIBRETRANSLATE_URL` should include only the protocol, hostname or IP, and port of your LibreTranslate instance. The `translate_file` API path is appended automatically.

Queued translation tasks are stored in a small SQLite database (`/config/queue.db`) so that pending work survives container recreations. Mount the `/config` directory to a persistent location on the host to retain the queue.

Check the current queue with:

```bash
babelarr queue --status [--list]
```

The command prints the number of pending items and, with `--list`, each queued path.

The application scans for new source-language subtitles on startup, upon file creation and at a configurable interval (default every 60 minutes) thereafter. Translated subtitles are saved beside the source file with language suffixes (e.g. `.nl.srt`, `.bs.srt`). Existing subtitles that are modified or moved are re-queued for translation after a short debounce to ensure the file is fully written.

If LibreTranslate is unreachable at startup or during operation, Babelarr logs the outage and pauses worker threads until the service becomes available again.

The container runs as a non-root user with UID and GID `1000`. Ensure the host paths mounted at `/data` and `/config` are writable by this user.

Example `docker-compose.yml`:

```yaml
version: "3.8"

services:
  libretranslate:
    image: libretranslate/libretranslate:latest
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - ./libretranslate-data:/data

  babelarr:
    build: .
    depends_on:
      - libretranslate
    volumes:
      - ./subtitles:/data
      - ./config:/config
    environment:
      WATCH_DIRS: "/data"
      SRC_LANG: "en"
      TARGET_LANGS: "nl,bs"
      LIBRETRANSLATE_URL: "http://libretranslate:5000"
```

## Development

Run the test suite with [pytest](https://docs.pytest.org/):

```bash
pytest
```

This project uses [pre-commit](https://pre-commit.com/) to lint and type-check the codebase.

Install the hooks:

```bash
pip install pre-commit
pre-commit install
```

Run all checks:

```bash
pre-commit run --all-files
```
