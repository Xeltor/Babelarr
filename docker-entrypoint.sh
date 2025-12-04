#!/bin/sh
set -euo pipefail

SKIP_LT_HEALTH_CHECK=${SKIP_LT_HEALTH_CHECK:-false}

wait_for_libretranslate() {
  python3 <<'PY'
import os
import sys
import time
import urllib.request
import urllib.error

libretranslate_url = os.environ.get("LIBRETRANSLATE_URL", "http://libretranslate:5000")
path = "/health"
url = f"{libretranslate_url.rstrip('/')}{path}"
timeout = float(os.environ.get("LIBRETRANSLATE_WAIT_TIMEOUT", "120"))
interval = float(os.environ.get("LIBRETRANSLATE_WAIT_INTERVAL", "2"))
deadline = time.time() + timeout

while True:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            status = response.getcode()
            if 200 <= status < 300:
                print(f"LibreTranslate at {url} reported {status}; continuing startup.")
                break
            print(f"LibreTranslate health returned {status}; retrying in {interval}s.")
    except Exception as exc:  # pylint: disable=broad-except
        print(f"Waiting for LibreTranslate health ({exc})")
    if time.time() >= deadline:
        sys.exit(1)
    time.sleep(max(0.1, interval))
PY
}

if [ "$SKIP_LT_HEALTH_CHECK" != "true" ]; then
  wait_for_libretranslate
else
  echo "Skipping LibreTranslate health check because SKIP_LT_HEALTH_CHECK is true."
fi

exec babelarr "$@"
