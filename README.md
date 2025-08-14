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
  -e WATCH_DIRS="/data" \
  -e TARGET_LANGS="nl,bs" \
  -e LIBRETRANSLATE_URL="http://libretranslate:5000/translate" \
  -e LOG_LEVEL="INFO" \
  babelarr
```

The application scans for new `.en.srt` files on startup, upon file creation and every hour thereafter. Translated subtitles are saved beside the source file with language suffixes (e.g. `.nl.srt`, `.bs.srt`).

Queued translation tasks are stored in a small SQLite database (`queue.db` by default) so that pending work survives restarts.

`LOG_LEVEL` controls the verbosity of console output and accepts standard logging levels such as `DEBUG`, `INFO`, `WARNING` and `ERROR`.

