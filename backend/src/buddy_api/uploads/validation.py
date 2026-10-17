"""Inspect unchanged sources with existing parser containment and bounded ZIP reads."""

import time
from pathlib import Path
from zipfile import ZipFile

from buddy_api.extraction import extract_pptx
from buddy_api.extraction.contracts import ExtractionError, Limits
from buddy_api.extraction.runner import extract_pdf
from buddy_api.uploads.config import UploadSettings
from buddy_api.uploads.contracts import UploadError


def validate_source(path: Path, revision: str, settings: UploadSettings) -> int:
    start = time.monotonic()
    limits = Limits(
        source_bytes=settings.max_bytes,
        expanded_bytes=settings.expanded_bytes,
        slides=settings.max_slides,
    )
    if path.suffix.lower() == ".pdf":
        try:
            return len(extract_pdf(path, source_revision=revision, limits=limits).slides)
        except ExtractionError:
            raise UploadError(
                422,
                "invalid_pdf",
                "PDF is malformed, encrypted, unsupported, or exceeds parsing limits.",
            ) from None
    try:
        # Uses the real isolated, memory/deadline-bounded OOXML reader; persist no
        # extracted content and schedule no processing job as part of upload.
        result = extract_pptx(path, source_revision=revision, limits=limits)
        if not result.slides:
            raise ExtractionError("invalid_presentation")
        if any(warning.code == "embedded_material_not_executed" for warning in result.warnings):
            raise ExtractionError("unsupported_active_content")
        # Validate CRCs of media too. Only XML parts are read by extraction. The
        # same immutable local file has already passed ZIP count/expanded limits.
        with ZipFile(path) as archive:
            expanded = 0
            for member in archive.infolist():
                if member.is_dir():
                    continue
                with archive.open(member) as part:
                    while chunk := part.read(64 * 1024):
                        expanded += len(chunk)
                        if expanded > settings.expanded_bytes:
                            raise ExtractionError("archive_limit")
                        if time.monotonic() - start > limits.seconds:
                            raise ExtractionError("time_limit")
        return len(result.slides)
    except UploadError:
        raise
    except ExtractionError as exc:
        code = "unsupported_pptx" if exc.code == "unsupported_active_content" else "invalid_pptx"
        raise UploadError(
            422, code, "PPTX is malformed, unsupported, or exceeds parsing limits."
        ) from None
    except Exception:
        raise UploadError(422, "invalid_pptx", "PPTX is corrupt or unsupported.") from None
