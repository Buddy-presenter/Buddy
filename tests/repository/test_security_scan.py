"""A scanner that did not analyze the code must never satisfy the security gate."""

import runpy
from pathlib import Path

import pytest

check = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/security_scan.py"))[
    "complete_and_clean"
]


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
