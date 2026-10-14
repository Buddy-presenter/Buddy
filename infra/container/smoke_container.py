"""Verify the real container listens and does not claim an unconfigured DB is ready."""

import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

for attempt in range(30):
    try:
        with urlopen("http://127.0.0.1:8000/health", timeout=2) as response:
            assert json.load(response)["status"] == "ok"
        break
    except (URLError, OSError):
        if attempt == 29:
            raise
        time.sleep(1)
try:
    urlopen("http://127.0.0.1:8000/api/v1/ready", timeout=3).close()
    raise AssertionError("Unconfigured database must not be ready.")
except HTTPError as response:
    assert response.code == 503
    response.close()
print("API container startup passed; missing database correctly blocks readiness.")
