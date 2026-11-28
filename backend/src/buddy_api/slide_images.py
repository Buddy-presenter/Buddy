"""The current slide's image for Jev's live decisions (#259).

The desktop already renders the slide on screen (PDF page or PowerPoint export) and uploads
it once per slide change. The server keeps only the latest image per session in memory,
never on disk, and Jev receives it with each decision on that slide. Measured with the real
Jev: visual references ("the red one", "the chart on the left") 18/85 -> 84/85, ordinary
sentences unchanged, about +35 ms per decision.
"""

import asyncio
import io
import time
from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from PIL import Image
from starlette.exceptions import HTTPException

from buddy_api.auth import Principal, authenticated
from buddy_api.contracts import ErrorResponse

MAX_BYTES = 1_500_000
# TypeSafe accepts 280-1568 px per side and 78,400-2,458,624 pixels per image.
MIN_SIDE, MAX_SIDE = 280, 1568
MAX_SESSIONS = 512
TTL_SECONDS = 4 * 3600


@dataclass(frozen=True)
class SlideImage:
    owner: UUID
    slide_id: str
    format: Literal["png", "jpg"]
    data: bytes
    stored_at: float


class SlideImages:
    """Latest slide image per session, in memory only. A miss means Jev decides from text."""

    def __init__(self) -> None:
        self._images: dict[UUID, SlideImage] = {}

    def put(self, session: UUID, image: SlideImage) -> None:
        now = time.monotonic()
        self._images = {
            key: value
            for key, value in self._images.items()
            if now - value.stored_at <= TTL_SECONDS
        }
        if session not in self._images and len(self._images) >= MAX_SESSIONS:
            self._images.pop(min(self._images, key=lambda k: self._images[k].stored_at))
        self._images[session] = image

    def get(self, session: UUID, owner: UUID, slide_id: str) -> SlideImage | None:
        image = self._images.get(session)
        if (
            image is None
            or image.owner != owner
            or image.slide_id != slide_id
            or time.monotonic() - image.stored_at > TTL_SECONDS
        ):
            return None
        return image


def checked_image(data: bytes) -> Literal["png", "jpg"]:
    """Accept only a still PNG or JPEG that Jev can take as is; raise ValueError otherwise."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        kind: Literal["png", "jpg"] = "png"
    elif data.startswith(b"\xff\xd8\xff"):
        kind = "jpg"
    else:
        raise ValueError("unsupported format")
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        if getattr(image, "is_animated", False):
            raise ValueError("animated image")
        image.verify()
    if not (MIN_SIDE <= width <= MAX_SIDE and MIN_SIDE <= height <= MAX_SIDE):
        raise ValueError("unsupported size")
    return kind


def slide_image_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/sessions",
        tags=["presentation agent"],
        # Explicit descriptions: Python versions name some statuses differently (413).
        responses={
            401: {"model": ErrorResponse, "description": "Sign-in required"},
            403: {"model": ErrorResponse, "description": "Not the session owner"},
            404: {"model": ErrorResponse, "description": "Session not found"},
            409: {"model": ErrorResponse, "description": "The session ended"},
            413: {"model": ErrorResponse, "description": "Slide image too large"},
            422: {"model": ErrorResponse, "description": "Unsupported slide image"},
        },
    )

    @router.put("/{identifier}/slide-image", status_code=204)
    async def put_slide_image(
        identifier: UUID,
        request: Request,
        principal: Annotated[Principal, Depends(authenticated)],
        slide_id: Annotated[str, Query(min_length=1, max_length=128, pattern=r"^\S+$")],
    ) -> Response:
        from buddy_api.presentation_sessions import owned_session

        body = bytearray()
        try:
            async with asyncio.timeout(10):
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise HTTPException(413, "Slide image is too large.")
        except TimeoutError:
            raise HTTPException(408, "Slide image upload timed out.") from None
        try:
            kind = await asyncio.to_thread(checked_image, bytes(body))
        except (ValueError, OSError, Image.DecompressionBombError):
            raise HTTPException(422, "Send a PNG or JPEG between 280 and 1568 pixels.") from None
        database = request.app.state.database
        if database is None:
            raise HTTPException(503, "Sessions are unavailable.")
        async with database.sessions() as db:
            session = await owned_session(db, principal, identifier)
            if session.status != "active":
                raise HTTPException(409, "The session ended.")
        request.app.state.slide_images.put(
            identifier, SlideImage(principal.id, slide_id, kind, bytes(body), time.monotonic())
        )
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    return router
