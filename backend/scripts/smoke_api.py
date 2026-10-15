"""Start the real ASGI server on an isolated socket and verify HTTP readiness contracts."""

import argparse
import json
import socket
import sys
import time
from threading import Thread
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import uvicorn

from buddy_api.app import create_app
from buddy_api.config import Environment, Settings
from buddy_api.persistence.settings import DatabaseSettings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        action="store_true",
        help="Use BUDDY_DATABASE_URL and require real database readiness.",
    )
    args = parser.parse_args()
    database = DatabaseSettings.from_environment() if args.database else None
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        port = listener.getsockname()[1]
        service = uvicorn.Server(
            uvicorn.Config(
                create_app(Settings(Environment.TEST, database=database)),
                access_log=False,
                log_level="warning",
                proxy_headers=False,
            )
        )
        thread = Thread(target=service.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        result = 0
        try:
            while not service.started:
                if not thread.is_alive() or time.monotonic() >= deadline:
                    raise RuntimeError("API did not start within the smoke-check deadline.")
                time.sleep(0.02)
            base_url = f"http://127.0.0.1:{port}"
            for path in ["/health", "/api/v1/health", "/api/v1/version"]:
                with urlopen(base_url + path, timeout=2) as response:
                    payload = json.load(response)
                    assert response.headers.get_content_type() == "application/json"
                    if path == "/health":
                        assert payload["status"] == "ok"
                    else:
                        assert payload["request_id"] == response.headers["X-Request-ID"]
            if database is not None:
                with urlopen(base_url + "/api/v1/ready", timeout=5) as response:
                    assert json.load(response)["data"]["dependencies"] == [
                        {"name": "database", "status": "ready"}
                    ]
            else:
                try:
                    urlopen(base_url + "/api/v1/ready", timeout=2).close()
                    raise AssertionError("Unconfigured database must fail readiness.")
                except HTTPError as response:
                    assert response.code == 503
                    payload = json.load(response)
                    assert payload["request_id"] == response.headers["X-Request-ID"]
                    assert payload["error"]["details"] == [
                        {"location": "database", "code": "dependency_unavailable"}
                    ]
            print("Real API HTTP startup and readiness checks passed.")
        except (AssertionError, RuntimeError, URLError, OSError, ValueError) as exc:
            print(f"API smoke check failed: {type(exc).__name__}", file=sys.stderr)
            result = 1
        finally:
            service.should_exit = True
            thread.join(timeout=5)
            if thread.is_alive():
                print("API did not shut down within the smoke-check deadline.", file=sys.stderr)
                result = 1
        return result


if __name__ == "__main__":
    raise SystemExit(main())
