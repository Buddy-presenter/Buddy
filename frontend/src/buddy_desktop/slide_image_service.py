"""Upload the slide on screen so Jev can see it when deciding (#259).

The desktop already renders the current slide (PDF page image or PowerPoint's exported
preview). Once per slide change it sends a JPEG of at most 1280x1280 to the session; the
server keeps only the latest one in memory. Failures are silent: Jev then decides from text.
"""

from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener
from uuid import UUID

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage

from buddy_desktop.config import AppConfig
from buddy_desktop.library_service import AccessProvider
from buddy_desktop.services import NoRedirects

# TypeSafe accepts 280-1568 px per side; keep uploads small and fast.
MAX_SIDE = 1280
MIN_SIDE = 280
MAX_BYTES = 1_400_000


def slide_jpeg(image: QImage) -> bytes | None:
    """The slide as a JPEG within Jev's size limits, or None if it cannot be made to fit."""
    if image.isNull():
        return None
    if max(image.width(), image.height()) > MAX_SIDE:
        image = image.scaled(
            MAX_SIDE,
            MAX_SIDE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    if min(image.width(), image.height()) < MIN_SIDE:
        return None
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    rgb = image.convertToFormat(QImage.Format.Format_RGB888)
    if not rgb.save(buffer, "JPEG", 80):  # type: ignore[call-overload]
        return None
    encoded = bytes(buffer.data().data())
    return encoded if len(encoded) <= MAX_BYTES else None


class SlideImageService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def upload(self, access: AccessProvider, session_id: str, slide_id: str, jpeg: bytes) -> bool:
        request = Request(
            f"{self.config.api_url}/api/v1/sessions/{UUID(session_id)}/slide-image"
            f"?slide_id={quote(slide_id, safe='')}",
            method="PUT",
            data=jpeg,
            headers={"Authorization": "Bearer " + access(), "Content-Type": "image/jpeg"},
        )
        try:
            with build_opener(NoRedirects()).open(request, timeout=8) as response:
                return bool(response.status == 204)
        except HTTPError as exc:
            exc.close()
            return False
        except (URLError, OSError, TimeoutError):
            return False
