"""Repeatable tests; explicit database runs cannot succeed by skipping integration."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "target",
        choices=("all", "desktop", "api", "database", "integration", "repository", "worker"),
    )
    args = parser.parse_args(argv)
    if args.target == "worker":
        parser.exit(
            2, "Worker application smoke is blocked: implement the worker in issue #8 first.\n"
        )
    if args.target in {"database", "integration"} and not os.environ.get("BUDDY_TEST_DATABASE_URL"):
        parser.exit(
            2, "Database tests require BUDDY_TEST_DATABASE_URL (disposable *_test database).\n"
        )
    command = [sys.executable, "-m", "pytest", "--strict-markers"]
    paths = {
        "all": ["frontend/tests", "backend/tests", "tests/integration", "tests/repository"],
        "desktop": ["frontend/tests"],
        "api": ["backend/tests"],
        "database": ["backend/tests/api"],
        "integration": ["tests/integration"],
        "repository": ["tests/repository"],
    }
    if args.target in {"api", "database"}:
        command += ["-p", "no:pytest-qt"]
    command += paths[args.target]
    if args.target == "database":
        command += ["-m", "postgres"]
    return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
