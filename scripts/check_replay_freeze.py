"""Reject edits to established evaluation fixtures/gates; add a new version instead."""

import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "tests/fixtures/replay/"


def check(reference: str) -> list[str]:
    files = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", reference, "--", PREFIX],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    changed = []
    for name in files:
        if not name.endswith(".json"):
            continue
        original = subprocess.run(
            ["git", "show", f"{reference}:{name}"], cwd=ROOT, check=True, capture_output=True
        ).stdout
        path = ROOT / name
        if not path.is_file() or path.read_bytes() != original:
            changed.append(name)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", default="origin/main")
    args = parser.parse_args()
    try:
        changed = check(args.base_ref)
    except (OSError, subprocess.CalledProcessError):
        parser.exit(2, "Cannot read the evaluation baseline Git reference.\n")
    if changed:
        print("Frozen evaluation inputs changed; use an independently reviewed new version:")
        print("\n".join(changed))
        return 1
    print("Established replay fixtures and gates are unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
