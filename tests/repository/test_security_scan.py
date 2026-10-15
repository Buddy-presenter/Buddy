"""A scanner that did not analyze the code must never satisfy the security gate."""

import runpy
from pathlib import Path

import pytest

scanner = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/security_scan.py"))
check = scanner["complete_and_clean"]
scan_targets = scanner["scan_targets"]


def test_scan_targets_cover_all_relocated_maintained_code():
    root = Path(__file__).resolve().parents[2]
    targets = scan_targets(root)
    covered_python_files = {path.resolve() for target in targets for path in target.rglob("*.py")}

    assert {
        (root / "frontend/src").resolve(),
        (root / "backend/src").resolve(),
        (root / "shared/src").resolve(),
        (root / "scripts").resolve(),
        (root / "frontend/scripts").resolve(),
        (root / "backend/scripts").resolve(),
        (root / "backend/migrations").resolve(),
    } == {target.resolve() for target in targets}
    assert {
        root / "backend/scripts/release/deploy.py",
        root / "frontend/scripts/release/build_desktop.py",
        root / "backend/migrations/env.py",
    } <= covered_python_files


def test_clean_complete_scan():
    assert check({"errors": [], "results": [], "metrics": {"_totals": {"loc": 100}}})


@pytest.mark.parametrize(
    "report",
    [
        {},
        None,
        {"errors": [], "results": [], "metrics": {"_totals": {"loc": 0}}},
        {"errors": ["parse failure"], "results": [], "metrics": {"_totals": {"loc": 100}}},
        {
            "errors": [],
            "results": [{"issue_severity": "HIGH"}],
            "metrics": {"_totals": {"loc": 100}},
        },
    ],
)
def test_incomplete_or_unsafe_scan(report):
    assert not check(report)
