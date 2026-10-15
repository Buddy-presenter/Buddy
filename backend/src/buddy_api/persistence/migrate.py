"""Safe operator entry point: upgrade/check/current; no automatic startup migrations."""

import argparse
import os
import sys

from alembic import command

from buddy_api.persistence.migration_config import migration_config
from buddy_api.persistence.settings import DatabaseConfigurationError, DatabaseSettings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["upgrade", "check", "current"])
    args = parser.parse_args()
    try:
        settings = DatabaseSettings.from_environment(
            production=os.environ.get("BUDDY_ENVIRONMENT") == "production"
        )
    except DatabaseConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        config = migration_config(settings)
        if args.operation == "upgrade":
            command.upgrade(config, "head")
        elif args.operation == "check":
            command.check(config)
        else:
            command.current(config, check_heads=True)
    except Exception:
        print(
            "Database migration operation failed. Check connectivity, permissions, "
            "schema existence and revision history; "
            "restore from a verified backup before recovery.",
            file=sys.stderr,
        )
        return 1
    print("Database " + args.operation + " completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
