"""Isolated Office executor for a dedicated Windows interactive worker."""

import hashlib
import importlib
import json
import sys
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from buddy_desktop.powerpoint import PowerPointAutomation
from buddy_desktop.powerpoint_contract import PowerPointAction
from buddy_desktop.powerpoint_generated import generated_action


def validate(folder: Path) -> dict[str, Any]:
    import psutil  # type: ignore[import-untyped]

    raw = (folder / "request.json").read_bytes()
    if len(raw) > 16384:
        raise ValueError("Request exceeds bound")
    request = json.loads(raw)
    source = folder / "source.pptx"
    content = source.read_bytes()
    result: dict[str, Any] = {
        "schema_version": "buddy.powerpoint-validation-result.v1",
        "source_format": "pptx",
        "state": "needs-review",
        "reasons": ["powerpoint_execution_unavailable"],
        "semantic_verified": False,
        "validator_version": "powerpoint-executor-2",
        "renderer_version": "desktop-powerpoint-2",
        "profile": "windows-powerpoint-2",
        "runtime_platform": sys.platform,
        "office_version": None,
        "source_revision": request["source_revision"],
        "source_sha256": request["source_sha256"],
        "action_sha256": hashlib.sha256(
            json.dumps(
                request["action"], sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest(),
        "before_sha256": None,
        "after_sha256": None,
    }
    if (
        request.get("source_format") != "pptx"
        or request.get("schema_version") != "buddy.powerpoint-validation-request.v1"
        or hashlib.sha256(content).hexdigest() != request["source_sha256"]
    ):
        result["reasons"] = ["powerpoint_source_mismatch"]
        return result
    if PowerPointAutomation._powerpoint_process_running():
        result["reasons"] = ["powerpoint_worker_busy_use_dedicated_session"]
        return result
    pythoncom = importlib.import_module("pythoncom")
    client = importlib.import_module("win32com.client")
    pythoncom.CoInitialize()
    office = presentation = None
    stage = "startup"
    try:
        office_started = time.time()
        office = client.DispatchEx("PowerPoint.Application")
        if office.Presentations.Count:
            office = None
            return result
        office.Visible = True
        office.AutomationSecurity = 3
        office.DisplayAlerts = 1
        stage = "process_identity"
        processes = [
            p
            for p in psutil.process_iter(["name", "create_time"])
            if (p.info["name"] or "").lower() == "powerpnt.exe"
            and p.info["create_time"] >= office_started - 1
        ]
        if len(processes) != 1:
            result["reasons"] = ["powerpoint_process_identity_unavailable"]
            return result
        process = processes[0]
        pid = process.pid
        (folder / "office-process.json").write_text(
            json.dumps({"pid": pid, "created": process.create_time()})
        )
        adapter = PowerPointAutomation(
            # This process owns one initialized COM apartment and private Office
            # instance for its whole lifetime. Per-call adapter teardown must not
            # invalidate the owning COM proxy between execute/preview calls.
            com_loader=lambda: (
                SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=lambda: None),
                SimpleNamespace(Dispatch=lambda _name: office),
            ),
            progid_probe=lambda: None,
            process_probe=lambda: True,
        )
        stage = "launch"
        info = adapter.launch(source)
        stage = "presentation_lookup"
        presentation = office.Presentations.Item(1)
        if request["slide_number"] != info.slide_index:
            info = adapter.execute(
                PowerPointAction("go_to_slide", info, slide_number=request["slide_number"])
            ).info
        stage = "action_contract"
        action = generated_action(
            request["action"],
            info,
            deck_id=str(request["deck_id"]),
            render_id=request["action"]["render_id"],
            slide_id=request["action"]["slide_id"],
            native_slide_id=request["native_slide_id"],
            width=request["slide_width"],
            height=request["slide_height"],
        )
        stage = "before_render"
        before = adapter.preview(info).png
        original_shapes = presentation.Slides.Item(info.slide_index).Shapes.Count
        stage = "execute"
        after_info = adapter.execute(action).info
        stage = "after_render"
        after = adapter.preview(after_info).png
        if (
            presentation.Slides.Item(info.slide_index).Shapes.Count <= original_shapes
            or before == after
        ):
            result["reasons"] = ["powerpoint_no_visible_change"]
            return result
        stage = "erase"
        adapter.execute(PowerPointAction("erase_drawing", after_info))
        if presentation.Slides.Item(info.slide_index).Shapes.Count != original_shapes:
            result["reasons"] = ["powerpoint_cleanup_failed"]
            return result
        (folder / "before.png").write_bytes(before)
        (folder / "after.png").write_bytes(after)
        stage = "runtime_profile"
        result.update(
            state="validated",
            reasons=[],
            office_version=f"{office.Version}.{office.Build}",
            before_sha256=hashlib.sha256(before).hexdigest(),
            after_sha256=hashlib.sha256(after).hexdigest(),
        )
    except Exception as exc:
        result["reasons"] = ["powerpoint_execution_failed", "stage_" + stage, type(exc).__name__]
    finally:
        if presentation is not None:
            with suppress(Exception):
                presentation.Saved = True
                presentation.Close()
        if office is not None:
            with suppress(Exception):
                office.Quit()
        pythoncom.CoUninitialize()
    return result


def main() -> int:
    if sys.platform != "win32" or len(sys.argv) != 2 or sys.stdin.buffer.read(1) != b"1":
        return 2
    folder = Path(sys.argv[1]).resolve()
    result = validate(folder)
    (folder / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
