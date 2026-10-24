"""Owner-authorized, bounded reads for the saved presentation review workspace."""

import hashlib
import http.client
import json
import math
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
from uuid import UUID

from buddy_desktop.config import AppConfig
from buddy_desktop.library_service import LibraryAccessExpired
from buddy_desktop.presentation_service import DownloadedPDF, PresentationService
from buddy_desktop.services import NoRedirects, ServiceError

MAX_ANALYSIS_BYTES = 8 * 1024 * 1024 + 65536
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class ReviewSummary:
    status: str
    text: str | None
    reason: str | None


@dataclass(frozen=True)
class ReviewEvidence:
    id: UUID
    kind: str
    sha256: str


@dataclass(frozen=True)
class ReviewAction:
    instance_id: str
    purpose: str
    action_type: str
    version: str
    region: tuple[float, float, float, float]
    state: str
    usable: bool
    evidence: tuple[ReviewEvidence, ...]
    reasons: tuple[str, ...]
    profile: str
    validator_version: str
    renderer_version: str
    validation_id: UUID | None


@dataclass(frozen=True)
class ReviewSlide:
    slide_id: str
    width: int
    height: int
    summary: ReviewSummary
    actions: tuple[ReviewAction, ...]


@dataclass(frozen=True)
class ReviewAnalysis:
    id: UUID
    deck_id: UUID
    version: int
    publication: str
    action_limit_reached: bool
    source_revision: str | None
    source_sha256: str | None
    presentation_summary: ReviewSummary
    slides: tuple[ReviewSlide, ...]


def _dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError
    return value


