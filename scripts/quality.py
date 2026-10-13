"""Run local quality tools with the active interpreter and shared repository settings."""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPLICATIONS = {
    "desktop": ROOT / "src" / "buddy_desktop",
    "api": ROOT / "src" / "buddy_api",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("application", choices=[*APPLICATIONS, "all"])
    parser.add_argument("command", choices=["format", "format-check", "lint", "type", "check"])
    args = parser.parse_args()
    targets = (
        list(APPLICATIONS.values())
        if args.application == "all"
        else [APPLICATIONS[args.application]]
    )
    for target in targets:
        if not target.is_dir():
            print(f"Application source is missing: {target.relative_to(ROOT)}", file=sys.stderr)
            return 2
    # The all target checks shared scripts/tests as well as both application packages.
    formatting_paths = ["."] if args.application == "all" else [str(targets[0])]
    typing_paths = [str(target) for target in targets]
    operations = {
        "format": ["ruff", "format", *formatting_paths],
        "format-check": ["ruff", "format", "--check", *formatting_paths],
        "lint": ["ruff", "check", *formatting_paths],
        "type": ["mypy", *typing_paths],
    }
    commands = ["format-check", "lint", "type"] if args.command == "check" else [args.command]
    for command in commands:
        operation = operations[command]
        print(f"Running {args.application} {command}", flush=True)
        result = subprocess.run([sys.executable, "-m", *operation], cwd=ROOT, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
