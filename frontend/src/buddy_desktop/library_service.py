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
class DeckDeletionResult:
    deck_id: UUID


@dataclass(frozen=True)
class LibraryPage:
    items: tuple[DeckSummary, ...]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True)
class DeckDetails:
    id: UUID
    title: str
    created_at: datetime
    source_format: str
    processing: ProcessingDetail
    analysis_status: str
    analysis_version: int | None
    slide_count: int


def parse_processing(value: object) -> ProcessingDetail:
    if not isinstance(value, dict):
        raise ValueError
    state, stage, progress = value["state"], value["stage"], value["progress"]
    attempts, error_code = value["attempts"], value["error_code"]
    retry, cancel = value["can_retry"], value["can_cancel"]
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
        or type(attempts) is not int
        or attempts < 0
        or (error_code is not None and not isinstance(error_code, str))
        or type(retry) is not bool
        or type(cancel) is not bool
    ):
        raise ValueError
    return ProcessingDetail(
        UUID(value["job_id"]) if value["job_id"] is not None else None,
        state,
        stage,
        progress,
        attempts,
        error_code,
        retry,
        cancel,
    )


class LibraryService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def deck_details(self, access: str, deck_id: UUID) -> DeckDetails:
        return self._deck_details(access, deck_id)

    def rename_deck(self, access: str, deck_id: UUID, title: str) -> DeckDetails:
        title = title.strip()
        if not 1 <= len(title) <= 256 or any(ord(c) < 32 or ord(c) == 127 for c in title):
            raise ServiceError(
                "Enter a presentation name of 1 to 256 characters without control characters."
            )
        result = self._deck_details(access, deck_id, title=title)
        if result.title != title:
            raise ServiceError(
                "The server did not confirm the saved presentation name. Refresh details."
            )
        return result

    def _deck_details(self, access: str, deck_id: UUID, *, title: str | None = None) -> DeckDetails:
        if not access or len(access) > 16300:
            raise LibraryAccessExpired("Sign in again to open this presentation.")
        request = Request(
            self.config.api_url + f"/api/v1/decks/{deck_id}",
            data=json.dumps({"title": title}, ensure_ascii=False).encode()
            if title is not None
            else None,
            method="PATCH" if title is not None else "GET",
            headers={
                "Authorization": "Bearer " + access,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Cache-Control": "no-store",
            },
        )
        try:
            with build_opener(NoRedirects()).open(
                request, timeout=self.config.request_timeout
            ) as response:
                content = response.read(16385)
                if (
                    len(content) > 16384
                    or response.headers.get_content_type() != "application/json"
                ):
                    raise ValueError
        except HTTPError as exc:
            code = exc.code
            exc.close()
            if code in {401, 403}:
                raise LibraryAccessExpired("Sign in again to access this presentation.") from None
            if code == 404:
                raise ServiceError(
                    "This presentation is no longer available. Return to Library."
                ) from None
            if code == 422:
                raise ServiceError(
                    "The presentation name was rejected. Check it and try again."
                ) from None
            raise ServiceError(
                "Could not load or save this presentation. Refresh details and try again."
            ) from None
        except (URLError, OSError, http.client.HTTPException):
            raise ServiceError(
                "Cannot reach the library. Check your connection and refresh details."
            ) from None
        except ValueError:
            raise ServiceError("The server returned invalid deck details.") from None
        try:
            data = json.loads(content)["data"]
            if set(data) != {
                "id",
                "title",
                "created_at",
                "source_format",
                "processing",
                "analysis",
            }:
                raise ValueError
            identifier, name = UUID(data["id"]), data["title"]
            created = datetime.fromisoformat(data["created_at"])
            analysis = data["analysis"]
            status, version, slides = (
                analysis["status"],
                analysis["version"],
                analysis["slide_count"],
            )
            if (
                identifier != deck_id
                or not isinstance(name, str)
                or not 1 <= len(name) <= 256
                or created.tzinfo is None
                or data["source_format"] not in {"pdf", "pptx"}
                or status
                not in {"unavailable", "stale", "partial", "awaiting_validation", "published"}
                or (version is not None and (type(version) is not int or version < 1))
                or type(slides) is not int
                or not 0 <= slides <= 500
            ):
                raise ValueError
            return DeckDetails(
                identifier,
                name,
                created,
                data["source_format"],
                parse_processing(data["processing"]),
                status,
                version,
                slides,
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ServiceError("The server returned invalid deck details.") from None

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
                raw_processing = row.get("processing")
                processing = (
                    parse_processing(raw_processing) if raw_processing is not None else None
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

    def delete_deck(self, access: str, deck_id: UUID) -> DeckDeletionResult:
        if not access or len(access) > 16300:
            raise ServiceError("Invalid library request.")
        request = Request(
            self.config.api_url + f"/api/v1/decks/{deck_id}",
            headers={"Authorization": "Bearer " + access, "Accept": "application/json"},
            method="DELETE",
        )
        try:
            with build_opener(NoRedirects()).open(
                request, timeout=self.config.request_timeout
            ) as response:
                content = response.read(65537)
                if (
                    response.status != 202
                    or len(content) > 65536
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
            if status == 404:
                raise ServiceError("This presentation is no longer in your library.") from None
            if status == 429:
                raise ServiceError("Too many requests. Wait a minute, then try again.") from None
            raise ServiceError(
                "Could not delete this presentation. Refresh the library and try again."
            ) from None
        except (URLError, OSError, http.client.HTTPException):
            raise ServiceError(
                "Could not reach the library. Check your connection and try again."
            ) from None
        except ValueError:
            raise ServiceError("The backend returned an invalid deletion response.") from None
        try:
            data = json.loads(content)["data"]
            if UUID(data["id"]) != deck_id or data["status"] != "deleting":
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ServiceError("The backend returned an invalid deletion response.") from None
        return DeckDeletionResult(deck_id)
