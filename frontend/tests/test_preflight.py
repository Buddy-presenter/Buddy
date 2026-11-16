"""Real child-process PDF sessions plus access, bounds and fail-closed contracts."""

import hashlib
import json
import os
import sys
from pathlib import Path
from threading import Event

import pytest

from buddy_desktop.pdf_document import open_pdf
from buddy_desktop.preflight import PreflightRunner, check_tools
from buddy_desktop.preflight_powerpoint import safe_source
from buddy_desktop.slide_contract import ContractError

FIXTURE = Path(__file__).resolve().parents[2] / "tests/fixtures/slides/Buddy_PDF_Geometry.pdf"


@pytest.fixture
def snapshot():
    source = FIXTURE.read_bytes()
    manifest, _ = open_pdf(source, deck_id="test-deck", revision="test-revision")
    action = dict(
        version="1.1",
        instance_id="zoom",
        purpose="Preflight test",
        deck_id="test-deck",
        render_id=manifest.render_id,
        slide_id=manifest.slides[0].slide_id,
        type="zoom_region",
        params={"region": {"x": 96, "y": 410, "width": 480, "height": 240}},
    )
    return dict(
        source=source,
        source_format="pdf",
        deck_id="test-deck",
        revision="test-revision",
        slides={s.slide_id: {} for s in manifest.slides},
        catalog={"zoom": action},
    )


@pytest.mark.parametrize(
    "tools",
    [
        [{"op": "shell", "command": "whoami"}],
        [{"op": "capture", "path": "../other.png"}],
        [{"op": "apply_action", "action_id": "unknown"}],
        [{"op": "load_slide", "slide_id": "unknown"}],
        [{"op": "capture"}] * 5,
        [{"op": "read_state"}] * 17,
        [{"op": []}],
        [{"op": "apply_action", "action_id": []}],
    ],
)
def test_tool_allowlist_rejects_arbitrary_operations(tools):
    with pytest.raises(ContractError):
        check_tools(tools, {}, {})


def test_real_pdf_process_capture_and_deterministic_action_baselines(snapshot):
    with PreflightRunner("owner") as runner:
        root = runner._root
        report = runner.run(
            "owner",
            **snapshot,
            tools=[
                {"op": "capture"},
                {"op": "apply_action", "action_id": "zoom"},
                {"op": "read_state"},
                {"op": "capture"},
                {"op": "apply_action", "action_id": "zoom"},
                {"op": "reset"},
                {"op": "capture"},
            ],
        )
        if sys.platform not in {"win32", "darwin"}:
            assert report["reason"] == "unsupported_desktop_runtime"
            return
        # macOS screen recording may be denied on managed CI hosts; never pass a fake capture.
        if sys.platform == "darwin" and report["reason"] == "desktop_capture_unavailable":
            pytest.skip("Native capture unsupported by this host's screen-recording permission")
        if (
            sys.platform == "darwin"
            and os.environ.get("GITHUB_ACTIONS") == "true"
            and report["reason"] == "audience_viewport_occluded"
        ):
            assert report["state"] == "needs-review" and report["semantic_verified"] is False
            pytest.skip(
                "Managed Mac cannot verify an unobstructed audience; native acceptance pending"
            )
        assert report["state"] == "completed", (
            report.get("reason"),
            report.get("actual_display_scale"),
        )
        assert report["semantic_verified"] is False
        assert report["qt_settings_private"] is True
        assert report["viewport"] == [1280, 720]
        trace = report["trace"]
        assert trace[1]["baseline"] == trace[4]["baseline"]
        assert trace[1]["after"] == trace[4]["after"]
        assert trace[1]["after"] != trace[1]["baseline"]
        images = [runner.artifact("owner", report["run_id"], f"{i}.png") for i in range(3)]
        assert images[0] == images[2] and images[0] != images[1]
        assert all(i.startswith(b"\x89PNG") for i in images)
        assert not (root / report["run_id"] / "source.pdf").exists()
        stored = json.loads(runner.artifact("owner", report["run_id"], "report.json"))
        assert stored["source_sha256"] == hashlib.sha256(snapshot["source"]).hexdigest()
        with pytest.raises(PermissionError):
            runner.artifact("other", report["run_id"], "0.png")
        with pytest.raises(FileNotFoundError):
            runner.artifact("owner", report["run_id"], "../request.json")
        if os.name != "nt":
            assert root.stat().st_mode & 0o077 == 0
    assert not root.exists()
    with pytest.raises(PermissionError):
        runner.run("owner", **snapshot, tools=[{"op": "read_state"}])


