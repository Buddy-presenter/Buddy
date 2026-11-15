"""Established corpus and threshold bytes cannot be altered to hide regressions."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "repository"
    (root / "scripts").mkdir(parents=True)
    shutil.copyfile(
        Path(__file__).resolve().parents[2] / "scripts/check_replay_freeze.py",
        root / "scripts/check_replay_freeze.py",
    )
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    fixture = root / "tests/fixtures/replay/holdout.json"
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(b'{"sealed":true}\n')
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "Seal",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return root, fixture


def run(root, reference="HEAD"):
    return subprocess.run(
        [sys.executable, "scripts/check_replay_freeze.py", "--base-ref", reference],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )


def test_sealed_baseline_and_independent_new_version(checkout):
    root, fixture = checkout
    assert run(root).returncode == 0
    fixture.with_name("holdout-v2.json").write_bytes(b'{"new":true}\n')
    assert run(root).returncode == 0


@pytest.mark.parametrize("change", ["edit", "delete"])
def test_existing_corpus_or_gates_cannot_change(checkout, change):
    root, fixture = checkout
    if change == "edit":
        fixture.write_bytes(b'{"sealed":false}\n')
    else:
        fixture.unlink()
    result = run(root)
    assert result.returncode == 1
    assert "holdout.json" in result.stdout


def test_missing_baseline_fails_closed(checkout):
    root, _ = checkout
    assert run(root, "missing-reference").returncode == 2
