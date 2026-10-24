"""Private S3 download and real PDF inspector under a whole-stage process deadline."""

import base64
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import traceback
from contextlib import suppress

from buddy_api.analysis.contracts import Manifest
from buddy_api.analysis.grounding import ground_actions
from buddy_api.analysis.provider import ModelOutput, parse_model_output
from buddy_api.analysis.rendering import render_manifest
from buddy_api.extraction.contracts import Extraction, ExtractionError
from buddy_api.extraction.runner import extract_pdf, parser_memory_limit
from buddy_api.processing.contracts import MetadataOutput, PDFSource
from buddy_api.uploads.config import UploadSettings
from buddy_api.uploads.storage import SourceStorage


def inspect_source(source: PDFSource, storage: SourceStorage) -> MetadataOutput:
    content = storage.read(
        source.key,
        version=source.version,
        size=source.size,
        sha256=source.sha256,
        source_format="pdf",
    )
    return MetadataOutput(
        metadata=(metadata := extract_pdf(io.BytesIO(content), source_revision=source.revision)),
        manifest=render_manifest(content, metadata, str(source.deck_id)),
    )


def execute(source: PDFSource, settings: UploadSettings, seconds: float = 90) -> MetadataOutput:
    # Parent-owned workspace survives a killed child long enough to remove its artifacts.
    with tempfile.TemporaryDirectory(prefix="stage-") as directory:
        return _execute(source, settings, seconds, directory)


def _execute(
    source: PDFSource, settings: UploadSettings, seconds: float, directory: str
) -> MetadataOutput:
    request = json.dumps(
        {
            "operation": "inspect",
            "source": source.model_dump(mode="json"),
            "storage": settings.model_dump(mode="json"),
        }
    ).encode()
    stdout = _run_child(request, seconds, directory, storage_access=True)
    try:
        response = json.loads(stdout)
        if response.get("error") == "storage_unavailable":
            raise OSError("Private source unavailable")
        if "error" in response:
            raise ExtractionError("pdf_inspection_failed")
        return MetadataOutput.model_validate(response)
    except (json.JSONDecodeError, TypeError):
        raise ExtractionError("pdf_stage_failed") from None


def execute_manifest(
    content: bytes, extraction: Extraction, deck_id: str, seconds: float = 90
) -> Manifest:
    """Regenerate manifest from saved metadata in the bounded PDFium child process."""
    with tempfile.TemporaryDirectory(prefix="stage-render-") as directory:
        request = json.dumps(
            {
                "operation": "render_manifest",
                "content": base64.b64encode(content).decode("ascii"),
                "extraction": extraction.model_dump(mode="json"),
                "deck_id": deck_id,
            },
            separators=(",", ":"),
        ).encode()
        stdout = _run_child(request, seconds, directory, storage_access=False)
    try:
        response = json.loads(stdout)
        if "error" in response:
            raise ExtractionError("pdf_render_manifest_failed")
        return Manifest.model_validate(response)
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ExtractionError("pdf_render_manifest_failed") from None


def execute_grounding(
    content: bytes,
    extraction: Extraction,
    manifest: Manifest,
    generated: ModelOutput,
    seconds: float = 120,
) -> ModelOutput:
    """Resolve model targets inside the bounded PDFium/Tesseract child process."""
    with tempfile.TemporaryDirectory(prefix="stage-ground-") as directory:
        request = json.dumps(
            {
                "operation": "ground_actions",
                "content": base64.b64encode(content).decode("ascii"),
                "extraction": extraction.model_dump(mode="json"),
                "manifest": manifest.model_dump(mode="json"),
                "output": generated.model_dump(mode="json"),
            },
            separators=(",", ":"),
        ).encode()
        stdout = _run_child(request, seconds, directory, storage_access=False)
    try:
        response = json.loads(stdout)
        if "error" in response:
            diagnostic = (
                response.get("diagnostic") if os.environ.get("PYTEST_CURRENT_TEST") else None
            )
            suffix = f" ({diagnostic})" if isinstance(diagnostic, str) else ""
            raise ExtractionError(f"pdf_action_grounding_failed{suffix}")
        return parse_model_output(response)
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ExtractionError("pdf_action_grounding_failed") from None


