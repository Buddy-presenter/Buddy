import subprocess
import sys
import time
from uuid import UUID

import pytest
from pydantic import ValidationError

from buddy_api.processing import scratch
from buddy_api.processing.__main__ import main
from buddy_api.processing.config import WorkerSettings
from buddy_api.processing.contracts import Message, PDFSource


@pytest.mark.parametrize(
    "field,value",
    [("owner_quota", 0), ("concurrency", 17), ("max_attempts", 6), ("runtime_seconds", 1)],
)
def test_operator_limits_are_bounded(field, value):
    with pytest.raises(ValidationError):
        WorkerSettings(
            queue_url="https://sqs.eu-central-1.amazonaws.com/123456789012/test",
            region="eu-central-1",
            **{field: value},
        )


def test_envelope_has_no_untrusted_source_or_stage():
    with pytest.raises(ValidationError):
        Message(
            job_id="10000000-0000-0000-0000-000000000001", generation=0, source_key="other-owner"
        )
    with pytest.raises(ValidationError):
        PDFSource(
            key="source",
            version="version",
            size=2**30,
            sha256="a" * 64,
            revision="v1",
            deck_id=UUID("10000000-0000-0000-0000-000000000001"),
        )


def test_health_requires_fresh_real_runtime_tick(monkeypatch, tmp_path):
    from buddy_api.processing import __main__ as worker

    health = tmp_path / "health"
    monkeypatch.setattr(worker, "HEALTH", health)
    monkeypatch.setattr(sys, "argv", ["worker", "--health"])
    assert main() == 1
    health.write_text(str(time.time()), encoding="ascii")
    assert main() == 0
    health.write_text(str(time.time() - 50), encoding="ascii")
    assert main() == 1
    health.write_text("partial-write", encoding="ascii")
    assert main() == 1


def test_abandoned_scratch_removed_only_under_exclusive_lock(monkeypatch, tmp_path):
    root = tmp_path / "buddy-processing"
    root.mkdir()
    stale = root / "work-abandoned"
    stale.mkdir()
    (stale / "private-source").write_bytes(b"synthetic")
    untouched = root / "unrelated"
    untouched.mkdir()
    monkeypatch.setattr(scratch, "root", lambda: root)
    with scratch.workspace() as current:
        assert not stale.exists() and untouched.exists() and current.is_dir()
        with pytest.raises(OSError), scratch.workspace():
            pytest.fail("A second worker must not sweep a live workspace")
        assert current.is_dir()
    assert not current.exists() and untouched.exists()


def test_stage_rejects_bad_internal_request_without_echoing_input():
    result = subprocess.run(
        [sys.executable, "-m", "buddy_api.processing.stage"],
        input=b'{"private":"not-source"}',
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0
    assert result.stdout == b'{"error":"pdf_inspection_failed"}'
    assert b"private" not in result.stderr


def test_whole_stage_timeout_kills_process_and_removes_its_scratch(monkeypatch, tmp_path):
    import tempfile

    from buddy_api.processing import stage
    from buddy_api.uploads.config import UploadSettings

    original = subprocess.Popen
    children = []

    def stalled(_command, **kwargs):
        assert "BUDDY_DATABASE_URL" not in kwargs["env"]
        child = original(
            [sys.executable, "-c", "import sys,time; sys.stdin.buffer.read(); time.sleep(30)"],
            **kwargs,
        )
        children.append(child)
        return child

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(stage.subprocess, "Popen", stalled)
    monkeypatch.setenv("BUDDY_DATABASE_URL", "must-not-reach-parser")
    source = PDFSource(
        key="source",
        version="version",
        size=1,
        sha256="a" * 64,
        revision="v1",
        deck_id=UUID("10000000-0000-0000-0000-000000000001"),
    )
    storage = UploadSettings(
        bucket="buddy-test",
        region="eu-central-1",
        kms_key="arn:aws:kms:eu-central-1:123456789012:key/00000000-0000-0000-0000-000000000000",
    )
    with pytest.raises(TimeoutError):
        stage.execute(source, storage, seconds=0.2)
    assert children and children[0].poll() is not None
    assert list(tmp_path.glob("stage-*")) == []
