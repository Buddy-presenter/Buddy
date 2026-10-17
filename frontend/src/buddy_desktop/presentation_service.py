"""Authenticated PDF/PPTX uploads and owner-only immutable PDF downloads."""

import hashlib
import http.client
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
from uuid import UUID

from buddy_desktop.config import AppConfig
from buddy_desktop.library_service import LibraryAccessExpired
from buddy_desktop.services import NoRedirects, ServiceError

TYPES = {
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class SavedPresentation:
    deck_id: str


@dataclass(frozen=True)
class DownloadedPDF:
    content: bytes
    deck_id: str
    revision: str


class PresentationService:
    def __init__(self, config: AppConfig, access: str):
        self.config, self.access = config, access

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        content: bytes | None = None,
        content_type: str = "application/json",
        key: UUID | None = None,
        pdf: bool = False,
    ) -> tuple[bytes, Any]:
        headers = {
            "Authorization": "Bearer " + self.access,
            "Content-Type": content_type,
            "Accept": "application/pdf" if pdf else "application/json",
        }
        if key:
            headers["Idempotency-Key"] = str(key)
        request = Request(self.config.api_url + path, data=content, method=method, headers=headers)
        timeout = (
            max(120.0, self.config.request_timeout)
            if pdf or method == "PUT"
            else self.config.request_timeout
        )
        try:
            with build_opener(NoRedirects()).open(request, timeout=timeout) as response:
                limit = MAX_BYTES if pdf else 65536
                payload = response.read(limit + 1)
                expected = "application/pdf" if pdf else "application/json"
                if len(payload) > limit or response.headers.get_content_type() != expected:
                    raise ServiceError("The server returned an invalid presentation response.")
                return payload, response.headers
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status in {401, 403}:
                raise LibraryAccessExpired("Sign in again to access your presentations.") from None
            raise ServiceError(
                {
                    409: "Presentation state changed. Refresh and retry.",
                    413: "Presentation exceeds your file or storage limit.",
                    422: "Presentation is damaged, encrypted, or unsupported.",
                    429: "Upload capacity is full. Try again later.",
                }.get(status, "Presentation request failed. Refresh and retry.")
            ) from None
        except (URLError, OSError, http.client.HTTPException):
            raise ServiceError("Cannot reach the server. Retry with the same file.") from None

    def upload(self, path: Path, key: UUID) -> SavedPresentation:
        source_format = path.suffix.lower().lstrip(".")
        if source_format not in TYPES:
            raise ServiceError("Choose a PDF or PPTX file.")
        try:
            with path.open("rb") as stream:
                content = stream.read(MAX_BYTES + 1)
        except OSError:
            raise ServiceError("Cannot read the selected file.") from None
        if not 1 <= len(content) <= MAX_BYTES:
            raise ServiceError("Choose a presentation up to 64 MiB.")
        metadata = {
            "filename": path.name,
            "title": path.stem[:256],
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "content_type": TYPES[source_format],
        }
        try:
            response, _ = self.request(
                "/api/v1/uploads", method="POST", key=key, content=json.dumps(metadata).encode()
            )
            data = json.loads(response)["data"]
            if data["status"] != "complete":
                identifier = str(UUID(data["id"]))
                response, _ = self.request(
                    f"/api/v1/uploads/{identifier}/content",
                    method="PUT",
                    content=content,
                    content_type=TYPES[source_format],
                )
                data = json.loads(response)["data"]
            if data["status"] != "complete":
                raise ValueError
            return SavedPresentation(str(UUID(data["deck"]["id"])))
        except (ValueError, KeyError, TypeError):
            raise ServiceError("The server returned an invalid upload response.") from None

    def download(self, deck_id: str) -> DownloadedPDF:
        deck_id = str(UUID(deck_id))
        content, headers = self.request(f"/api/v1/decks/{deck_id}/source", pdf=True)
        digest, revision = headers.get("X-Source-Sha256"), headers.get("X-Source-Revision")
        if (
            not isinstance(revision, str)
            or not 1 <= len(revision) <= 128
            or hashlib.sha256(content).hexdigest() != digest
            or not content.startswith(b"%PDF-")
        ):
            raise ServiceError("Saved PDF integrity check failed. Refresh and retry.")
        return DownloadedPDF(content, deck_id, revision)
