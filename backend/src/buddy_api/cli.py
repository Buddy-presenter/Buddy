"""Console entry point: validate before binding a server socket."""

import sys

import uvicorn

from buddy_api.app import create_app
from buddy_api.config import ConfigurationError, Settings


def main() -> int:
    try:
        config = Settings.from_environment()
    except ConfigurationError as exc:
        print(f"Buddy API configuration error: {exc}", file=sys.stderr)
        return 2
    uvicorn.run(
        create_app(config),
        host=config.bind_host,
        port=config.port,
        access_log=False,
        log_level="warning",
        proxy_headers=False,
    )
    return 0
