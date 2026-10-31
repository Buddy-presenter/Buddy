"""Invoke the real Qt process without importing desktop implementation into the API."""

import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Literal, cast

import psutil  # type: ignore[import-untyped]

from buddy_api.extraction.contracts import ExtractionError
from buddy_api.extraction.runner import extract_pptx, windows_memory_limit
from buddy_api.validation.config import ValidatorSettings
from buddy_api.validation.contracts import ProcessRequest, ProcessResult, action_digest
from buddy_api.validation.powerpoint import PowerPointProcessRequest, PowerPointProcessResult


@dataclass(frozen=True)
class ValidationBundle:
    result: ProcessResult | PowerPointProcessResult
    artifacts: dict[str, bytes]


def run_validation(
    content: bytes,
    request: ProcessRequest | PowerPointProcessRequest,
    settings: ValidatorSettings,
    cancelled: Event | None = None,
) -> ValidationBundle:
    powerpoint = isinstance(request, PowerPointProcessRequest)

    def failed(reason: str) -> ValidationBundle:
        if isinstance(request, PowerPointProcessRequest):
            office_result = PowerPointProcessResult(
                state="needs-review",
                reasons=[reason],
                runtime_platform=cast(Literal["linux", "win32", "darwin"], sys.platform),
                source_revision=request.source_revision,
                source_sha256=request.source_sha256,
                action_sha256=action_digest(request.action),
            )
            return ValidationBundle(
                office_result, {"report": office_result.model_dump_json().encode()}
            )
        result = ProcessResult(
            state="needs-review",
            runtime_platform=cast(Literal["linux", "win32", "darwin"], sys.platform),
            reasons=[reason],
            profile=request.profile,
            source_revision=request.source_revision,
            source_sha256=request.source_sha256,
            action_sha256=action_digest(request.action),
        )
        return ValidationBundle(result, {"report": result.model_dump_json().encode()})

    if not content.startswith(b"PK" if powerpoint else b"%PDF-") or len(content) > 64 * 1024 * 1024:
        return failed("invalid_source")
    if isinstance(request, PowerPointProcessRequest):
        try:
            metadata = extract_pptx(io.BytesIO(content), source_revision=request.source_revision)
            if any(
                w.code in {"external_links_not_followed", "embedded_material_not_executed"}
                for w in metadata.warnings
            ):
                return failed("unsafe_office_content")
            slide = metadata.slides[request.slide_number - 1]
            if (
                metadata.source_sha256 != request.source_sha256
                or slide.source_id != request.native_slide_id
                or slide.id != request.action.get("slide_id")
                or request.action.get("deck_id") != str(request.deck_id)
                or request.action.get("render_id") != "pptx-" + request.source_sha256
                or abs(slide.width / 12700 - request.slide_width) > 0.01
                or abs(slide.height / 12700 - request.slide_height) > 0.01
            ):
                return failed("powerpoint_source_mismatch")
        except (ExtractionError, ValueError, IndexError):
            return failed("invalid_source")
        if sys.platform != "win32":
            return failed("powerpoint_runtime_requires_windows_office")
    with tempfile.TemporaryDirectory(prefix="buddy-validator-") as directory:
        folder = Path(directory)
        (folder / ("source.pptx" if powerpoint else "source.pdf")).write_bytes(content)
        (folder / "request.json").write_text(request.model_dump_json(), encoding="utf-8")
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL"}
        }
        environment.update(
            HOME=directory,
            USERPROFILE=directory,
            TEMP=directory,
            TMP=directory,
            TMPDIR=directory,
            QT_QPA_PLATFORM="offscreen",
            QT_SCALE_FACTOR="2" if request.profile == "wide-2x" else "1",
            PYTHONDONTWRITEBYTECODE="1",
        )
        if powerpoint:
            for name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
                if name in os.environ:
                    environment[name] = os.environ[name]
        started_at = time.time()
        try:
            process = subprocess.Popen(
                [
                    settings.python,
                    "-m",
                    "buddy_desktop.powerpoint_validation_worker"
                    if powerpoint
                    else "buddy_desktop.validation_worker",
                    directory,
                ],
                cwd=directory,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=os.name != "nt",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            return failed("runtime_unavailable")
        reason = None
        try:
            with windows_memory_limit(process.pid, settings.memory_mb * 1024 * 1024):
                monitor = psutil.Process(process.pid)
                assert process.stdin is not None
                process.stdin.write(b"1")
                process.stdin.close()
                deadline = time.monotonic() + (
                    settings.powerpoint_seconds if powerpoint else settings.seconds
                )
                while process.poll() is None:
                    if cancelled is not None and cancelled.is_set():
                        reason = "cancelled"
                        break
                    if time.monotonic() > deadline:
                        reason = "time_limit"
                        break
                    try:
                        rss = 0
                        monitored: set[int] = set()
                        for child in [monitor, *monitor.children(recursive=True)]:
                            with suppress(psutil.NoSuchProcess):
                                if child.is_running():
                                    rss += child.memory_info().rss
                                    monitored.add(child.pid)
                        # COM launches Office outside Python's child tree on some
                        # installations. Its exact owned PID must share the budget.
                        if powerpoint:
                            with suppress(OSError, ValueError, KeyError, psutil.NoSuchProcess):
                                owned = json.loads((folder / "office-process.json").read_text())
                                office = psutil.Process(owned["pid"])
                                if (
                                    office.name().lower() == "powerpnt.exe"
                                    and office.create_time() == owned["created"]
                                    and office.create_time() >= started_at - 1
                                    and office.pid not in monitored
                                ):
                                    rss += office.memory_info().rss
                    except psutil.NoSuchProcess:
                        rss = 0  # A child finished while collecting the process-tree sample.
                    if rss > settings.memory_mb * 1024 * 1024:
                        reason = "memory_limit"
                        break
                    count = scratch_bytes = 0
                    for directory, _subdirs, names in os.walk(folder):
                        for name in names:
                            # The PDF subprocess removes its own temporary files;
                            # disappearance during sampling is normal, not a failure.
                            with suppress(FileNotFoundError):
                                scratch_bytes += (Path(directory) / name).stat().st_size
                                count += 1
                    if count > 32 or scratch_bytes > 256 * 1024 * 1024:
                        reason = "storage_limit"
                        break
                    time.sleep(0.01)
                if reason:
                    return failed(reason)
                if process.returncode:
                    return failed("worker_failed")
        except (OSError, psutil.Error):
            return failed("runtime_unavailable")
        except ExtractionError:
            return failed("isolation_unavailable")
        finally:
            if sys.platform != "win32":
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            elif process.poll() is None:
                process.kill()
            process.wait()
            if powerpoint:
                with suppress(OSError, ValueError, KeyError, psutil.Error):
                    owned = json.loads((folder / "office-process.json").read_text())
                    office = psutil.Process(owned["pid"])
                    if (
                        office.name().lower() == "powerpnt.exe"
                        and office.create_time() == owned["created"]
                        and office.create_time() >= started_at - 1
                    ):
                        office.kill()
                        office.wait(timeout=5)
        try:
            with (folder / "result.json").open("rb") as output:
                payload = output.read(65537)
            if len(payload) > 65536:
                return failed("invalid_worker_result")
            result = (
                PowerPointProcessResult.model_validate_json(payload)
                if powerpoint
                else ProcessResult.model_validate_json(payload)
            )
            if (
                result.runtime_platform != sys.platform
                or result.profile != request.profile
                or result.source_revision != request.source_revision
                or result.source_sha256 != request.source_sha256
                or result.action_sha256 != action_digest(request.action)
            ):
                return failed("stale_worker_result")
            artifacts = {"report": result.model_dump_json().encode()}
            for kind, name in (("before_image", "before.png"), ("after_image", "after.png")):
                path = folder / name
                if path.exists():
                    with path.open("rb") as output:
                        image = output.read(16 * 1024 * 1024 + 1)
                    if len(image) > 16 * 1024 * 1024 or not image.startswith(b"\x89PNG\r\n\x1a\n"):
                        return failed("invalid_worker_evidence")
                    artifacts[kind] = image
                    if (
                        isinstance(result, PowerPointProcessResult)
                        and result.state == "validated"
                        and hashlib.sha256(image).hexdigest()
                        != getattr(
                            result, "before_sha256" if kind == "before_image" else "after_sha256"
                        )
                    ):
                        return failed("stale_worker_evidence")
            if result.state == "validated" and len(artifacts) != 3:
                return failed("missing_worker_evidence")
            return ValidationBundle(result, artifacts)
        except (OSError, ValueError, json.JSONDecodeError):
            return failed("invalid_worker_result")
