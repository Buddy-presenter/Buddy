"""Fixed-file desktop operator; provider credentials stay in this trusted host."""

import hashlib
import json
import os
import signal
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
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from buddy_api.extraction.runner import windows_memory_limit
from buddy_api.preflight.config import PreflightSettings
from buddy_api.preflight.judge import VERDICT_SCHEMA, VisualJudge


@dataclass(frozen=True)
class PreflightBundle:
    report: dict[str, Any]
    artifacts: dict[str, bytes]


def run_preflight(
    source: bytes,
    snapshot: dict[str, Any],
    settings: PreflightSettings,
    cancelled: Event,
    progress: Callable[[int], None],
    *,
    judge: Callable[[dict[str, Any], bytes, bytes], object] | None = None,
) -> PreflightBundle:
    def unavailable(reason: str) -> PreflightBundle:
        return PreflightBundle(
            dict(state="unavailable", reason=reason, history=[], semantic_verified=False), {}
        )

    if sys.platform not in {"win32", "darwin"}:
        return unavailable("unsupported_desktop_runtime")
    if snapshot["source_format"] == "pptx" and sys.platform != "win32":
        return unavailable("powerpoint_runtime_requires_windows_office")
    if (
        len(source) > 64 * 1024 * 1024
        or hashlib.sha256(source).hexdigest() != snapshot["source_sha256"]
    ):
        return unavailable("source_mismatch")
    provider = None
    with tempfile.TemporaryDirectory(prefix="buddy-trusted-preflight-") as directory:
        folder = Path(directory)
        (folder / "snapshot.json").write_text(
            json.dumps(snapshot, allow_nan=False), encoding="utf-8"
        )
        (folder / ("source." + snapshot["source_format"])).write_bytes(source)
        environment = {
            k: v
            for k, v in os.environ.items()
            if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL"}
        }
        environment.update(
            HOME=directory,
            USERPROFILE=directory,
            TEMP=directory,
            TMP=directory,
            PYTHONDONTWRITEBYTECODE="1",
        )
        if snapshot["source_format"] == "pptx":
            for name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
                if name in os.environ:
                    environment[name] = os.environ[name]
        process = subprocess.Popen(
            [settings.python, "-m", "buddy_desktop.preflight_operator", directory],
            cwd=directory,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=sys.platform != "win32",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            with windows_memory_limit(process.pid, settings.powerpoint_memory_mb * 1024 * 1024):
                assert process.stdin is not None
                process.stdin.write(b"1")
                process.stdin.close()
                monitor = psutil.Process(process.pid)
                deadline, last_sequence = time.monotonic() + settings.deadline, 0
                while process.poll() is None:
                    if cancelled.is_set() or time.monotonic() > deadline:
                        return unavailable("cancelled" if cancelled.is_set() else "time_limit")
                    memory = 0
                    with suppress(psutil.NoSuchProcess):
                        for child in [monitor, *monitor.children(recursive=True)]:
                            with suppress(psutil.NoSuchProcess):
                                memory += child.memory_info().rss
                    if memory > settings.powerpoint_memory_mb * 1024 * 1024:
                        return unavailable("memory_limit")
                    files = list(folder.rglob("*"))
                    if len(files) > 256:
                        return unavailable("storage_limit")
                    size = 0
                    for file in files:
                        with suppress(FileNotFoundError):
                            if file.is_file():
                                size += file.stat().st_size
                    if size > 256 * 1024 * 1024:
                        return unavailable("storage_limit")
                    request = folder / "judge-request.json"
                    if request.is_file():
                        raw = request.read_bytes()
                        if len(raw) > 16384:
                            return unavailable("invalid_verdict_request")
                        values = json.loads(raw)
                        sequence = values["sequence"]
                        if sequence > last_sequence:
                            if sequence != last_sequence + 1 or sequence > 3:
                                return unavailable("attempt_limit")
                            images = []
                            for role in ("before", "after"):
                                data = (folder / ("judge-" + role + ".png")).read_bytes()
                                if (
                                    len(data) > 3_750_000
                                    or hashlib.sha256(data).hexdigest() != values[role]
                                ):
                                    return unavailable("capture_binding_failed")
                                images.append(data)
                            progress(sequence)
                            if judge is None:
                                provider = provider or VisualJudge(settings.region, settings.model)
                            try:
                                evaluator = judge or provider
                                assert evaluator is not None
                                verdict = evaluator(values["target"], *images)
                                Draft202012Validator(VERDICT_SCHEMA).validate(verdict)
                            except Exception:
                                # Do not log provider errors containing private content.
                                verdict = None
                            reply = folder / "judge-response.tmp"
                            reply.write_text(
                                json.dumps(
                                    dict(sequence=sequence, verdict=verdict), allow_nan=False
                                ),
                                encoding="utf-8",
                            )
                            reply.replace(folder / "judge-response.json")
                            last_sequence = sequence
                    time.sleep(0.05)
                if process.returncode:
                    return unavailable("worker_failed")
        finally:
            if provider is not None:
                provider.close()
            if sys.platform != "win32":
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            elif process.poll() is None:
                process.kill()
            process.wait()
        result = folder / "result"
        raw = (result / "report.json").read_bytes()
        if len(raw) > 256 * 1024:
            return unavailable("report_limit")
        report = json.loads(raw)
        artifacts = {"report": raw}
        for index, entry in enumerate(report["history"]):
            if index > 2 or entry["version"] != index:
                return unavailable("invalid_worker_result")
            attempt = json.loads((result / f"attempt-{index}" / "report.json").read_bytes())
            for role in ("before", "after"):
                meta = attempt.get("evidence", {}).get(role)
                if meta is None:
                    continue
                data = (result / f"attempt-{index}" / (role + ".png")).read_bytes()
                if hashlib.sha256(data).hexdigest() != meta["sha256"] or len(data) > 3_750_000:
                    return unavailable("capture_binding_failed")
                artifacts[f"attempt-{index}-{role}_image"] = data
        if sum(map(len, artifacts.values())) > 32 * 1024 * 1024:
            return unavailable("evidence_limit")
        return PreflightBundle(report, artifacts)
