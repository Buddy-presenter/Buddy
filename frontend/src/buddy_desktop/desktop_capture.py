"""Capture a PowerPoint action check on the owner's PC; the server judges every attempt.

The isolated operator child (``buddy_desktop.preflight_operator``) opens the saved
source in a dedicated desktop PowerPoint, applies the action and screenshots the
real audience window. For each attempt it asks this host for a verdict through
fixed files; the host forwards the screenshots to the Buddy API, which calls the
vision model and records the verdict. The desktop never decides an outcome and
holds no provider credentials.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any

import psutil  # type: ignore[import-untyped]

MAX_PNG = 3_750_000
DEADLINE_SECONDS = 450
MEMORY_LIMIT = 1536 * 1024 * 1024

# Called with (sequence, target, before_png, after_png); returns the server verdict.
JudgeCall = Callable[[int, dict[str, Any], bytes, bytes], object]


@dataclass(frozen=True)
class CaptureResult:
    report: dict[str, Any] | None
    artifacts: dict[str, bytes]
    # Set when no complete report exists; one of the server's known failure reasons.
    failure: str | None = None


def child_command(module: str, flag: str) -> list[str]:
    """Run a Buddy helper module from source or from the packaged executable."""
    if getattr(sys, "frozen", False):
        return [sys.executable, flag]
    executable = Path(sys.executable)
    # Prefer the console-less interpreter so no terminal window flashes on Windows.
    windowless = executable.with_name("pythonw.exe")
    command = str(windowless if sys.platform == "win32" and windowless.is_file() else executable)
    return [command, "-m", module]


def _environment(folder: Path) -> dict[str, str]:
    keep = {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "PYTHONPATH"}
    environment = {k: v for k, v in os.environ.items() if k.upper() in keep}
    environment.update(
        HOME=str(folder),
        TEMP=str(folder),
        TMP=str(folder),
        PYTHONDONTWRITEBYTECODE="1",
    )
    # Desktop Office runs as the signed-in Windows user; it needs that real
    # profile for activation. Job data and Qt settings stay in the private folder.
    for name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        if name in os.environ:
            environment[name] = os.environ[name]
    return environment


def capture_check(
    snapshot: dict[str, Any],
    source: bytes,
    judge: JudgeCall,
    cancelled: Event,
    progress: Callable[[int], None] | None = None,
) -> CaptureResult:
    if sys.platform != "win32" or snapshot.get("source_format") != "pptx":
        return CaptureResult(None, {}, "powerpoint_requires_windows_office")
    if len(source) > 64 * 1024 * 1024 or hashlib.sha256(source).hexdigest() != snapshot.get(
        "source_sha256"
    ):
        return CaptureResult(None, {}, "source_mismatch")
    with tempfile.TemporaryDirectory(prefix="buddy-desktop-check-") as directory:
        folder = Path(directory)
        (folder / "snapshot.json").write_text(json.dumps(snapshot, allow_nan=False), "utf-8")
        (folder / "source.pptx").write_bytes(source)
        process = subprocess.Popen(
            [*child_command("buddy_desktop.preflight_operator", "--preflight-operator"), directory],
            cwd=directory,
            env=_environment(folder),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            assert process.stdin is not None
            process.stdin.write(b"1")
            process.stdin.close()
            monitor = psutil.Process(process.pid)
            deadline, last_sequence = time.monotonic() + DEADLINE_SECONDS, 0
            while process.poll() is None:
                if cancelled.is_set():
                    return CaptureResult(None, {}, "cancelled")
                if time.monotonic() > deadline:
                    return CaptureResult(None, {}, "time_limit")
                memory = 0
                with suppress(psutil.NoSuchProcess):
                    for child in [monitor, *monitor.children(recursive=True)]:
                        with suppress(psutil.NoSuchProcess):
                            memory += child.memory_info().rss
                if memory > MEMORY_LIMIT:
                    return CaptureResult(None, {}, "worker_failed")
                request = folder / "judge-request.json"
                if request.is_file():
                    raw = request.read_bytes()
                    if len(raw) > 16384:
                        return CaptureResult(None, {}, "worker_failed")
                    values = json.loads(raw)
                    sequence = values["sequence"]
                    if sequence > last_sequence:
                        if sequence != last_sequence + 1 or sequence > 3:
                            return CaptureResult(None, {}, "worker_failed")
                        images = []
                        for role in ("before", "after"):
                            data = (folder / ("judge-" + role + ".png")).read_bytes()
                            if (
                                len(data) > MAX_PNG
                                or hashlib.sha256(data).hexdigest() != values[role]
                            ):
                                return CaptureResult(None, {}, "desktop_capture_unavailable")
                            images.append(data)
                        if progress:
                            progress(sequence)
                        verdict = judge(sequence, values["target"], images[0], images[1])
                        reply = folder / "judge-response.tmp"
                        reply.write_text(
                            json.dumps(dict(sequence=sequence, verdict=verdict), allow_nan=False),
                            encoding="utf-8",
                        )
                        reply.replace(folder / "judge-response.json")
                        last_sequence = sequence
                time.sleep(0.05)
            if process.returncode:
                return CaptureResult(None, {}, _child_failure(folder))
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
        return _collect(folder)


def _child_failure(folder: Path) -> str:
    """Map the operator's own bounded failure reason, if it recorded one."""
    with suppress(OSError, ValueError, KeyError, TypeError):
        for report in sorted((folder / "result").rglob("report.json")):
            reason = json.loads(report.read_bytes()).get("reason")
            if isinstance(reason, str) and reason:
                return reason
    return "worker_failed"


def _collect(folder: Path) -> CaptureResult:
    result = folder / "result"
    raw = (result / "report.json").read_bytes()
    if len(raw) > 256 * 1024:
        return CaptureResult(None, {}, "worker_failed")
    report = json.loads(raw)
    artifacts: dict[str, bytes] = {}
    for index, entry in enumerate(report["history"]):
        if index > 2 or entry["version"] != index:
            return CaptureResult(None, {}, "worker_failed")
        attempt = json.loads((result / f"attempt-{index}" / "report.json").read_bytes())
        for role in ("before", "after"):
            meta = attempt.get("evidence", {}).get(role)
            if meta is None:
                continue
            data = (result / f"attempt-{index}" / (role + ".png")).read_bytes()
            if hashlib.sha256(data).hexdigest() != meta["sha256"] or len(data) > MAX_PNG:
                return CaptureResult(None, {}, "desktop_capture_unavailable")
            artifacts[f"attempt-{index}-{role}_image"] = data
    return CaptureResult(report, artifacts)
