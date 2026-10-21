"""Bounded, typed library transport. No redirects, credentials on disk, or raw errors."""

import http.client
import json
from dataclasses import dataclass
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener
from uuid import UUID

from buddy_desktop.config import AppConfig
from buddy_desktop.services import NoRedirects, ServiceError

STATUSES = {"uploaded", "queued", "processing", "failed", "ready", "incomplete"}
SORTS = {"newest", "oldest", "title_asc", "title_desc"}


class LibraryAccessExpired(ServiceError):
    """Clear private results and ask the account controls to reauthenticate."""


@dataclass(frozen=True)
class DeckSummary:
    id: UUID
    title: str
    created_at: datetime
    status: str
    attempts: int
    source_format: str = "pptx"
    can_present: bool = False
    processing: "ProcessingDetail | None" = None


@dataclass(frozen=True)
class ProcessingDetail:
    job_id: UUID | None
    state: str
    stage: str | None
    progress: int
    attempts: int
    error_code: str | None
    can_retry: bool
    can_cancel: bool


@dataclass(frozen=True)
class ProcessingResult:
    job_id: UUID
    status: str
    state: str


@dataclass(frozen=True)
class LibraryPage:
    items: tuple[DeckSummary, ...]
    total: int
    page: int
    page_size: int


class LibraryService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def list(self, access: str, *, search: str, sort: str, page: int) -> LibraryPage:
        if not access or len(access) > 16300 or sort not in SORTS or not 1 <= page <= 100000:
            raise ServiceError("Invalid library request.")
        query = urlencode({"search": search, "sort": sort, "page": page, "page_size": 20})
        request = Request(
            self.config.api_url + "/api/v1/decks?" + query,
            headers={"Authorization": "Bearer " + access, "Accept": "application/json"},
        )
        try:
            with build_opener(NoRedirects()).open(
                request, timeout=self.config.request_timeout
            ) as response:
                content = response.read(65537)
                if (
                    len(content) > 65536
                    or response.headers.get_content_type() != "application/json"
                ):
                    raise ValueError
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status in {401, 403}:
                raise LibraryAccessExpired(
                    "Library access expired. Sign in or resume your session."
                ) from None
            if status == 429:
                raise ServiceError("Too many requests. Wait a minute, then refresh.") from None
            raise ServiceError("Library unavailable. Check the backend and try again.") from None
        except (URLError, OSError, http.client.HTTPException):
            raise ServiceError(
                "Cannot reach the library. Check your connection and try again."
            ) from None
        except ValueError:
            raise ServiceError("The backend returned an invalid library response.") from None
        try:
            payload = json.loads(content)
            data = payload["data"]
            total, actual_page, size = data["total"], data["page"], data["page_size"]
            rows = data["items"]
            if (
                type(total) is not int
                or total < 0
                or type(actual_page) is not int
                or actual_page != page
                or type(size) is not int
                or size != 20
                or not isinstance(rows, list)
                or len(rows) > size
                or len(rows) != min(size, max(0, total - (page - 1) * size))
            ):
                raise ValueError
            items = []
            for row in rows:
                title, status, attempts = row["title"], row["status"], row["attempts"]
                created = datetime.fromisoformat(row["created_at"])
                if (
                    not isinstance(title, str)
                    or not 1 <= len(title) <= 256
                    or status not in STATUSES
                    or type(attempts) is not int
                    or attempts < 0
                    or created.tzinfo is None
                ):
                    raise ValueError
                source_format, can_present = (
                    row.get("source_format", "pptx"),
                    row.get("can_present", False),
                )
                if source_format not in {"pdf", "pptx"} or type(can_present) is not bool:
                    raise ValueError
                if can_present and source_format != "pdf":
                    raise ValueError
                processing = None
                raw_processing = row.get("processing")
                if raw_processing is not None:
                    state = raw_processing["state"]
                    stage = raw_processing["stage"]
                    progress = raw_processing["progress"]
                    processing_attempts = raw_processing["attempts"]
                    error_code = raw_processing["error_code"]
                    can_retry, can_cancel = (
                        raw_processing["can_retry"],
                        raw_processing["can_cancel"],
                    )
                    if (
                        state
                        not in {
                            "uploaded",
                            "queued",
                            "inspecting_pdf_metadata",
                            "analyzing",
                            "validating",
                            "review_ready",
                            "partial",
                            "failed",
                            "cancelled",
                        }
                        or (stage is not None and not isinstance(stage, str))
                        or type(progress) is not int
                        or not 0 <= progress <= 100
                        or type(processing_attempts) is not int
                        or processing_attempts < 0
                        or (error_code is not None and not isinstance(error_code, str))
                        or type(can_retry) is not bool
                        or type(can_cancel) is not bool
                    ):
                        raise ValueError
                    raw_job_id = raw_processing["job_id"]
                    processing = ProcessingDetail(
                        UUID(raw_job_id) if raw_job_id is not None else None,
                        state,
                        stage,
                        progress,
                        processing_attempts,
                        error_code,
                        can_retry,
                        can_cancel,
                    )
                items.append(
                    DeckSummary(
                        UUID(row["id"]),
                        title,
                        created,
                        status,
                        attempts,
                        source_format,
                        can_present,
                        processing,
                    )
                )
            if len({item.id for item in items}) != len(items):
                raise ValueError
            return LibraryPage(tuple(items), total, actual_page, size)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ServiceError("The backend returned an invalid library response.") from None

    def operate_job(self, access: str, job_id: UUID, operation: str) -> ProcessingResult:
        if not access or len(access) > 16300 or operation not in {"cancel", "retry"}:
            raise ServiceError("Invalid processing request.")
        request = Request(
            self.config.api_url + f"/api/v1/jobs/{job_id}/{operation}",
            data=b"{}",
            headers={
                "Authorization": "Bearer " + access,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with build_opener(NoRedirects()).open(
                request, timeout=self.config.request_timeout
            ) as response:
                content = response.read(65537)
                if (
                    len(content) > 65536
                    or response.headers.get_content_type() != "application/json"
                ):
                    raise ValueError
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status in {401, 403}:
                raise LibraryAccessExpired(
                    "Library access expired. Sign in or resume your session."
                ) from None
            if status == 409:
                raise ServiceError("This processing job can no longer be changed.") from None
            if status == 429:
                raise ServiceError("Processing capacity is full. Try again shortly.") from None
            raise ServiceError(
                "Could not update this processing job. Refresh and try again."
            ) from None
        except (URLError, OSError, http.client.HTTPException):
            raise ServiceError(
                "Could not reach the processing service. Check and try again."
            ) from None
        except ValueError:
            raise ServiceError("The backend returned an invalid processing response.") from None
        try:
            data = json.loads(content)["data"]
            result = ProcessingResult(UUID(data["id"]), data["status"], data["stage"])
            if (
                result.job_id != job_id
                or not isinstance(result.status, str)
                or not isinstance(result.state, str)
            ):
                raise ValueError
            return result
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ServiceError("The backend returned an invalid processing response.") from None
