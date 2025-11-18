# Babelarr

Babelarr now treats `.mkv` files as the primary source: it scans every configured `WATCH_DIRS`/`MKV_DIRS`, tags undefined subtitle streams, and translates any missing languages from your ordered `ENSURE_LANGS` list by calling [LibreTranslate](https://libretranslate.com/). Translated `.lang.srt` files are written beside the MKV (or your preferred watch directory), and the first language in `ENSURE_LANGS` is preferred as the source while other detected tracks serve as fallbacks.

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
  -e ENSURE_LANGS="en,nl,bs" \
  -e LIBRETRANSLATE_URL="http://libretranslate:5000" \
  -e LOG_LEVEL="INFO" \
  -e LOG_FILE="/config/babelarr.log" \
  babelarr
```

### Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `WATCH_DIRS` | `/data` | Colon-separated directories to scan for subtitles. |
| `ENSURE_LANGS` | `en,nl,bs` | Ordered comma-separated languages that Babelarr should ensure are available; missing languages are translated from the others, and the first entry is preferred as the default source. |
| `SRC_LANG` | `en` | (Legacy) Two-letter source language that `ENSURE_LANGS` falls back to when not set. |
| `TARGET_LANGS` | `nl,bs` | (Legacy) Backward-compatible list of target languages that `ENSURE_LANGS` also defaults to when unspecified. |
| `LIBRETRANSLATE_URL` | `http://libretranslate:5000` | Base URL of the LibreTranslate instance (no path). |
| `LIBRETRANSLATE_API_KEY` | *(unset)* | API key for authenticated LibreTranslate instances. |
| `JELLYFIN_URL` | *(unset)* | Base URL of the Jellyfin server for library refreshes. |
| `JELLYFIN_TOKEN` | *(unset)* | API token for Jellyfin requests. |
| `LOG_LEVEL` | `INFO` | Controls verbosity of console output. |
| `LOG_FILE` | *(unset)* | If set, writes logs to the specified file. |
| `WORKERS` | `1` | Number of translation worker threads (maximum 10). |
| `RETRY_COUNT` | `3` | Translation retry attempts. |
| `BACKOFF_DELAY` | `1` | Initial delay between retries in seconds. |
| `DEBOUNCE_SECONDS` | `0.1` | Wait time to ensure files have finished writing before rescheduling a scan. |
| `STABILIZE_TIMEOUT` | `30` | Max seconds to wait for a subtitle file to stop growing before translating. |
| `SCAN_INTERVAL_MINUTES` | `60` | Minutes between full directory scans. |
| `AVAILABILITY_CHECK_INTERVAL` | `30` | Seconds between checks for LibreTranslate availability. |
| `HTTP_TIMEOUT` | `30` | Timeout in seconds for non-translation HTTP requests. |
| `TRANSLATION_TIMEOUT` | `900` | Timeout in seconds for translation requests. |
| `LIBRETRANSLATE_MAX_CONCURRENT_REQUESTS` | `10` | Maximum number of LibreTranslate requests (translations or detections) allowed simultaneously. |
| `MKV_DIRS` | *(defaults to `WATCH_DIRS`)* | Colon-separated directories to scan for MKV files when tagging embedded subtitles. |
| `MKV_SCAN_INTERVAL_MINUTES` | `180` | Minutes between MKV rescans. |
| `MKV_MIN_CONFIDENCE` | `0.85` | Minimum LibreTranslate confidence required before applying a language tag. |
| `MKV_CACHE_PATH` | `/config/mkv-cache.db` | Path for the SQLite cache that tracks processed MKV files. |
| `MKV_CACHE_ENABLED` | `true` | Disable to force reprocessing of MKVs without reading/writing the cache (useful for testing). |

If `ENSURE_LANGS` is empty or only contains invalid entries, the application raises a `ValueError` during startup; when `ENSURE_LANGS` is unset it falls back to `SRC_LANG` plus `TARGET_LANGS` for backwards compatibility.

Command-line options `--log-level` and `--log-file` override the `LOG_LEVEL` and `LOG_FILE` environment variables respectively.

`LIBRETRANSLATE_URL` should include only the protocol, hostname or IP, and port of your LibreTranslate instance. The `translate_file` API path is appended automatically.

The application scans MKV directories on startup, after file changes, and at a configurable interval (default every 180 minutes) thereafter. Translated subtitles are saved beside the MKV with language suffixes (e.g. `.nl.srt`, `.bs.srt`), and the watcher waits for files to stabilize before scheduling a rescan so ongoing writes don’t interfere with translation.

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
