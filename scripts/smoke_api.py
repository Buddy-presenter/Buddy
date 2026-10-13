"""Start the real ASGI server on an isolated socket and verify HTTP and desktop integration."""

import json
import socket
import sys
import time
from threading import Thread
from urllib.error import URLError
from urllib.request import urlopen

import uvicorn

from buddy_api.app import create_app
from buddy_api.config import Environment, Settings
from buddy_desktop.config import AppConfig
from buddy_desktop.services import HealthService


def main() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        port = listener.getsockname()[1]
        service = uvicorn.Server(
            uvicorn.Config(
                create_app(Settings(Environment.TEST)),
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
            assert HealthService(AppConfig(base_url, 2)).check().status == "ok"
            for path in ["/api/v1/health", "/api/v1/ready", "/api/v1/version"]:
                with urlopen(base_url + path, timeout=2) as response:
                    payload = json.load(response)
                    assert payload["request_id"] == response.headers["X-Request-ID"]
            print("Real API HTTP startup, readiness and desktop health integration passed.")
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
