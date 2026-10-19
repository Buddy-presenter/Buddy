"""Private S3 download and real PDF inspector under a whole-stage process deadline."""

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
from contextlib import suppress

from buddy_api.analysis.rendering import render_manifest
from buddy_api.extraction.contracts import ExtractionError
from buddy_api.extraction.runner import extract_pdf, windows_memory_limit
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
    # No database URL or identity tokens are inherited by the download/parser stage.
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {
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
    }
    if os.environ.get("BUDDY_ENVIRONMENT") == "test":
        env.update({key: os.environ[key] for key in ("AWS_ENDPOINT_URL_S3",) if key in os.environ})
    process = subprocess.Popen(
        [sys.executable, "-m", "buddy_api.processing.stage"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={
            **env,
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": directory,
            "TMP": directory,
            "TEMP": directory,
        },
        start_new_session=os.name != "nt",
    )
    request = json.dumps(
        {"source": source.model_dump(mode="json"), "storage": settings.model_dump(mode="json")}
    ).encode()
    try:
        # Windows job kills the complete parser tree; Linux uses a private process group.
        with windows_memory_limit(process.pid, 1024 * 1024 * 1024):
            try:
                stdout, _ = process.communicate(request, timeout=seconds)
            except subprocess.TimeoutExpired:
                raise TimeoutError("PDF stage deadline exceeded") from None
            if process.returncode != 0 or len(stdout) > 16 * 1024 * 1024:
                raise ExtractionError("pdf_stage_failed")
            response = json.loads(stdout)
            if response.get("error") == "storage_unavailable":
                raise OSError("Private source unavailable")
            if "error" in response:
                raise ExtractionError("pdf_inspection_failed")
            return MetadataOutput.model_validate(response)
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

            resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
            resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
        request = json.loads(sys.stdin.buffer.read(16385))
        source = PDFSource.model_validate(request["source"])
        storage = SourceStorage(UploadSettings.model_validate(request["storage"]))
        result = inspect_source(source, storage).model_dump_json()
    except UploadError:
        result = '{"error":"storage_unavailable"}'
    except Exception:
        result = '{"error":"pdf_inspection_failed"}'
    finally:
        if storage:
            storage.close()
    sys.stdout.write(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
