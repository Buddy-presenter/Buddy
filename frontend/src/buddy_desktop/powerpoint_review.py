"""Owner-bound temporary PowerPoint review session; COM stays off the GUI thread."""

import hashlib
import tempfile
from contextlib import suppress
from pathlib import Path
from threading import Event, RLock

from buddy_desktop.powerpoint import PowerPointAutomation, canonical_path
from buddy_desktop.powerpoint_contract import PowerPointAction, PowerPointError, PowerPointFrame
from buddy_desktop.powerpoint_generated import generated_action
from buddy_desktop.review_service import ReviewAction, ReviewAnalysis, ReviewSlide


class PowerPointReviewSession:
    def __init__(self) -> None:
        self.automation = PowerPointAutomation()
        self.cancelled = Event()
        self.lock = RLock()
        self.folder: tempfile.TemporaryDirectory[str] | None = None
        self.path: Path | None = None
        self.source_hash: str | None = None

    def open(self, content: bytes, analysis: ReviewAnalysis, index: int) -> PowerPointFrame:
        with self.lock:
            if (
                self.cancelled.is_set()
                or len(content) > 64 * 1024 * 1024
                or analysis.source_format != "pptx"
                or not analysis.office_safe
                or hashlib.sha256(content).hexdigest() != analysis.source_sha256
            ):
                raise PowerPointError("A matching safe PowerPoint source is required.")
            first_open = self.path is None
            if first_open:
                self.folder = tempfile.TemporaryDirectory(prefix="buddy-private-pptx-")
                self.path = Path(self.folder.name) / f"buddy-review-{analysis.deck_id}.pptx"
                self.path.write_bytes(content)
                self.source_hash = analysis.source_sha256
            if self.source_hash != analysis.source_sha256:
                raise PowerPointError("The saved PowerPoint source changed. Reopen Review.")
            self._verify_source(analysis.source_sha256)
            try:
                assert self.path is not None
                if first_open or self.automation.status(self.path) is None:
                    self.automation.launch(self.path)
                return self.show(index, analysis.slides[index])
            except Exception:
                if first_open:
                    self.close()
                raise

    def _verify_source(self, expected_hash: str | None) -> None:
        if self.path is None:
            raise PowerPointError("Open the saved PowerPoint source first.")
        with self.path.open("rb") as stream:
            source_content = stream.read(64 * 1024 * 1024 + 1)
        if (
            len(source_content) > 64 * 1024 * 1024
            or hashlib.sha256(source_content).hexdigest() != expected_hash
        ):
            raise PowerPointError("The opened source has changed. Reopen Review.")

    def show(self, index: int, slide: ReviewSlide) -> PowerPointFrame:
        with self.lock:
            if self.path is None or self.cancelled.is_set():
                raise PowerPointError("Open the saved PowerPoint before browsing its slides.")
            info = self.automation.status(self.path)
            if info is None:
                raise PowerPointError("The saved PowerPoint slideshow ended. Open it again.")
            if info.slide_index != index + 1:
                info = self.automation.execute(
                    PowerPointAction("go_to_slide", info, slide_number=index + 1)
                ).info
            if (
                info.slide_id != slide.native_slide_id
                or abs(info.slide_width - slide.width) > 0.01
                or abs(info.slide_height - slide.height) > 0.01
            ):
                raise PowerPointError("The live PowerPoint geometry differs from this analysis.")
            return self.automation.preview(info)

    def apply(self, action: ReviewAction, analysis: ReviewAnalysis, index: int) -> PowerPointFrame:
        with self.lock:
            if (
                self.cancelled.is_set()
                or self.path is None
                or self.source_hash != analysis.source_sha256
                or not action.usable
                or action.state != "validated"
                or action.raw is None
            ):
                raise PowerPointError("Only a validated action from the opened source can run.")
            import json

            if (
                hashlib.sha256(
                    json.dumps(
                        action.raw, sort_keys=True, separators=(",", ":"), allow_nan=False
                    ).encode()
                ).hexdigest()
                != action.action_sha256
            ):
                raise PowerPointError(
                    "This action changed after validation. Reload its saved analysis."
                )
            self._verify_source(analysis.source_sha256)
            frame = self.show(index, analysis.slides[index])
            pythoncom, client = self.automation._start_com()
            try:
                office = client.Dispatch("PowerPoint.Application")
                self.automation._target_window(office, frame.info)
                if f"{office.Version}.{office.Build}" != action.runtime_version:
                    raise PowerPointError(
                        "This Office build differs from the validation runtime. "
                        "Revalidate the action."
                    )
            finally:
                self.automation._end_com(pythoncom)
            slide = analysis.slides[index]
            command = generated_action(
                action.raw,
                frame.info,
                deck_id=str(analysis.deck_id),
                render_id=analysis.render_id,
                slide_id=slide.slide_id,
                native_slide_id=slide.native_slide_id,
                width=slide.width,
                height=slide.height,
            )
            if self.cancelled.is_set():
                raise PowerPointError("The private PowerPoint review was closed.")
            result = self.automation.execute(command)
            return self.automation.preview(result.info)

    def close(self) -> None:
        self.cancelled.set()
        with self.lock:
            if self.path is not None and self.automation._process_probe():
                # Close only this account-bound downloaded source, never unrelated shows.
                with suppress(Exception):
                    pythoncom, client = self.automation._start_com()
                    try:
                        office = client.Dispatch("PowerPoint.Application")
                        for index in range(office.Presentations.Count, 0, -1):
                            presentation = office.Presentations.Item(index)
                            if canonical_path(str(presentation.FullName)) == canonical_path(
                                self.path
                            ):
                                presentation.Saved = True
                                presentation.Close()
                    finally:
                        self.automation._end_com(pythoncom)
            if self.folder is not None:
                self.folder.cleanup()
            self.path = self.folder = None