def test_cancelled_run_can_retry_without_reusing_partial_state(snapshot):
    cancelled = Event()
    cancelled.set()
    with PreflightRunner("owner") as runner:
        report = runner.run("owner", **snapshot, tools=[{"op": "read_state"}], cancelled=cancelled)
        assert report["state"] == "needs-review" and report["reason"] == "cancelled"
        retry = runner.run("owner", **snapshot, tools=[{"op": "read_state"}])
        assert retry["run_id"] != report["run_id"]
        assert retry["state"] == (
            "completed" if sys.platform in {"win32", "darwin"} else "needs-review"
        )


def test_processed_catalog_larger_than_action_object_limit_loads_in_real_child(snapshot):
    action = snapshot["catalog"]["zoom"]
    snapshot["catalog"] = {str(i): {**action, "instance_id": f"action-{i}"} for i in range(20)}
    with PreflightRunner("owner") as runner:
        report = runner.run("owner", **snapshot, tools=[{"op": "read_state"}])
        assert report["state"] == (
            "completed" if sys.platform in {"win32", "darwin"} else "needs-review"
        ), report
        if sys.platform not in {"win32", "darwin"}:
            assert report["reason"] == "unsupported_desktop_runtime"


def test_load_slide_tool_uses_the_source_pages_coordinate_system(snapshot):
    second_slide = list(snapshot["slides"])[1]
    with PreflightRunner("owner") as runner:
        report = runner.run(
            "owner",
            **snapshot,
            tools=[
                {"op": "load_slide", "slide_id": second_slide},
                {"op": "read_state"},
                {"op": "reset"},
            ],
        )
        if sys.platform not in {"win32", "darwin"}:
            assert report["reason"] == "unsupported_desktop_runtime"
            return
        assert report["state"] == "completed", report
        assert all(step["after"]["slide_id"] == second_slide for step in report["trace"])
        assert report["trace"][0]["after"]["region"] == dict(x=0, y=0, width=1000, height=600)


def test_authorization_and_bad_catalog_fail_before_launch(snapshot, monkeypatch):
    with PreflightRunner("owner") as runner:
        monkeypatch.setattr(runner, "_launch", lambda *args: pytest.fail("must not launch"))
        with pytest.raises(PermissionError):
            runner.run("foreign", **snapshot, tools=[{"op": "read_state"}])
        snapshot["catalog"]["zoom"]["deck_id"] = "foreign"
        with pytest.raises(ContractError):
            runner.run("owner", **snapshot, tools=[{"op": "read_state"}])


def test_worker_failure_is_not_visual_approval_and_source_is_cleaned(snapshot, monkeypatch):
    with PreflightRunner("owner") as runner:
        monkeypatch.setattr(runner, "_launch", lambda *args: "worker_failed")
        report = runner.run("owner", **snapshot, tools=[{"op": "read_state"}])
        assert report["state"] == "needs-review" and report["semantic_verified"] is False
        assert set(p.name for p in (runner._root / report["run_id"]).iterdir()) == {"report.json"}


def test_passive_powerpoint_package_and_external_content_rejection():
    import io
    import zipfile

    safe_source((Path(__file__).parent / "fixtures/slide_actions/wide.pptx").read_bytes())
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            "ppt/_rels/presentation.xml.rels",
            '<Relationships><Relationship TargetMode="External" Target="https://example.com"/></Relationships>',
        )
    with pytest.raises(ContractError, match="unsafe_office_content"):
        safe_source(stream.getvalue())


def test_office_dialog_cannot_be_accepted_as_an_audience_capture():
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QColor, QImage, QPainter

    from buddy_desktop.powerpoint_zoom_evidence import distance
    from buddy_desktop.preflight_powerpoint import office_reference

    source = QImage(1280, 720, QImage.Format.Format_RGB32)
    source.fill(QColor("#101820"))
    clean = office_reference(source, 1280, 720, [1920, 1080], None)
    obstructed = clean.copy()
    painter = QPainter(obstructed)
    painter.fillRect(QRect(290, 130, 700, 450), QColor("#282828"))
    painter.end()
    assert distance(source, clean) < 0.001
    assert distance(obstructed, clean) > 0.015


