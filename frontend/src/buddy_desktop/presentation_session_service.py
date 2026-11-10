"""Strict, authenticated client for durable presentation-session state."""

import http.client
import json
import math
from dataclasses import dataclass
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
from uuid import UUID

from buddy_desktop.config import AppConfig
from buddy_desktop.library_service import LibraryAccessExpired
from buddy_desktop.services import NoRedirects, ServiceError

FOCUS_VIEWS = {"slide", "zoom_region", "highlight_region"}
AUTONOMY_MODES = {"manual", "assisted", "autonomous"}


class SessionConflict(ServiceError):
    """The server has a newer state version; fetch it before writing again."""


@dataclass(frozen=True)
class SessionState:
    active_slide_id: str
    focus_view: str = "slide"
    focus_region: dict[str, float] | None = None
    autonomy_mode: str = "manual"
    last_accepted_action_id: str | None = None


@dataclass(frozen=True)
class PresentationSession:
    id: str
    owner_id: str
    deck_id: str
    analysis_version_id: str
    status: str
    state_version: int
    state: SessionState | None
    created_at: datetime


def _text(value: object, maximum: int = 128) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError
    return value


def parse_snapshot(payload: object) -> PresentationSession:
    if not isinstance(payload, dict) or set(payload) != {"data", "request_id"}:
        raise ValueError
    _text(payload["request_id"])
    data = payload["data"]
    fields = {
        "id",
        "owner_id",
        "deck_id",
        "analysis_version_id",
        "status",
        "state_version",
        "state",
        "created_at",
    }
    if not isinstance(data, dict) or set(data) != fields:
        raise ValueError
    for key in ("id", "owner_id", "deck_id", "analysis_version_id"):
        UUID(_text(data[key]))
    if data["status"] not in {"created", "active", "ended"}:
        raise ValueError
    if type(data["state_version"]) is not int or data["state_version"] < 0:
        raise ValueError
    raw_state = data["state"]
    state = None
    if raw_state is not None:
        state_fields = {
            "active_slide_id",
            "focus_view",
            "focus_region",
            "autonomy_mode",
            "last_accepted_action_id",
        }
        if not isinstance(raw_state, dict) or set(raw_state) != state_fields:
            raise ValueError
        active_slide_id = _text(raw_state["active_slide_id"])
        focus_view = raw_state["focus_view"]
        autonomy_mode = raw_state["autonomy_mode"]
        last_action = raw_state["last_accepted_action_id"]
        region = raw_state["focus_region"]
        if focus_view not in FOCUS_VIEWS or autonomy_mode not in AUTONOMY_MODES:
            raise ValueError
        if last_action is not None:
            _text(last_action)
        parsed_region = None
        if region is not None:
            keys = {"x", "y", "width", "height"}
            if not isinstance(region, dict) or set(region) != keys:
                raise ValueError
            if any(
                type(region[key]) not in {int, float} or not math.isfinite(region[key])
                for key in keys
            ):
                raise ValueError
            parsed_region = {key: float(region[key]) for key in keys}
            if (
                parsed_region["x"] < 0
                or parsed_region["y"] < 0
                or parsed_region["width"] <= 0
                or parsed_region["height"] <= 0
            ):
                raise ValueError
        if (focus_view == "slide") != (parsed_region is None):
            raise ValueError
        state = SessionState(
            active_slide_id,
            focus_view,
            parsed_region,
            autonomy_mode,
            last_action,
        )
    created_at = datetime.fromisoformat(_text(data["created_at"], 64))
    if created_at.tzinfo is None:
        raise ValueError
    return PresentationSession(
        id=data["id"],
        owner_id=data["owner_id"],
        deck_id=data["deck_id"],
        analysis_version_id=data["analysis_version_id"],
        status=data["status"],
        state_version=data["state_version"],
        state=state,
        created_at=created_at,
    )


class PresentationSessionService:
    def __init__(self, config: AppConfig):
        self.config = config

    def request(
        self,
        access: str,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, object] | None = None,
    ) -> PresentationSession:
        if not access or len(access) > 16300 or not path.startswith("/api/v1/sessions"):
            raise ServiceError("Invalid presentation session request.")
        content = (
            json.dumps(body, separators=(",", ":"), allow_nan=False).encode() if body else None
        )
        request = Request(
            self.config.api_url + path,
            data=content,
            method=method,
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
                    "Sign in again to access the presentation session."
                ) from None
            if status == 409:
                if method == "PATCH" or path.endswith("/end"):
                    raise SessionConflict(
                        "Another controller changed this presentation. Syncing its latest state."
                    ) from None
                raise ServiceError(
                    "This deck is not ready for presentation, or its analysis changed. "
                    "Refresh the library and try again."
                ) from None
            if status == 404:
                raise ServiceError("This presentation session is no longer available.") from None
            if status == 422:
                raise ServiceError(
                    "The presentation state was rejected. Refresh the deck and try again."
                ) from None
            if status == 429:
                raise ServiceError(
                    "Session service is busy. Retry the state sync shortly."
                ) from None
            raise ServiceError("Could not update the presentation session. Try again.") from None
        except (URLError, OSError, http.client.HTTPException):
            raise ServiceError(
                "Cannot reach the session service. Presentation controls remain available."
            ) from None
        except (ValueError, TypeError):
            raise ServiceError("The server returned an invalid session response.") from None
        try:
            return parse_snapshot(json.loads(content))
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise ServiceError("The server returned an invalid session response.") from None

    def start(self, access: str, deck_id: str) -> PresentationSession:
        session = self.request(
            access, "/api/v1/sessions", method="POST", body={"deck_id": str(UUID(deck_id))}
        )
        if (
            session.deck_id != str(UUID(deck_id))
            or session.status != "active"
            or session.state is None
        ):
            raise ServiceError("The server returned an invalid started presentation session.")
        return session

    def read(self, access: str, session_id: str) -> PresentationSession:
        session = self.request(access, f"/api/v1/sessions/{UUID(session_id)}")
        if session.id != str(UUID(session_id)):
            raise ServiceError("The server returned an invalid presentation session.")
        return session

    def resume(self, access: str, session_id: str) -> PresentationSession:
        session = self.request(
            access,
            f"/api/v1/sessions/{UUID(session_id)}/resume",
            method="POST",
            body={},
        )
        if session.id != str(UUID(session_id)) or session.status != "active":
            raise ServiceError("The server returned an invalid resumed presentation session.")
        return session

    def update(
        self,
        access: str,
        session: PresentationSession,
        state: SessionState,
    ) -> PresentationSession:
        updated = self.request(
            access,
            f"/api/v1/sessions/{UUID(session.id)}/state",
            method="PATCH",
            body={
                "expected_state_version": session.state_version,
                "active_slide_id": state.active_slide_id,
                "focus_view": state.focus_view,
                "focus_region": state.focus_region,
                "autonomy_mode": state.autonomy_mode,
                "last_accepted_action_id": state.last_accepted_action_id,
            },
        )
        if (
            updated.id != session.id
            or updated.status != "active"
            or updated.state_version != session.state_version + 1
            or updated.state != state
        ):
            raise ServiceError("The server returned an invalid saved presentation state.")
        return updated

    def end(self, access: str, session: PresentationSession) -> PresentationSession:
        ended = self.request(
            access,
            f"/api/v1/sessions/{UUID(session.id)}/end",
            method="POST",
            body={"expected_state_version": session.state_version},
        )
        if (
            ended.id != session.id
            or ended.status != "ended"
            or ended.state_version not in {session.state_version, session.state_version + 1}
        ):
            raise ServiceError("The server returned an invalid ended presentation session.")
        return ended