def _run_child(request: bytes, seconds: float, directory: str, *, storage_access: bool) -> bytes:
    # Neither mode inherits the database URL or unrelated identity tokens. Only the
    # source-inspection mode receives the scoped storage credentials it needs to read S3.
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
    }
    if storage_access:
        allowed.update(
            {
                "AWS_PROFILE",
                "AWS_CONFIG_FILE",
                "AWS_SHARED_CREDENTIALS_FILE",
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
                "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
                "AWS_CONTAINER_CREDENTIALS_FULL_URI",
                "AWS_CONTAINER_AUTHORIZATION_TOKEN",
                "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
                "AWS_EC2_METADATA_DISABLED",
            }
        )
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    if storage_access and os.environ.get("BUDDY_ENVIRONMENT") == "test":
        env.update({key: os.environ[key] for key in ("AWS_ENDPOINT_URL_S3",) if key in os.environ})
    child_env = {
        **env,
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": directory,
        "TMP": directory,
        "TEMP": directory,
    }
    if os.environ.get("PYTEST_CURRENT_TEST"):
        child_env["BUDDY_CHILD_DIAGNOSTICS"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-m", "buddy_api.processing.stage"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=child_env,
        start_new_session=os.name != "nt",
    )
    try:
        # Windows job kills the complete parser tree; Linux uses a private process group.
        with parser_memory_limit(process.pid, 1024 * 1024 * 1024):
            try:
                stdout, _ = process.communicate(request, timeout=seconds)
            except subprocess.TimeoutExpired:
                raise TimeoutError("PDF child-process deadline exceeded") from None
            if process.returncode != 0 or len(stdout) > 16 * 1024 * 1024:
                raise ExtractionError("pdf_child_process_failed")
            return stdout
    finally:
        if sys.platform != "win32":
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
        process.communicate()


def main() -> int:
    from buddy_api.uploads.contracts import UploadError

    storage = None
    try:
        if sys.platform != "win32":
            import resource

            _configure_child_resource_limits(resource)
        raw = sys.stdin.buffer.read(40 * 1024 * 1024 + 1)
        if len(raw) > 40 * 1024 * 1024:
            raise ValueError
        request = json.loads(raw)
        if request.get("operation") == "render_manifest":
            content = base64.b64decode(request["content"], validate=True)
            extraction = Extraction.model_validate(request["extraction"])
            result = render_manifest(content, extraction, request["deck_id"]).model_dump_json()
        elif request.get("operation") == "ground_actions":
            content = base64.b64decode(request["content"], validate=True)
            extraction = Extraction.model_validate(request["extraction"])
            manifest = Manifest.model_validate(request["manifest"])
            output = parse_model_output(request["output"])
            result = ground_actions(content, extraction, manifest, output).model_dump_json()
        else:
            source = PDFSource.model_validate(request["source"])
            storage = SourceStorage(UploadSettings.model_validate(request["storage"]))
            result = inspect_source(source, storage).model_dump_json()
    except UploadError:
        result = '{"error":"storage_unavailable"}'
    except Exception as exc:
        result = '{"error":"pdf_inspection_failed"}'
        if os.environ.get("BUDDY_CHILD_DIAGNOSTICS"):
            frame = traceback.extract_tb(exc.__traceback__)[-1]
            result = json.dumps(
                {
                    "error": "pdf_inspection_failed",
                    "diagnostic": f"{type(exc).__name__}: {str(exc)[:120]} at {frame.filename}:{frame.lineno}",
                }
            )
    finally:
        if storage:
            storage.close()
    sys.stdout.write(result)
    return 0


def _configure_child_resource_limits(resource: object) -> None:
    """Apply address-space and CPU limits; macOS memory is monitored by the parent."""
    infinity = resource.RLIM_INFINITY  # type: ignore[attr-defined]
    memory_limit = getattr(resource, "RLIMIT_AS", None)
    if memory_limit is not None:
        _set_child_limit(resource, memory_limit, 1024**3, infinity)
    _set_child_limit(resource, resource.RLIMIT_CPU, 120, infinity)  # type: ignore[attr-defined]


def _set_child_limit(resource: object, kind: int, ceiling: int, infinity: int) -> None:
    _, hard = resource.getrlimit(kind)  # type: ignore[attr-defined]
    bound = ceiling if hard == infinity else min(ceiling, hard)
    resource.setrlimit(kind, (bound, hard))  # type: ignore[attr-defined]


if __name__ == "__main__":
    raise SystemExit(main())
