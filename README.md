# Babelarr

A lightweight subtitle translator that watches directories for subtitle files in a configurable source language (default `.en.srt`) and uses [LibreTranslate](https://libretranslate.com/) to generate translations such as Dutch and Bosnian. Files are discovered through a watchdog and periodic scans, queued, and translated sequentially.

## Prerequisites

- Python 3.12 or newer

## Usage and Installation

### Quick Start (Non-Docker)

Create and activate a virtual environment, install Babelarr, and configure the required environment variables:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .  # or: pip install babelarr

export WATCH_DIRS="/path/to/subtitles"
export LIBRETRANSLATE_URL="http://localhost:5000"

babelarr
```

All subsequent `make` and CLI commands should be executed with the virtual environment activated.

### Docker

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
  -e LOG_FILE="/config/babelarr.log" \
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
| `JELLYFIN_URL` | *(unset)* | Base URL of the Jellyfin server for library refreshes. |
| `JELLYFIN_TOKEN` | *(unset)* | API token for Jellyfin requests. |
| `LOG_LEVEL` | `INFO` | Controls verbosity of console output. |
| `LOG_FILE` | *(unset)* | If set, writes logs to the specified file. |
| `WORKERS` | `1` | Number of translation worker threads (maximum 10). |
| `RETRY_COUNT` | `3` | Translation retry attempts. |
| `BACKOFF_DELAY` | `1` | Initial delay between retries in seconds. |
| `DEBOUNCE_SECONDS` | `0.1` | Wait time to ensure files have finished writing before enqueueing. |
| `STABILIZE_TIMEOUT` | `30` | Max seconds to wait for a subtitle file to stop growing before enqueueing. |
| `SCAN_INTERVAL_MINUTES` | `60` | Minutes between full directory scans. |
| `AVAILABILITY_CHECK_INTERVAL` | `30` | Seconds between checks for LibreTranslate availability. |
| `HTTP_TIMEOUT` | `30` | Timeout in seconds for non-translation HTTP requests. |
| `TRANSLATION_TIMEOUT` | `900` | Timeout in seconds for translation requests. |
| `QUEUE_DB` | `/config/queue.db` | Path to the SQLite queue database. |
| `MKV_DIRS` | *(defaults to `WATCH_DIRS`)* | Colon-separated directories to scan for MKV files when tagging embedded subtitles. |
| `MKV_SCAN_INTERVAL_MINUTES` | `180` | Minutes between MKV rescans. |
| `MKV_SAMPLE_BYTES` | `8192` | Maximum bytes to sample from each subtitle stream before detection. |
| `MKV_MIN_CONFIDENCE` | `0.85` | Minimum LibreTranslate confidence required before applying a language tag. |
| `MKV_CACHE_PATH` | `/config/mkv-cache.json` | Location of the JSON cache that tracks processed MKV files. |

If `TARGET_LANGS` is empty or only contains invalid entries, the application raises a `ValueError` during startup.

Command-line options `--log-level` and `--log-file` override the `LOG_LEVEL` and `LOG_FILE` environment variables respectively.

`LIBRETRANSLATE_URL` should include only the protocol, hostname or IP, and port of your LibreTranslate instance. The `translate_file` API path is appended automatically.

Queued translation tasks are stored in a small SQLite database (`/config/queue.db`) so that pending work survives container recreations. Mount the `/config` directory to a persistent location on the host to retain the queue.

Check the current queue with:

```bash
babelarr queue [--list]
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

After activating the virtual environment, run `make setup` to install development dependencies and pre-commit hooks. The provided Makefile wraps common development tasks:

```bash
make setup  # install dev dependencies and pre-commit hooks
make lint   # format and lint the codebase
make test   # run the test suite
make check  # lint and tests together
```

These targets invoke [pre-commit](https://pre-commit.com/) and [pytest](https://docs.pytest.org/) under the hood.

## License

This project is licensed under the [MIT License](LICENSE).
