"""Add ECS-assigned IPs to exact allowed hosts so ALB readiness checks can reach the API."""

import ipaddress
import json
import os
import sys
from urllib.parse import urlsplit
from urllib.request import urlopen


def metadata_hosts(uri: str) -> list[str]:
    parsed = urlsplit(uri)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "169.254.170.2"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/v4/")
    ):
        raise ValueError("Unexpected ECS metadata address.")
    with urlopen(uri, timeout=3) as response:
        data = response.read(65537)
        if len(data) > 65536:
            raise ValueError("Oversized ECS metadata response.")
        value = json.loads(data)
    return [
        str(ipaddress.IPv4Address(address))
        for network in value.get("Networks", [])
        for address in network.get("IPv4Addresses", [])
    ]


def main() -> int:
    try:
        if uri := os.environ.get("ECS_CONTAINER_METADATA_URI_V4"):
            hosts = metadata_hosts(uri)
            if not hosts:
                raise ValueError("Missing ECS task address.")
            os.environ["BUDDY_ALLOWED_HOSTS"] = ",".join(
                [os.environ.get("BUDDY_ALLOWED_HOSTS", "localhost"), *hosts]
            )
        if len(sys.argv) < 2:
            raise ValueError("Missing container command.")
        os.execvp(sys.argv[1], sys.argv[1:])
    except Exception:
        print(
            "Container startup failed; verify metadata and deployment configuration.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
