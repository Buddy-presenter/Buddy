"""Invoke the real Qt process without importing desktop implementation into the API."""

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
from buddy_api.extraction.runner import windows_memory_limit
from buddy_api.validation.config import ValidatorSettings
from buddy_api.validation.contracts import ProcessRequest, ProcessResult, action_digest


@dataclass(frozen=True)
class ValidationBundle:
    result: ProcessResult
    artifacts: dict[str, bytes]


def run_validation(
    content: bytes,
    request: ProcessRequest,
    settings: ValidatorSettings,
    cancelled: Event | None = None,
) -> ValidationBundle:
    def failed(reason: str) -> ValidationBundle:
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

    if not content.startswith(b"%PDF-") or len(content) > 64 * 1024 * 1024:
        return failed("invalid_source")
    with tempfile.TemporaryDirectory(prefix="buddy-validator-") as directory:
        folder = Path(directory)
        (folder / "source.pdf").write_bytes(content)
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
        try:
            process = subprocess.Popen(
                [settings.python, "-m", "buddy_desktop.validation_worker", directory],
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
                deadline = time.monotonic() + settings.seconds
                while process.poll() is None:
                    if cancelled is not None and cancelled.is_set():
                        reason = "cancelled"
                        break
                    if time.monotonic() > deadline:
                        reason = "time_limit"
                        break
                    try:
                        rss = sum(
                            child.memory_info().rss
                            for child in [monitor, *monitor.children(recursive=True)]
                            if child.is_running()
                        )
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
        try:
            with (folder / "result.json").open("rb") as output:
                payload = output.read(65537)
            if len(payload) > 65536:
                return failed("invalid_worker_result")
            result = ProcessResult.model_validate_json(payload)
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
            if result.state == "validated" and len(artifacts) != 3:
                return failed("missing_worker_evidence")
            return ValidationBundle(result, artifacts)
        except (OSError, ValueError, json.JSONDecodeError):
            return failed("invalid_worker_result")
