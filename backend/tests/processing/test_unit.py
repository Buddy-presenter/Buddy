import io
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from buddy_api.processing import scratch
from buddy_api.processing.__main__ import main
from buddy_api.processing.config import WorkerSettings
from buddy_api.processing.contracts import Message, PDFSource


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_quota", 0),
        ("concurrency", 17),
        ("max_attempts", 6),
        ("runtime_seconds", 1),
        ("queue_message_retention_seconds", 1209601),
        ("queue_redrive_max_receive_count", 1),
        ("text_targeting", "guess_coordinates"),
    ],
)
def test_operator_limits_are_bounded(field, value):
    with pytest.raises(ValidationError):
        WorkerSettings(
            queue_url="https://sqs.eu-central-1.amazonaws.com/123456789012/test",
            region="eu-central-1",
            **{field: value},
        )


def test_queue_retention_is_loaded_from_operator_environment():
    settings = WorkerSettings.from_environment(
        {
            "BUDDY_PROCESSING_QUEUE_URL": "https://sqs.eu-central-1.amazonaws.com/123456789012/test",
            "BUDDY_AWS_REGION": "eu-central-1",
            "BUDDY_WORKER_QUEUE_MESSAGE_RETENTION_SECONDS": "604800",
            "BUDDY_WORKER_RUNTIME_SECONDS": "600",
            "BUDDY_WORKER_QUEUE_REDRIVE_MAX_RECEIVE_COUNT": "5",
            "BUDDY_WORKER_TEXT_TARGETING": "candidate_lines",
        }
    )
    assert settings is not None
    assert settings.queue_message_retention_seconds == 604800
    assert settings.runtime_seconds == 600
    assert settings.text_targeting == "candidate_lines"
    assert settings.queue_redrive_max_receive_count == 5
    assert settings.queue_reconciliation_seconds == 2400


def test_queue_reconciliation_precedes_redrive_and_retention_horizons():
    settings = WorkerSettings(
        queue_url="https://sqs.eu-central-1.amazonaws.com/123456789012/test",
        region="eu-central-1",
    )

    assert settings.queue_reconciliation_seconds == 1800
    assert settings.text_targeting == "quote"
    assert settings.queue_reconciliation_seconds < (
        settings.runtime_seconds * settings.queue_redrive_max_receive_count
    )
    assert settings.queue_reconciliation_seconds < settings.queue_message_retention_seconds


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


def test_resumed_pdf_manifest_timeout_kills_bounded_renderer(monkeypatch, tmp_path):
    import tempfile

    from buddy_api.extraction.runner import extract_pdf
    from buddy_api.processing import stage

    content = (
        Path(__file__).resolve().parents[3] / "tests/fixtures/slides/Buddy_PDF_Geometry.pdf"
    ).read_bytes()
    extraction = extract_pdf(io.BytesIO(content), source_revision="v1")
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
    monkeypatch.setenv("BUDDY_DATABASE_URL", "must-not-reach-renderer")
    with pytest.raises(TimeoutError, match="deadline"):
        stage.execute_manifest(content, extraction, "10000000-0000-0000-0000-000000000001", 0.2)
    assert children and children[0].poll() is not None
    assert list(tmp_path.glob("stage-render-*")) == []
