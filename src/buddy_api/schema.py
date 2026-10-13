"""Generate a deterministic contract and conservatively reject breaking API drift."""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from buddy_api.app import create_app
from buddy_api.config import Environment, Settings

SCHEMA_PATH = Path("docs/api/openapi.json")
DOCUMENTATION_KEYS = {"description", "title", "summary", "examples", "example", "tags"}


def generate_schema() -> dict[str, Any]:
    # Exporting contracts does not read production secrets or start external services.
    return create_app(Settings(Environment.TEST)).openapi()


def breaking_changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Allow additions to named maps, but conservatively reject existing contract changes."""
    failures: list[str] = []
    maps = {"paths", "schemas", "properties", "responses", "content"}

    def compare(old: Any, new: Any, path: str, *, allow_additions: bool = False) -> None:
        if isinstance(old, dict) and isinstance(new, dict):
            for key, value in old.items():
                if key in DOCUMENTATION_KEYS or key.startswith("x-"):
                    continue
                if key not in new:
                    failures.append(f"Removed {path}/{key}")
                else:
                    additions = key in maps or (path == "/paths")
                    compare(value, new[key], f"{path}/{key}", allow_additions=additions)
            if not allow_additions:
                for key in new.keys() - old.keys():
                    if key not in DOCUMENTATION_KEYS and not key.startswith("x-"):
                        failures.append(f"Changed contract at {path}/{key}")
        elif old != new:
            failures.append(f"Changed contract at {path}")

    for path, operations in before.get("paths", {}).items():
        current = after.get("paths", {}).get(path)
        if current is None:
            failures.append(f"Removed endpoint {path}")
        else:
            compare(operations, current, f"/paths/{path}", allow_additions=True)
    compare(
        before.get("components", {}).get("schemas", {}),
        after.get("components", {}).get("schemas", {}),
        "/components/schemas",
        allow_additions=True,
    )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=SCHEMA_PATH)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--compare-ref", help="Compare with a Git ref, e.g. origin/main, in CI.")
    args = parser.parse_args()
    rendered = json.dumps(generate_schema(), indent=2, sort_keys=True) + "\n"
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
            print(
                "OpenAPI snapshot is stale. Regenerate it and review the contract change.",
                file=sys.stderr,
            )
            return 1
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    if args.compare_ref:
        if args.compare_ref.startswith("-"):
            parser.error("The baseline must be a Git ref, not a command option.")
        ref = subprocess.run(
            ["git", "rev-parse", "--verify", args.compare_ref], capture_output=True, text=True
        )
        if ref.returncode:
            print("Cannot resolve the baseline Git ref.", file=sys.stderr)
            return 2
        baseline = subprocess.run(
            ["git", "show", f"{ref.stdout.strip()}:{SCHEMA_PATH.as_posix()}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if baseline.returncode:
            print("Baseline has no API snapshot; this PR introduces the first contract.")
            return 0
        try:
            changes = breaking_changes(json.loads(baseline.stdout), json.loads(rendered))
        except (ValueError, TypeError):
            print("The baseline API schema is invalid.", file=sys.stderr)
            return 2
        if changes:
            print("Potentially breaking API changes require a new API version:", file=sys.stderr)
            for change in changes:
                print(f"- {change}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
