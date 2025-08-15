FROM python:3.11-slim

# Create non-root user (uid/gid 1000)
RUN groupadd -g 1000 app && useradd -u 1000 -g app -d /app -m app

WORKDIR /app
COPY pyproject.toml ./
COPY babelarr ./babelarr
RUN pip install --no-cache-dir .

# Create mount points for subtitles and the persistent queue database
RUN mkdir -p /data /config && chown -R app:app /data /config /app

USER app:app

# Example environment variables:
#   -e WATCH_DIRS="/subs:/incoming"
#   -e TARGET_LANGS="nl,bs"
CMD ["babelarr"]