def test_real_child_crash_stays_unapproved_and_cleans_job(snapshot, monkeypatch):
    import buddy_desktop.preflight as module

    popen = module.subprocess.Popen

    def crash(command, **kwargs):
        return popen(
            [command[0], "-c", "import sys; sys.stdin.buffer.read(1); sys.exit(7)"], **kwargs
        )

    monkeypatch.setattr(module.subprocess, "Popen", crash)
    with PreflightRunner("owner") as runner:
        report = runner.run("owner", **snapshot, tools=[{"op": "read_state"}])
        assert report["state"] == "needs-review"
        assert report["reason"] == "worker_failed"
        assert report["semantic_verified"] is False


@pytest.mark.parametrize("source_format", ["pdf", "pptx"])
def test_native_profile_dependency_does_not_inherit_application_credentials(
    tmp_path, monkeypatch, source_format
):
    import buddy_desktop.preflight as module

    profiles = {
        name: str(tmp_path / "licensed-user" / name.lower())
        for name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA")
    }
    for name, value in profiles.items():
        monkeypatch.setenv(name, value)
    for name in (
        "AWS_SECRET_ACCESS_KEY",
        "BUDDY_API_TOKEN",
        "BUDDY_DATABASE_URL",
        "OPENAI_API_KEY",
    ):
        monkeypatch.setenv(name, "must-not-reach-child")
    captured = {}
    popen = module.subprocess.Popen

    def inspect_environment(command, **kwargs):
        captured.update(kwargs["env"])
        return popen(
            [command[0], "-c", "import sys; sys.stdin.buffer.read(1); sys.exit(7)"], **kwargs
        )

    monkeypatch.setattr(module.subprocess, "Popen", inspect_environment)
    assert PreflightRunner._launch(tmp_path, "wide-1x", None, source_format) == "worker_failed"
    for name in (
        "AWS_SECRET_ACCESS_KEY",
        "BUDDY_API_TOKEN",
        "BUDDY_DATABASE_URL",
        "OPENAI_API_KEY",
    ):
        assert name not in captured
    for name in ("HOME", "TEMP", "TMP", "QML_DISK_CACHE_PATH", "QT_SHADER_CACHE_DIR"):
        assert Path(captured[name]).is_relative_to(tmp_path)
    if sys.platform == "win32" and source_format == "pptx":
        assert all(captured[name] == value for name, value in profiles.items())
    else:
        assert all(captured[name] != value for name, value in profiles.items())


def test_real_hung_child_is_stopped_at_deadline(snapshot, monkeypatch):
    import itertools
    import time
    from types import SimpleNamespace

    import buddy_desktop.preflight as module

    popen = module.subprocess.Popen
    children = []

    def hang(command, **kwargs):
        child = popen(
            [command[0], "-c", "import sys,time; sys.stdin.buffer.read(1); time.sleep(300)"],
            **kwargs,
        )
        children.append(child)
        return child

    ticks = itertools.count(step=100)
    monkeypatch.setattr(
        module, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=time.sleep)
    )
    monkeypatch.setattr(module.subprocess, "Popen", hang)
    with PreflightRunner("owner") as runner:
        report = runner.run("owner", **snapshot, tools=[{"op": "read_state"}])
        assert report["reason"] == "time_limit"
        assert report["state"] == "needs-review"
        assert children[0].poll() is not None


def test_retention_expires_runs_and_caps_private_evidence(snapshot, monkeypatch):
    with PreflightRunner("owner") as runner:
        monkeypatch.setattr(runner, "_launch", lambda *args: "worker_failed")
        first = runner.run("owner", **snapshot, tools=[{"op": "read_state"}])
        for _ in range(8):
            runner.run("owner", **snapshot, tools=[{"op": "read_state"}])
        assert len(runner._runs) == 8
        with pytest.raises(FileNotFoundError):
            runner.artifact("owner", first["run_id"], "report.json")
        latest = next(reversed(runner._runs))
        runner._runs[latest] -= 3601
        with pytest.raises(FileNotFoundError):
            runner.artifact("owner", latest, "report.json")
