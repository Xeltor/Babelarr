FROM python:3.11-slim

RUN pip install --no-cache-dir watchdog schedule requests

WORKDIR /app
COPY main.py .

# Example environment variables:
#   -e WATCH_DIRS="/subs:/incoming"
#   -e TARGET_LANGS="nl,bs"
CMD ["python", "main.py"]
