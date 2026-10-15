"""Exercise the local wrapper with real quality tools in an isolated checkout."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    shutil.copyfile(
        Path(__file__).resolve().parents[2] / "scripts/quality.py", root / "scripts/quality.py"
    )
    (root / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 100\n[tool.mypy]\nstrict = true\n", encoding="utf-8"
    )
    for component, package in [("frontend", "buddy_desktop"), ("backend", "buddy_api")]:
        source = root / component / "src" / package
        source.mkdir(parents=True)
        (source / "__init__.py").write_text('value: str = "valid"\n', encoding="utf-8")
    shared = root / "shared/src/buddy_identity"
    shared.mkdir(parents=True)
    (shared / "__init__.py").write_text('value: str = "valid"\n', encoding="utf-8")
    return root


def run(checkout, *arguments):
    return subprocess.run(
        [sys.executable, str(checkout / "scripts/quality.py"), *arguments],
        cwd=checkout.parent,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("target", ["desktop", "api", "all"])
def test_checks_use_checkout_configuration_from_other_directory(checkout, target):
    assert run(checkout, target, "check").returncode == 0


def test_formatting_is_explicit_and_failed_checks_stop_without_mutation(checkout):
    source = checkout / "backend/src/buddy_api/__init__.py"
    original = 'value:str="valid"\n'
    source.write_text(original, encoding="utf-8")
    failed = run(checkout, "api", "check")
    assert failed.returncode != 0
    assert "Running api type" not in failed.stdout
    assert source.read_text(encoding="utf-8") == original
    assert run(checkout, "api", "format").returncode == 0
    assert run(checkout, "api", "format-check").returncode == 0


@pytest.mark.parametrize("command,source", [("lint", "import os\n"), ("type", "value: str = 1\n")])
def test_tool_failures_propagate(checkout, command, source):
    (checkout / "backend/src/buddy_api/__init__.py").write_text(source, encoding="utf-8")
    assert run(checkout, "api", command).returncode != 0


@pytest.mark.parametrize("arguments", [("unknown", "lint"), ("api", "unknown")])
def test_invalid_arguments_are_rejected(checkout, arguments):
    assert run(checkout, *arguments).returncode == 2


def test_missing_application_is_reported(checkout):
    shutil.rmtree(checkout / "backend/src/buddy_api")
    result = run(checkout, "api", "check")
    assert result.returncode == 2
    assert "Application source is missing" in result.stderr
