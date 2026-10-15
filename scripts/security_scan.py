"""Block high-severity findings and incomplete Bandit scans, including scanner errors."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def scan_targets(root: Path) -> list[Path]:
    """Return every maintained application and operational code directory."""
    return [
        root / "frontend/src",
        root / "backend/src",
        root / "shared/src",
        root / "frontend/scripts",
        root / "backend/scripts",
        root / "backend/migrations",
        root / "scripts",
    ]


def complete_and_clean(report: object) -> bool:
    if not isinstance(report, dict):
        return False
    metrics = report.get("metrics", {}).get("_totals", {})
    return report.get("errors") == [] and report.get("results") == [] and metrics.get("loc", 0) > 0


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "bandit.json"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "bandit",
                "-r",
                *(str(path.relative_to(root)) for path in scan_targets(root)),
                "--severity-level",
                "high",
                "--confidence-level",
                "all",
                "-f",
                "json",
                "-o",
                str(output),
            ],
            cwd=root,
            check=False,
        )
        try:
            report = json.loads(output.read_text(encoding="utf-8"))
            passed = result.returncode == 0 and complete_and_clean(report)
        except (OSError, ValueError, TypeError, AttributeError):
            passed = False
        if not passed:
            print(
                "Static scan failed: findings, scanner errors, or incomplete report.",
                file=sys.stderr,
            )
            if output.exists():
                print(output.read_text(encoding="utf-8"), file=sys.stderr)
            return 1
    print("Static scan completed without high-severity findings or skipped files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
