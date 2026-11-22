"""Release acceptance must not pass missing or mislabelled recording evidence."""

import asyncio
import importlib.util
from pathlib import Path

import pytest

from buddy_api.replay import load_corpus, replay

SPEC = importlib.util.spec_from_file_location(
    "replay_release_check", Path(__file__).resolve().parents[2] / "scripts/check_replay_release.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
check = MODULE.check

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/replay"


def test_release_requires_both_format_directories(tmp_path):
    with pytest.raises(OSError):
        check(tmp_path)


def test_release_rejects_mislabelled_powerpoint_data(tmp_path):
    for source_format in ("pdf", "pptx"):
        folder = tmp_path / source_format
        folder.mkdir()
        (folder / "holdout.json").write_bytes((FIXTURES / "holdout-v3.json").read_bytes())
        (folder / "holdout-gates.json").write_bytes(
            (FIXTURES / "holdout-v3-gates.json").read_bytes()
        )
        corpus, digest = load_corpus(folder / "holdout.json")
        trace = asyncio.run(replay(corpus, digest))
        (folder / "service-policy-trace.json").write_text(trace.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="separately"):
        check(tmp_path)
