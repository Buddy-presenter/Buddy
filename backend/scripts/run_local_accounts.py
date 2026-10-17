"""Start a local account API using existing Cognito, never a mock email provider.

Configure/migrate a local PostgreSQL database first. No AWS resources are modified.
"""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from buddy_api.cli import main


def configure() -> None:
    if urlsplit(os.environ.get("BUDDY_DATABASE_URL", "")).hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("Set BUDDY_DATABASE_URL to your migrated local PostgreSQL database.")
    if os.environ.get("BUDDY_ENVIRONMENT", "development") != "development":
        raise ValueError("This launcher is for local development only.")
    os.environ.setdefault("BUDDY_ENVIRONMENT", "development")
    os.environ.setdefault("BUDDY_BIND_HOST", "127.0.0.1")
    os.environ.setdefault("BUDDY_ALLOWED_HOSTS", "localhost,127.0.0.1,::1")
    if os.environ["BUDDY_BIND_HOST"] not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("The local account API must bind to loopback.")
    root = Path(__file__).resolve().parents[2]
    public = json.loads((root / "infra/staging/public-config.json").read_text())
    for name, value in public.items():
        if name != "BUDDY_API_URL":
            os.environ.setdefault(name, value)


def run() -> int:
    try:
        configure()
    except ValueError as exc:
        print(str(exc))
        return 2
    return main()


if __name__ == "__main__":
    raise SystemExit(run())
