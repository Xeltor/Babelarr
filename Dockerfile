FROM python:3.11-slim

# Install MKV tooling and media utilities needed for embedded subtitle tagging
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        mkvtoolnix \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user (uid/gid 1000)
RUN groupadd -g 1000 app && useradd -u 1000 -g app -d /app -m app

WORKDIR /app
COPY pyproject.toml ./
COPY babelarr ./babelarr
RUN pip install --no-cache-dir .

COPY . .
RUN chmod +x docker-entrypoint.sh
# Create mount points for subtitles and the persistent queue database
RUN mkdir -p /data /config && chown -R app:app /data /config /app

USER app:app

# Example environment variables:
#   -e WATCH_DIRS="/subs:/incoming"
#   -e ENSURE_LANGS="en,nl,bs"
ENTRYPOINT ["./docker-entrypoint.sh"]