def _text(value: object, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 16000:
        raise ValueError
    return value


def _required_text(value: object) -> str:
    result = _text(value)
    assert result is not None
    return result


def _summary(value: object) -> ReviewSummary:
    row = _dict(value)
    status = row.get("status")
    if status not in {"pending", "available", "unavailable"}:
        raise ValueError
    text = _text(row.get("text"), optional=True)
    reason = _text(row.get("reason"), optional=True)
    if status == "available" and (text is None or reason is not None):
        raise ValueError
    if status != "available" and text is not None:
        raise ValueError
    if status == "unavailable" and reason is None:
        raise ValueError
    return ReviewSummary(status, text, reason)


def parse_analysis(payload: object, expected_deck: UUID) -> ReviewAnalysis:
    data = _dict(_dict(payload).get("data"))
    identifier, deck_id = UUID(str(data["id"])), UUID(str(data["deck_id"]))
    if deck_id != expected_deck:
        raise ValueError
    action_limit_reached = data.get("action_limit_reached", False)
    if type(action_limit_reached) is not bool:
        raise ValueError
    inputs = data.get("inputs")
    summary = ReviewSummary("unavailable", None, "This analysis predates the review format.")
    source_revision = source_sha256 = None
    widths: dict[str, tuple[int, int]] = {}
    if inputs is not None:
        input_data = _dict(inputs)
        source_revision = _required_text(input_data.get("source_revision"))
        source_sha256 = _required_text(input_data.get("source_sha256"))
        if len(source_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in source_sha256
        ):
            raise ValueError
        manifest = _dict(input_data["manifest"])
        if str(manifest["deck_id"]) != str(deck_id):
            raise ValueError
        rendered = manifest["slides"]
        if not isinstance(rendered, list) or not 1 <= len(rendered) <= 500:
            raise ValueError
        for slide in rendered:
            slide = _dict(slide)
            source = _dict(slide["source"])
            width, height = source["width"], source["height"]
            current_slide_id = _required_text(slide.get("slide_id"))
            if (
                type(width) is not int
                or type(height) is not int
                or min(width, height) <= 0
                or max(width, height) > 16384
                or current_slide_id in widths
            ):
                raise ValueError
            widths[current_slide_id] = (width, height)
        summary = _summary(input_data["presentation_summary"])
    raw_slides = data.get("slides")
    if not isinstance(raw_slides, list) or len(raw_slides) > 500:
        raise ValueError
    slides: list[ReviewSlide] = []
    for raw in raw_slides:
        row = _dict(raw)
        slide_id = _required_text(row.get("slide_id"))
        if slide_id not in widths:
            raise ValueError
        raw_actions = row.get("actions")
        if not isinstance(raw_actions, list) or len(raw_actions) > 100:
            raise ValueError
        actions: list[ReviewAction] = []
        for item in raw_actions:
            result = _dict(item)
            action = _dict(result.get("action"))
            params = _dict(action.get("params"))
            region = _dict(params.get("region"))
            rect: list[float] = []
            for key in ("x", "y", "width", "height"):
                value = region.get(key)
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise ValueError
                rect.append(float(value))
            x, y, width, height = rect
            if not all(math.isfinite(value) for value in (x, y, width, height)):
                raise ValueError
            page_width, page_height = widths[slide_id]
            if (
                min(x, y) < 0
                or min(width, height) <= 0
                or x + width > page_width
                or y + height > page_height
            ):
                raise ValueError
            validation = result.get("validation")
            evidence: list[ReviewEvidence] = []
            reasons: tuple[str, ...] = ()
            validation_id = None
            profile = _required_text(_dict(inputs).get("display_profile")) if inputs else "unknown"
            validator = (
                _required_text(_dict(inputs).get("validator_version")) if inputs else "unknown"
            )
            renderer = (
                _required_text(_dict(inputs).get("renderer_version")) if inputs else "unknown"
            )
            if validation is not None:
                validation = _dict(validation)
                validation_id = UUID(str(validation["validation_id"]))
                if (
                    validation.get("analysis_id") != str(identifier)
                    or validation.get("analysis_sha256") != data.get("analysis_sha256")
                    or validation.get("instance_id") != action.get("instance_id")
                    or validation.get("action_sha256")
                    != hashlib.sha256(
                        json.dumps(
                            action, sort_keys=True, separators=(",", ":"), allow_nan=False
                        ).encode()
                    ).hexdigest()
                    or validation.get("display_profile") != profile
                    or validation.get("validator_version") != validator
                    or validation.get("renderer_version") != renderer
                ):
                    raise ValueError
                evidence_rows = validation.get("evidence")
                raw_reasons = validation.get("reasons")
                if (
                    validation.get("state") != result.get("state")
                    or not isinstance(evidence_rows, list)
                    or len(evidence_rows) > 20
                    or not isinstance(raw_reasons, list)
                    or len(raw_reasons) > 20
                ):
                    raise ValueError
                for evidence_row in evidence_rows:
                    evidence_data = _dict(evidence_row)
                    kind = _required_text(evidence_data.get("kind"))
                    evidence_sha256 = _required_text(evidence_data.get("sha256"))
                    if kind not in {"executor_result", "before_image", "after_image", "report"}:
                        raise ValueError
                    if len(evidence_sha256) != 64 or any(
                        char not in "0123456789abcdef" for char in evidence_sha256
                    ):
                        raise ValueError
                    evidence.append(
                        ReviewEvidence(
                            UUID(str(evidence_data["id"])),
                            kind,
                            evidence_sha256,
                        )
                    )
                reasons = tuple(_required_text(reason) for reason in raw_reasons)
            if action.get("type") not in {"zoom_region", "highlight_region"}:
                raise ValueError
            if inputs is not None:
                manifest = _dict(_dict(inputs)["manifest"])
                if (
                    str(action.get("deck_id")) != str(deck_id)
                    or str(action.get("render_id")) != str(manifest["render_id"])
                    or str(action.get("slide_id")) != slide_id
                ):
                    raise ValueError
            if result.get("state") not in {"candidate", "validated", "rejected", "needs-review"}:
                raise ValueError
            if type(result.get("usable")) is not bool:
                raise ValueError
            if result["usable"] != (result["state"] == "validated"):
                raise ValueError
            actions.append(
                ReviewAction(
                    _required_text(action.get("instance_id")),
                    _required_text(action.get("purpose")),
                    _required_text(action.get("type")),
                    _required_text(action.get("version")),
                    (x, y, width, height),
                    result["state"],
                    result["usable"],
                    tuple(evidence),
                    tuple(r for r in reasons if r is not None),
                    profile,
                    validator,
                    renderer,
                    validation_id,
                )
            )
        slides.append(
            ReviewSlide(slide_id, *widths[slide_id], _summary(row["summary"]), tuple(actions))
        )
    if inputs is not None and [slide.slide_id for slide in slides] != list(widths):
        raise ValueError
    if type(data.get("version")) is not int or data["version"] < 1:
        raise ValueError
    if data.get("publication") not in {"legacy", "partial", "awaiting_validation", "published"}:
        raise ValueError
    if inputs is None and (data["publication"] != "legacy" or slides):
        raise ValueError
    return ReviewAnalysis(
        identifier,
        deck_id,
        data["version"],
        data["publication"],
        action_limit_reached,
        source_revision,
        source_sha256,
        summary,
        tuple(slides),
    )


class ReviewService:
    def __init__(self, config: AppConfig, access: str):
        self.config, self.access = config, access

    def _get(
        self, path: str, *, accept: str, limit: int, allowed_types: set[str] | None = None
    ) -> tuple[bytes, Any]:
        request = Request(
            self.config.api_url + path,
            headers={"Authorization": "Bearer " + self.access, "Accept": accept},
        )
        try:
            with build_opener(NoRedirects()).open(
                request, timeout=self.config.request_timeout
            ) as response:
                content = response.read(limit + 1)
                if len(content) > limit or response.headers.get_content_type() not in (
                    allowed_types or {accept}
                ):
                    raise ValueError
                return content, response.headers
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status in {401, 403}:
                raise LibraryAccessExpired("Sign in again to access this saved review.") from None
            if status == 404:
                raise ServiceError(
                    "No saved analysis is available for this presentation yet."
                ) from None
            if status == 429:
                raise ServiceError("Too many requests. Wait a minute, then retry.") from None
            raise ServiceError(
                "Saved review unavailable. Check the backend and try again."
            ) from None
        except (URLError, OSError, http.client.HTTPException):
            raise ServiceError(
                "Cannot reach the saved review. Check the backend and retry."
            ) from None
        except ValueError:
            raise ServiceError(
                "The backend returned an invalid or oversized review response."
            ) from None

    def analysis(self, deck_id: UUID) -> ReviewAnalysis:
        content, _ = self._get(
            f"/api/v1/analyses/by-deck/{deck_id}",
            accept="application/json",
            limit=MAX_ANALYSIS_BYTES,
        )
        try:
            return parse_analysis(json.loads(content), deck_id)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise ServiceError(
                "The saved analysis is inconsistent. Refresh or contact support."
            ) from None

    def source(self, deck_id: UUID) -> DownloadedPDF:
        return PresentationService(self.config, self.access).download(str(deck_id))

    def evidence(
        self, validation_id: UUID, artifact_id: UUID, expected_sha256: str
    ) -> tuple[bytes, str]:
        content, headers = self._get(
            f"/api/v1/action-validations/{validation_id}/evidence/{artifact_id}",
            accept="*/*",
            limit=MAX_EVIDENCE_BYTES,
            allowed_types={"image/png", "application/json"},
        )
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise ServiceError("Validation evidence integrity check failed.")
        return content, headers.get_content_type()
