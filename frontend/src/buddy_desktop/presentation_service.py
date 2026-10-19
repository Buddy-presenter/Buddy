"""Authenticated PDF/PPTX uploads and owner-only immutable PDF downloads."""

import contextlib
import hashlib
import http.client
import io
import json
import socket
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import HTTPHandler, HTTPSHandler, Request, build_opener
from uuid import UUID

from buddy_desktop.config import AppConfig
from buddy_desktop.library_service import LibraryAccessExpired
from buddy_desktop.services import NoRedirects, ServiceError

TYPES = {
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
MAX_BYTES = 64 * 1024 * 1024


class UploadCancelled(ServiceError):
    """Transfer stopped locally; the server may already have saved the deck."""


class UploadRejected(ServiceError):
    """The same bytes cannot be retried successfully."""


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
        self.cancelled = Event()
        self.progress: Callable[[str, int, int], None] = lambda *_args: None
        self._socket: socket.socket | None = None

    def cancel(self) -> None:
        self.cancelled.set()
        connection = self._socket
        if connection is not None:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            connection.close()

    def opener(self, response_timeout: float) -> Any:
        service = self

        class Reader(io.RawIOBase):
            """Poll cancellation without poisoning a timed-out socket.makefile reader."""

            def __init__(self, connection: socket.socket):
                self.connection = connection
                self.deadline = time.monotonic() + response_timeout
                # Retain the descriptor when urllib closes its connection after headers.
                self.lease = connection.makefile("rb", buffering=0)
                connection.settimeout(0.2)

            def readable(self) -> bool:
                return True

            def readinto(self, buffer: Any) -> int:
                while True:
                    service.check_cancelled()
                    if time.monotonic() >= self.deadline:
                        raise TimeoutError
                    try:
                        return self.connection.recv_into(buffer)
                    except TimeoutError:
                        continue

            def close(self) -> None:
                self.lease.close()
                super().close()

        class ReaderSocket:
            def __init__(self, connection: socket.socket):
                self.connection = connection

            def makefile(self, _mode: str) -> io.BufferedReader:
                return io.BufferedReader(Reader(self.connection))

        class Response(http.client.HTTPResponse):
            def __init__(self, connection: Any, *args: Any, **kwargs: Any):
                super().__init__(cast(Any, ReaderSocket(connection)), *args, **kwargs)

        class Connection(http.client.HTTPConnection):
            response_class = Response

            def connect(self) -> None:
                service.check_cancelled()
                super().connect()
                service._socket = self.sock
                service.check_cancelled()

        class SecureConnection(http.client.HTTPSConnection):
            response_class = Response

            def connect(self) -> None:
                service.check_cancelled()
                super().connect()
                service._socket = self.sock
                service.check_cancelled()

        class HTTP(HTTPHandler):
            def http_open(self, req: Any) -> Any:
                return self.do_open(Connection, req)

        class HTTPS(HTTPSHandler):
            def https_open(self, req: Any) -> Any:
                return self.do_open(SecureConnection, req)

        return build_opener(NoRedirects(), HTTP(), HTTPS())

    def check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise UploadCancelled(
                "Upload stopped. It may already be saved. Retry the same file to check safely."
            )

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        content: bytes | Iterator[bytes] | None = None,
        content_type: str = "application/json",
        key: UUID | None = None,
        pdf: bool = False,
        size: int | None = None,
    ) -> tuple[bytes, Any]:
        headers = {
            "Authorization": "Bearer " + self.access,
            "Content-Type": content_type,
            "Accept": "application/pdf" if pdf else "application/json",
        }
        if key:
            headers["Idempotency-Key"] = str(key)
        if size is not None:
            headers["Content-Length"] = str(size)
        request = Request(self.config.api_url + path, data=content, method=method, headers=headers)
        timeout = (
            max(120.0, self.config.request_timeout)
            if pdf or method == "PUT"
            else self.config.request_timeout
        )
        try:
            with self.opener(timeout).open(
                request, timeout=self.config.request_timeout
            ) as response:
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
            error = UploadRejected if status in {413, 415, 422} else ServiceError
            raise error(
                {
                    409: "Presentation state changed. Refresh and retry.",
                    413: "Presentation exceeds your file or storage limit.",
                    415: "Choose a supported PDF or PPTX file.",
                    422: "Presentation is damaged, encrypted, or unsupported.",
                    429: "Upload capacity is full. Try again later.",
                }.get(status, "Presentation request failed. Refresh and retry.")
            ) from None
        except (URLError, OSError, http.client.HTTPException):
            self.check_cancelled()
            raise ServiceError("Cannot reach the server. Retry with the same file.") from None
        finally:
            self._socket = None

    def upload(self, path: Path, key: UUID) -> SavedPresentation:
        self.check_cancelled()
        self.progress("Preparing file", 0, 0)
        source_format = path.suffix.lower().lstrip(".")
        if source_format not in TYPES:
            raise UploadRejected("Choose a PDF or PPTX file.")
        try:
            with path.open("rb") as stream:
                if not 1 <= path.stat().st_size <= MAX_BYTES:
                    raise UploadRejected("Choose a nonempty presentation up to 64 MiB.")
                chunks = []
                total = 0
                while chunk := stream.read(256 * 1024):
                    self.check_cancelled()
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise UploadRejected("Choose a presentation up to 64 MiB.")
                    chunks.append(chunk)
                content = b"".join(chunks)
        except OSError:
            raise ServiceError("Cannot read the selected file.") from None
        if not 1 <= len(content) <= MAX_BYTES:
            raise UploadRejected("Choose a nonempty presentation up to 64 MiB.")
        metadata = {
            "filename": path.name,
            "title": path.stem[:256],
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "content_type": TYPES[source_format],
        }
        try:
            self.check_cancelled()
            self.progress("Checking saved upload", 0, 0)
            response, headers = self.request(
                "/api/v1/uploads", method="POST", key=key, content=json.dumps(metadata).encode()
            )
            data = json.loads(response)["data"]
            identifier = str(UUID(data["id"]))
            deadline = time.monotonic() + 120
            while data["status"] in {"receiving", "cleanup"}:
                self.progress("Checking previous transfer", 0, 0)
                if time.monotonic() >= deadline:
                    raise ServiceError("Upload is still settling. Retry the same file shortly.")
                try:
                    delay = max(1, min(30, int(headers.get("Retry-After", "1"))))
                except (ValueError, TypeError):
                    delay = 1
                if self.cancelled.wait(delay):
                    self.check_cancelled()
                response, headers = self.request(f"/api/v1/uploads/{identifier}")
                data = json.loads(response)["data"]
            if data["status"] == "failed":
                if data.get("retryable") is not True:
                    raise UploadRejected("This upload was rejected. Choose a valid presentation.")
                self.check_cancelled()
                response, _ = self.request(
                    "/api/v1/uploads",
                    method="POST",
                    key=key,
                    content=json.dumps(metadata).encode(),
                )
                data = json.loads(response)["data"]
                if str(UUID(data["id"])) != identifier:
                    raise ValueError
            if data["status"] == "pending":
                self.check_cancelled()

                def transfer() -> Iterator[bytes]:
                    self.progress("Uploading", 0, len(content))
                    for start in range(0, len(content), 64 * 1024):
                        self.check_cancelled()
                        chunk = content[start : start + 64 * 1024]
                        yield chunk
                        self.progress("Uploading", start + len(chunk), len(content))
                    self.progress("Validating and saving", 0, 0)

                response, _ = self.request(
                    f"/api/v1/uploads/{identifier}/content",
                    method="PUT",
                    content=transfer(),
                    content_type=TYPES[source_format],
                    size=len(content),
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
