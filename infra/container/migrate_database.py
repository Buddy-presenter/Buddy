"""Run explicit migrations before deployment and protect migration metadata from runtime roles."""

import asyncio
import os
import subprocess
import sys

import asyncpg


async def restrict_metadata() -> None:
    connection = await asyncpg.connect(
        os.environ["BUDDY_DATABASE_URL"], timeout=10, command_timeout=30
    )
    try:
        await connection.execute(
            "REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM buddy_api, buddy_worker"
        )
    finally:
        await connection.close()


def main() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "buddy_api.persistence.migrate", "upgrade"], check=False
    )
    if result.returncode:
        return result.returncode
    try:
        asyncio.run(restrict_metadata())
    except Exception:
        print(
            "Migration metadata permissions could not be secured; deployment must stop.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
