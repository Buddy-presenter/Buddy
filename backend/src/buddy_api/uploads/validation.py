"""Inspect unchanged sources with existing parser containment and bounded ZIP reads."""

import time
from pathlib import Path
from zipfile import ZipFile

from defusedxml.ElementTree import fromstring  # type: ignore[import-untyped]

from buddy_api.extraction import extract_pptx
from buddy_api.extraction.contracts import ExtractionError, Limits
from buddy_api.uploads.config import UploadSettings
from buddy_api.uploads.contracts import UploadError


def validate_source(path: Path, revision: str, settings: UploadSettings) -> int:
    start = time.monotonic()
    limits = Limits(
        source_bytes=settings.max_bytes,
        expanded_bytes=settings.expanded_bytes,
        slides=settings.max_slides,
    )
    try:
        # Uses the real isolated, memory/deadline-bounded OOXML reader; persist no
        # extracted content and schedule no processing job as part of upload.
        result = extract_pptx(path, source_revision=revision, limits=limits)
        if not result.slides:
            raise ExtractionError("invalid_presentation")
        # Validate CRCs of media too. Only XML parts are read by extraction. The
        # same immutable local file has already passed ZIP count/expanded limits.
        with ZipFile(path) as archive:
            expanded = 0
            for member in archive.infolist():
                name = member.filename.lower()
                if (
                    "vba" in name
                    or "/embeddings/" in name
                    or "/activex/" in name
                    or name.endswith((".exe", ".dll", ".com", ".bat", ".js", ".vbs"))
                ):
                    raise ExtractionError("unsupported_active_content")
                if member.is_dir():
                    continue
                with archive.open(member) as part:
                    while chunk := part.read(64 * 1024):
                        expanded += len(chunk)
                        if expanded > settings.expanded_bytes:
                            raise ExtractionError("archive_limit")
                        if time.monotonic() - start > limits.seconds:
                            raise ExtractionError("time_limit")
                if name == "[content_types].xml" or name.endswith(".rels"):
                    # The parser already verified this XML's bounded size/DTD rules.
                    root = fromstring(archive.read(member), forbid_dtd=True)
                    for element in root.iter():
                        for attribute in ("ContentType", "Type"):
                            value = element.get(attribute, "").lower()
                            if any(
                                word in value
                                for word in ("macroenabled", "vba", "activex", "oleobject")
                            ):
                                raise ExtractionError("unsupported_active_content")
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
