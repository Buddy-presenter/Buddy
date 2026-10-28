"""Read-only desktop review of persisted PDF analyses and their validation evidence."""

import hashlib
import json
import sys
import time
from dataclasses import dataclass
from uuid import UUID

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt, Signal
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from buddy_desktop.account_widget import AccountWidget
from buddy_desktop.config import AppConfig
from buddy_desktop.library_service import LibraryAccessExpired
from buddy_desktop.pdf_document import render
from buddy_desktop.powerpoint_review import PowerPointReviewSession
from buddy_desktop.review_service import (
    ReviewAction,
    ReviewAnalysis,
    ReviewService,
)
from buddy_desktop.services import ServiceError
from buddy_desktop.tasks import TaskRunner
from buddy_desktop.views import Page, StatePanel, group
from buddy_desktop.visuals import command


@dataclass(frozen=True)
class LoadedReview:
    analysis: ReviewAnalysis | None
    pdf_content: bytes
    sizes: tuple[tuple[float, float], ...]
    first_image: QImage
    notice: str | None


@dataclass(frozen=True)
class RenderedPage:
    index: int
    image: QImage


@dataclass(frozen=True)
class EvidenceResult:
    kind: str
    content: bytes
    mime: str


def powerpoint_image(png: bytes) -> QImage:
    """Decode real Office indexed/RGB PNG exports with bounded image allocation."""
    if len(png) > 16 * 1024 * 1024 or not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ServiceError("PowerPoint returned an invalid slide preview.")
    buffer = QBuffer()
    buffer.setData(QByteArray(png))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    size = reader.size()
    if (
        not size.isValid()
        or max(size.width(), size.height()) > 2560
        or size.width() * size.height() > 4_194_304
    ):
        buffer.close()
        raise ServiceError("PowerPoint preview exceeds its image limit.")
    image = reader.read()
    buffer.close()
    if image.isNull() or image.size() != size:
        raise ServiceError("PowerPoint could not decode this slide preview.")
    return image


class PagePreview(QWidget):
    """Aspect-preserving read-only page image with a selected action region overlay."""

    def __init__(self) -> None:
        super().__init__()
        self._image = QImage()
        self._region: tuple[float, float, float, float] | None = None
        self._source_size: tuple[float, float] | None = None
        self.setMinimumSize(260, 200)
        self.setAccessibleName("Rendered PDF page")

    def set_page(
        self,
        image: QImage,
        region: tuple[float, float, float, float] | None,
        source_size: tuple[float, float] | None = None,
    ) -> None:
        self._image = image
        self._region = region
        self._source_size = source_size
        self.update()

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#000000"))
        if self._image.isNull():
            if self._source_size is None:
                return
            painter.setPen(QColor("#B5B8BA"))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "Open the saved PowerPoint to see this slide.",
            )
            return
        target = self.rect().adjusted(8, 8, -8, -8)
        scale = min(target.width() / self._image.width(), target.height() / self._image.height())
        width, height = self._image.width() * scale, self._image.height() * scale
        left = target.x() + (target.width() - width) / 2
        top = target.y() + (target.height() - height) / 2
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        from PySide6.QtCore import QRectF

        painter.drawImage(QRectF(left, top, width, height), self._image)
        if self._region is not None:
            x, y, rw, rh = self._region
            if self._source_size is not None:
                sx, sy = (
                    self._image.width() / self._source_size[0],
                    self._image.height() / self._source_size[1],
                )
                x, y, rw, rh = x * sx, y * sy, rw * sx, rh * sy
            pen = QPen(QColor("#F5843D"), 2)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(left + x * scale, top + y * scale, rw * scale, rh * scale))


class ReviewWidget(Page):
    """Saved review; PowerPoint actions run only after an explicit presenter command."""

    activity_changed = Signal(bool)
    signin_requested = Signal()
    back_requested = Signal()

    def __init__(self, config: AppConfig, account: AccountWidget) -> None:
        super().__init__(
            "Review", "Inspect saved summaries, slide content and action validation evidence."
        )
        self.config, self.account = config, account
        self.notice: QLabel
        self.runner = TaskRunner(self)
        self.runner.succeeded.connect(self.completed)
        self.runner.failed.connect(self.failed)
        self.runner.idle.connect(self.idle)
        self.cleanup_runner = TaskRunner(self)
        self.cleanup_runner.idle.connect(self._cleanup_idle)
        self.cleanup_runner.failed.connect(
            lambda _id, message: self.notice.setText("Private PowerPoint cleanup: " + message)
        )
        self._cleanup_queue: list[PowerPointReviewSession] = []
        self._pptx_session: PowerPointReviewSession | None = None
        self._evidence_dialog: QDialog | None = None
        self.source_format = "pdf"
        self.generation = 0
        self.request_generation = -1
        self.account_key: tuple[int, str | None, str] | None = None
        self.deck_id: UUID | None = None
        self.deck_title = ""
        self.deck_status = "uploaded"
        self.analysis: ReviewAnalysis | None = None
        self.pdf_content: bytes | None = None
        self.sizes: tuple[tuple[float, float], ...] = ()
        self.page_index = 0
        self.image = QImage()
        self.action_rows: tuple[ReviewAction, ...] = ()
        self.active = False
        self.closing = False
        self.account_busy = False
        self.pending_operation = ""
        self.pending_load = False

        toolbar = QHBoxLayout()
        self.back = QPushButton("Back to library")
        command(self.back, "chevron-left")
        self.back.setAccessibleName("Return to presentation library")
        self.back.clicked.connect(self.back_requested.emit)
        toolbar.addWidget(self.back)
        self.heading = QLabel("Choose a presentation from your library to review.")
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        self.heading.setObjectName("sectionTitle")
        self.heading.setWordWrap(True)
        toolbar.addWidget(self.heading, 1)
        self.body.addLayout(toolbar)

        self.state = StatePanel(centered=True)
        self.state.retry_requested.connect(self.load)
        self.body.addWidget(self.state)
        self.signin = QPushButton("Sign in")
        command(self.signin, "log-in", primary=True)
        self.signin.clicked.connect(self.signin_requested.emit)
        state_layout = self.state.layout()
        assert isinstance(state_layout, QVBoxLayout)
        state_layout.addWidget(self.signin, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.content = QWidget()
        self.content.setObjectName("reviewWorkspace")
        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(20)
        self.notice = QLabel()
        self.notice.setObjectName("muted")
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        content_layout.addWidget(self.notice)

        self.summary_layout = group("Presentation summary", content_layout)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary_layout.addWidget(self.summary)
        self._full_summary = ""
        self.summary_more = QPushButton("Read full summary")
        self.summary_more.setCheckable(True)
        self.summary_more.setObjectName("linkButton")
        self.summary_more.toggled.connect(self._expand_summary)
        self.summary_layout.addWidget(self.summary_more, alignment=Qt.AlignmentFlag.AlignLeft)
        self.summary_more.hide()

        navigation = QHBoxLayout()
        self.previous = QPushButton("Previous slide")
        command(self.previous, "chevron-left")
        self.previous.clicked.connect(lambda: self.show_page(self.page_index - 1))
        self.slide_counter = QLabel("No slides")
        self.slide_counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.slide_counter.setAccessibleName("Slide position")
        self.next = QPushButton("Next slide")
        command(self.next, "chevron-right")
        self.next.clicked.connect(lambda: self.show_page(self.page_index + 1))
        navigation.addWidget(self.previous)
        navigation.addWidget(self.slide_counter, 1)
        navigation.addWidget(self.next)
        content_layout.addLayout(navigation)
        powerpoint_row = QHBoxLayout()
        self.open_powerpoint = QPushButton("Open saved PowerPoint")
        command(self.open_powerpoint, "external-link")
        self.open_powerpoint.clicked.connect(self.open_saved_powerpoint)
        self.run_powerpoint = QPushButton("Run action")
        self.run_powerpoint.setAccessibleName("Run selected action in PowerPoint")
        command(self.run_powerpoint, "arrow-right", primary=True)
        self.run_powerpoint.clicked.connect(self.run_selected_powerpoint_action)
        for button in (self.open_powerpoint, self.run_powerpoint):
            button.hide()
        powerpoint_row.addWidget(self.open_powerpoint)
        powerpoint_row.addStretch(1)
        content_layout.addLayout(powerpoint_row)

        self.preview = PagePreview()
        self.preview.setMinimumHeight(480)
        content_layout.addWidget(self.preview, 2)
        self.slide_summary_title = QLabel("Slide summary")
        self.slide_summary_title.setObjectName("groupTitle")
        content_layout.addWidget(self.slide_summary_title)
        self.slide_summary = QLabel()
        self.slide_summary.setWordWrap(True)
        self.slide_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.slide_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        content_layout.addWidget(self.slide_summary)

        self.actions_title = QLabel("Suggested actions")
        self.actions_title.setObjectName("sectionTitle")
        action_header = QHBoxLayout()
        action_header.addWidget(self.actions_title, 1)
        action_header.addWidget(self.run_powerpoint)
        content_layout.addLayout(action_header)
        self.action_table = QTableWidget(0, 4)
        self.action_table.setObjectName("reviewActions")
        self.action_table.setHorizontalHeaderLabels(["What it does", "Tool", "Availability", ""])
        self.action_table.setAccessibleName("Generated slide actions")
        self.action_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.action_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.action_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.action_table.horizontalHeader().setStretchLastSection(False)
        self.action_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            self.action_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.action_table.verticalHeader().hide()
        self.action_table.setWordWrap(True)
        self.action_table.setShowGrid(False)
        self.action_table.setMinimumHeight(130)
        self.action_table.itemSelectionChanged.connect(self.action_selected)
        content_layout.addWidget(self.action_table)
        self.action_status = QLabel("Select an action to inspect or run it.")
        self.action_status.setObjectName("muted")
        self.action_status.setWordWrap(True)
        self.action_status.setTextFormat(Qt.TextFormat.PlainText)
        content_layout.addWidget(self.action_status)
        self.details_toggle = QPushButton("Action details & evidence")
        self.details_toggle.setCheckable(True)
        command(self.details_toggle, "chevron-right")
        content_layout.addWidget(self.details_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        self.details_toggle.hide()  # Each action row exposes its own Details control.
        self.details_panel = QWidget()
        self.details_panel.setObjectName("actionDetails")
        details_layout = QVBoxLayout(self.details_panel)
        details_layout.setContentsMargins(16, 16, 16, 16)
        details_layout.setSpacing(12)
        details_header = QHBoxLayout()
        details_header.addWidget(QLabel("Action details & execution proof"), 1)
        hide_details = QPushButton("Hide details")
        hide_details.clicked.connect(lambda: self.details_toggle.setChecked(False))
        details_header.addWidget(hide_details)
        details_layout.addLayout(details_header)
        self.details_panel.hide()
        self.details_toggle.toggled.connect(self.details_panel.setVisible)
        self.action_detail = QPlainTextEdit()
        self.action_detail.setReadOnly(True)
        self.action_detail.setAccessibleName("Selected action details and validation")
        self.action_detail.setMaximumHeight(170)
        details_layout.addWidget(self.action_detail)
        evidence_row = QHBoxLayout()
        evidence_row.addWidget(QLabel("Execution proof"))
        self.evidence_choice = QComboBox()
        self.evidence_choice.setAccessibleName("Available validation evidence")
        evidence_row.addWidget(self.evidence_choice, 1)
        self.open_evidence = QPushButton("View proof")
        command(self.open_evidence, "external-link")
        self.open_evidence.setToolTip(
            "View the real before/after slide images or execution report."
        )
        self.open_evidence.clicked.connect(self.load_evidence)
        evidence_row.addWidget(self.open_evidence)
        details_layout.addLayout(evidence_row)
        content_layout.addWidget(self.details_panel)
        self.body.addWidget(self.content, 1)
        self.content.hide()

        account.session_changed.connect(self.account_changed)
        account.activity_changed.connect(self.account_activity)
        self.account_changed()

    def token(self) -> str | None:
        session = self.account.session
        if self.account_busy or session is None or session.cancelled.is_set() or not session.tokens:
            return None
        if time.monotonic() >= session.tokens.expires - 30:
            return None
        return session.tokens.access

    def configure(self, config: AppConfig) -> None:
        self.config = config
        self.clear_private()
        self.account_changed()

    def set_deck(
        self, deck_id: UUID, title: str, status: str = "uploaded", source_format: str = "pdf"
    ) -> None:
        if source_format not in {"pdf", "pptx"}:
            raise ServiceError("Unsupported presentation source format.")
        self.source_format = source_format
        self.deck_id, self.deck_title, self.deck_status = deck_id, title, status
        self.clear_private(keep_deck=True)
        self.pending_load = True
        if self.active:
            self.load()

    def activate(self, active: bool) -> None:
        self.active = active
        if active and self.deck_id is not None and self.token() and self.pdf_content is None:
            if self.runner.busy:
                if self.request_generation != self.generation:
                    self.pending_load = True
            else:
                self.load()

    def account_activity(self, busy: bool) -> None:
        self.account_busy = busy
        if busy:
            self.clear_private()
            self.state.show_state("Checking account", "Private review data is being cleared.")

    def account_changed(self) -> None:
        self.generation += 1
        key = (
            (id(self.account.session), self.account.account_id, self.config.api_url)
            if self.token()
            else None
        )
        if key != self.account_key:
            self.clear_private(keep_deck=False)
            self.deck_id = None
            self.deck_title = ""
        self.account_key = key
        if not self.token():
            self.state.show_state(
                "Sign in to review", "Saved presentations are private to your account."
            )
            self.signin.show()
        elif self.deck_id is None:
            self.state.show_state(
                "Choose a presentation", "Open Review from a PDF in your library."
            )
            self.signin.hide()

    def clear_private(self, *, keep_deck: bool = False) -> None:
        self._close_evidence()
        self.details_toggle.setChecked(False)
        if self._pptx_session is not None:
            self._pptx_session.cancelled.set()
            self._cleanup_queue.append(self._pptx_session)
            self._pptx_session = None
            self._cleanup_idle()
        self.generation += 1
        self.analysis = None
        self.pdf_content = None
        self.sizes = ()
        self.page_index = 0
        self.image = QImage()
        self.action_rows = ()
        self.action_table.setRowCount(0)
        self.action_detail.clear()
        self.evidence_choice.clear()
        self.preview.set_page(QImage(), None)
        self.notice.clear()
        self.summary.clear()
        self._full_summary = ""
        self.summary_more.setChecked(False)
        self.summary_more.hide()
        self.slide_summary.clear()
        self.content.hide()
        self.run_powerpoint.setEnabled(False)
        self.open_powerpoint.setEnabled(False)
        self.pending_load = False
        if not keep_deck:
            self.deck_id = None
            self.deck_title = ""

    def load(self) -> None:
        deck_id, deck_status, access = self.deck_id, self.deck_status, self.token()
        source_format = self.source_format
        if deck_id is None or not access or self.closing:
            return
        if self.runner.busy:
            self.pending_load = True
            return
        self.pending_load = False
        self.generation += 1
        self.request_generation = self.generation
        self.pending_operation = "load"
        self.state.show_state(
            "Loading saved review", "Checking saved analysis and its private presentation source."
        )
        self.signin.hide()
        service = ReviewService(self.config, access)

        def work() -> LoadedReview | LibraryAccessExpired:
            analysis: ReviewAnalysis | None = None
            notice = None
            try:
                analysis = (
                    service.analysis(deck_id, "pptx")
                    if source_format == "pptx"
                    else service.analysis(deck_id)
                )
            except LibraryAccessExpired as exc:
                return exc
            except ServiceError as exc:
                notice = str(exc)
                if analysis is None:
                    notice += " " + {
                        "uploaded": "Library status: awaiting processing.",
                        "queued": "Library status: processing is queued.",
                        "processing": "Library status: processing is still running.",
                        "failed": "Library status: processing failed.",
                        "incomplete": (
                            "Library status: processing finished with unavailable assets."
                        ),
                        "ready": "Library status: processing is complete.",
                    }.get(deck_status, "")
            if source_format == "pptx" and analysis is None:
                # A PowerPoint preview needs its saved native slide manifest. Do
                # not download a large source while analysis is still pending.
                return LoadedReview(None, b"", (), QImage(), notice)
            try:
                source = (
                    service.source(deck_id, "pptx")
                    if source_format == "pptx"
                    else service.source(deck_id)
                )
            except LibraryAccessExpired as exc:
                return exc
            content_hash = hashlib.sha256(source.content).hexdigest()
            if analysis is not None and (
                analysis.source_revision != source.revision
                or analysis.source_sha256 != content_hash
                or analysis.source_format != source_format
            ):
                analysis = None
                notice = (
                    "The saved analysis belongs to a different source revision. "
                    "Action details are hidden; reload after processing the current source."
                )
            if source_format == "pptx":
                return LoadedReview(
                    analysis,
                    source.content,
                    tuple((slide.width, slide.height) for slide in analysis.slides)
                    if analysis
                    else (),
                    QImage(),
                    notice
                    or (
                        "PowerPoint summaries use extracted text and notes; unreadable visuals "
                        "are identified explicitly. Open the saved source to inspect its slides."
                    ),
                )
            metadata, first = render(source.content, 0)
            sizes = tuple(tuple(size) for size in metadata["sizes"])
            if (
                analysis is not None
                and tuple((slide.width, slide.height) for slide in analysis.slides) != sizes
            ):
                analysis = None
                notice = (
                    "PDF page geometry differs from the saved analysis. The PDF is available "
                    "to browse without action overlays."
                )
            if analysis is not None and len(analysis.slides) != len(sizes):
                analysis = None
                notice = (
                    "PDF page count differs from the saved analysis. The PDF is available to "
                    "browse without action overlays."
                )
            return LoadedReview(analysis, source.content, sizes, first, notice)

        self.activity_changed.emit(True)
        self.runner.submit(work)

    def completed(self, identifier: int, result: object) -> None:
        if self.closing or self.request_generation != self.generation or not self.active:
            return
        if identifier < 1:
            return
        operation, self.pending_operation = self.pending_operation, ""
        if isinstance(result, LibraryAccessExpired):
            self.account.expire_session(str(result))
        elif isinstance(result, LoadedReview):
            self.analysis = result.analysis
            self.pdf_content = result.pdf_content
            # This account-bound PDF is cleared when the account or backend changes.
            notices = [result.notice] if result.notice else []
            if result.analysis is not None and result.analysis.action_limit_reached:
                notices.append("All 40 available action slots were used in this analysis.")
            self.notice.setText("\n\n".join(notices))
            self.sizes = result.sizes
            self.image = result.first_image
            self.page_index = 0
            self.heading.setText(self.deck_title)
            self.summary.setText(
                self._summary_text(
                    result.analysis.presentation_summary if result.analysis else None,
                    result.analysis.publication if result.analysis else "unavailable",
                )
            )
            self.content.show()
            self._full_summary = self.summary.text()
            self.summary_more.setChecked(False)
            self.summary_more.setVisible(len(self._full_summary) > 600)
            self._expand_summary(False)
            powerpoint = self.source_format == "pptx"
            self.open_powerpoint.setVisible(powerpoint)
            self.run_powerpoint.setVisible(powerpoint)
            self.open_powerpoint.setEnabled(
                powerpoint
                and sys.platform == "win32"
                and self.analysis is not None
                and self.analysis.office_safe
            )
            self.open_powerpoint.setToolTip(
                "Desktop PowerPoint on Windows is required."
                if sys.platform != "win32"
                else "Sources with external links or embedded active material cannot be opened."
                if self.analysis is not None and not self.analysis.office_safe
                else "Open the verified source read-only in desktop PowerPoint."
            )
            self.state.hide()
            self._display_page()
        elif isinstance(result, EvidenceResult):
            self._show_evidence(result)
        elif isinstance(result, RenderedPage):
            self.page_index, self.image = result.index, result.image
            self._display_page()
            if operation == "powerpoint_action":
                self.notice.setText(
                    "PowerPoint action completed on the saved source. Execution validation "
                    "does not establish semantic correctness."
                )

    @staticmethod
    def _summary_text(summary: object, publication: str) -> str:
        if summary is None:
            return (
                f"Analysis status: {publication.replace('_', ' ')}. "
                "No matching saved analysis is available yet."
            )
        status = getattr(summary, "status", "unavailable")
        if status == "available":
            return getattr(summary, "text", None) or "Summary text is unavailable."
        if status == "pending":
            return "The presentation summary is still being prepared."
        return "Presentation summary unavailable: " + str(
            getattr(summary, "reason", None) or "No reason was saved."
        )

    def _expand_summary(self, expanded: bool) -> None:
        text = self._full_summary
        if not expanded and len(text) > 600:
            text = text[:600].rsplit(" ", 1)[0] + "…"
        self.summary.setText(text)
        self.summary_more.setText("Show less" if expanded else "Read full summary")

    def failed(self, identifier: int, message: str) -> None:
        if self.closing or identifier < 1 or self.request_generation != self.generation:
            return
        operation, self.pending_operation = self.pending_operation, ""
        if operation == "evidence":
            self.notice.setText("Validation evidence unavailable: " + message)
            self.open_evidence.setEnabled(self.evidence_choice.count() > 0)
            return
        if operation == "page":
            self.notice.setText("Page could not be rendered: " + message)
            return
        if operation in {"powerpoint_open", "powerpoint_action"}:
            self.notice.setText("PowerPoint: " + message)
            return
        self.state.show_state("Review unavailable", message, retry=True)
        self.content.hide()

    def idle(self) -> None:
        if (
            self.pending_load
            and self.active
            and self.deck_id is not None
            and self.token()
            and not self.closing
        ):
            self.load()
            return
        if self.source_format == "pptx" and self.action_rows:
            self.action_selected()
        self.activity_changed.emit(self.cleanup_runner.busy)

    def _cleanup_idle(self) -> None:
        if self.cleanup_runner.busy:
            return
        if self._cleanup_queue:
            session = self._cleanup_queue.pop(0)
            self.activity_changed.emit(True)
            self.cleanup_runner.submit(session.close)
        else:
            self.activity_changed.emit(self.runner.busy)

    def open_saved_powerpoint(self) -> None:
        analysis, content = self.analysis, self.pdf_content
        if (
            self.runner.busy
            or self.closing
            or self.source_format != "pptx"
            or analysis is None
            or not analysis.office_safe
            or content is None
            or not self.token()
        ):
            return
        if self._pptx_session is None or self._pptx_session.cancelled.is_set():
            self._pptx_session = PowerPointReviewSession()
        session, index = self._pptx_session, self.page_index
        self.request_generation, self.pending_operation = self.generation, "powerpoint_open"
        self.activity_changed.emit(True)
        self.runner.submit(
            lambda: RenderedPage(
                index, powerpoint_image(session.open(content, analysis, index).png)
            )
        )

    def run_selected_powerpoint_action(self) -> None:
        row = self.action_table.currentRow()
        analysis, session = self.analysis, self._pptx_session
        if (
            self.runner.busy
            or self.closing
            or not self.token()
            or session is None
            or analysis is None
            or self.source_format != "pptx"
            or not 0 <= row < len(self.action_rows)
        ):
            return
        action, index = self.action_rows[row], self.page_index
        if not action.usable:
            return
        self.request_generation, self.pending_operation = self.generation, "powerpoint_action"
        self.activity_changed.emit(True)
        self.runner.submit(
            lambda: RenderedPage(
                index, powerpoint_image(session.apply(action, analysis, index).png)
            )
        )

    def show_page(self, index: int) -> None:
        if self.runner.busy or self.pdf_content is None or not 0 <= index < len(self.sizes):
            return
        if index == self.page_index:
            return
        if self.source_format == "pptx":
            session, analysis = self._pptx_session, self.analysis
            if session is None or analysis is None:
                self.page_index, self.image = index, QImage()
                self._display_page()
            else:
                self.request_generation, self.pending_operation = self.generation, "page"
                self.runner.submit(
                    lambda: RenderedPage(
                        index,
                        powerpoint_image(session.show(index, analysis.slides[index]).png),
                    )
                )
            return
        self.request_generation = self.generation
        self.pending_operation = "page"
        content = self.pdf_content
        self.activity_changed.emit(True)
        self.runner.submit(lambda: RenderedPage(index, render(content, index)[1]))

    def _display_page(self) -> None:
        if not self.sizes or not 0 <= self.page_index < len(self.sizes):
            return
        slide = self.analysis.slides[self.page_index] if self.analysis else None
        previous_row = self.action_table.currentRow()
        selected_action = (
            self.action_rows[previous_row].instance_id
            if 0 <= previous_row < len(self.action_rows)
            else None
        )
        self.slide_counter.setText(f"Slide {self.page_index + 1} of {len(self.sizes)}")
        self.previous.setEnabled(self.page_index > 0)
        self.next.setEnabled(self.page_index + 1 < len(self.sizes))
        self.slide_summary.setText(
            self._summary_text(slide.summary, self.analysis.publication)
            if slide and self.analysis
            else "No saved summary is available for this page."
        )
        self.action_table.setRowCount(0)
        self.action_detail.clear()
        self.evidence_choice.clear()
        self.action_rows = slide.actions if slide else ()
        for index, action in enumerate(self.action_rows):
            self.action_table.insertRow(index)
            for column, text in enumerate(
                (
                    action.purpose,
                    action.action_type.replace("draw_", "").replace("_", " ").title(),
                    "Ready"
                    if action.usable
                    else "Checking"
                    if action.state == "candidate"
                    else "Needs review",
                )
            ):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, index)
                self.action_table.setItem(index, column, item)
            detail_button = QPushButton("Details")
            detail_button.setAccessibleName(f"Details for action {index + 1}")
            detail_button.clicked.connect(
                lambda _checked=False, row=index: self.show_action_details(row)
            )
            self.action_table.setCellWidget(index, 3, detail_button)
            self.action_table.setRowHeight(index, 64)
        if self.action_rows:
            self.action_table.selectRow(
                next(
                    (
                        i
                        for i, action in enumerate(self.action_rows)
                        if action.instance_id == selected_action
                    ),
                    0,
                )
            )
        else:
            self.action_detail.setPlainText("No generated actions are available for this slide.")
        self.actions_title.setText(
            f"Suggested actions · {sum(a.usable for a in self.action_rows)} ready "
            f"/ {len(self.action_rows)} total"
        )
        self.preview.set_page(self.image, None)
        self.action_table.setAccessibleDescription(
            f"{len(self.action_rows)} actions on slide {self.page_index + 1}"
        )

    def action_selected(self) -> None:
        self.run_powerpoint.setEnabled(False)
        row = self.action_table.currentRow()
        if not 0 <= row < len(self.action_rows):
            self.preview.set_page(self.image, None)
            return
        action = self.action_rows[row]
        self.details_toggle.setChecked(False)
        self.action_status.setText(
            "Ready to run on this slide."
            if action.usable
            else "This action is still being checked."
            if action.state == "candidate"
            else "This action is unavailable. Open Details to see why."
        )
        x, y, width, height = action.region
        slide = self.analysis.slides[self.page_index] if self.analysis else None
        self.preview.set_page(
            self.image,
            None if self.source_format == "pptx" else action.region,
            (slide.width, slide.height) if slide and self.source_format == "pptx" else None,
        )
        self.run_powerpoint.setEnabled(
            self.source_format == "pptx"
            and self._pptx_session is not None
            and action.usable
            and not self.runner.busy
        )
        validation = (
            f"\nValidation profile: {action.profile}\nValidator: {action.validator_version}\n"
            f"Renderer: {action.renderer_version}\nValidation record: "
            f"{action.validation_id or 'not yet validated'}"
        )
        slide_id = self.analysis.slides[self.page_index].slide_id if self.analysis else "unknown"
        units = "PowerPoint slide points" if self.source_format == "pptx" else "PDF source pixels"
        lines = [
            f"Purpose: {action.purpose}",
            f"Type: {action.action_type}",
            f"Version: {action.version}",
            f"Target: slide {self.page_index + 1} ({slide_id})",
            f"Region ({units}): x={x:g}, y={y:g}, width={width:g}, height={height:g}",
            f"Status: {action.state.replace('-', ' ')} · "
            f"{'usable' if action.usable else 'not usable'}",
        ]
        if action.reasons:
            lines.append("Validation notes: " + "; ".join(action.reasons))
        lines.append(validation.strip())
        self.action_detail.setPlainText("\n".join(lines))
        for evidence in action.evidence:
            self.evidence_choice.addItem(
                f"{evidence.kind.replace('_', ' ')} · {evidence.sha256[:12]}", evidence
            )
        self.open_evidence.setEnabled(bool(action.evidence and action.validation_id))

    def show_action_details(self, row: int) -> None:
        show = self.action_table.currentRow() != row or not self.details_panel.isVisible()
        self.action_table.selectRow(row)
        self.details_toggle.setChecked(show)

    def _close_evidence(self) -> None:
        dialog, self._evidence_dialog = self._evidence_dialog, None
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()

    def load_evidence(self) -> None:
        row = self.action_table.currentRow()
        if self.runner.busy or not 0 <= row < len(self.action_rows):
            return
        action = self.action_rows[row]
        evidence = self.evidence_choice.currentData()
        validation_id = action.validation_id
        access = self.token()
        if evidence is None or validation_id is None or access is None:
            return
        service = ReviewService(self.config, access)
        generation = self.generation
        self.request_generation = generation
        self.pending_operation = "evidence"
        self.notice.setText("Loading execution proof…")
        self.open_evidence.setEnabled(False)
        self.activity_changed.emit(True)

        def work() -> EvidenceResult | LibraryAccessExpired:
            try:
                content, mime = service.evidence(validation_id, evidence.id, evidence.sha256)
                return EvidenceResult(evidence.kind, content, mime)
            except LibraryAccessExpired as exc:
                return exc

        self.runner.submit(work)

    def _show_evidence(self, result: EvidenceResult) -> None:
        self._close_evidence()
        dialog = QDialog(self)
        dialog.setWindowTitle("Execution proof · " + result.kind.replace("_", " ").title())
        dialog.resize(760, 560)
        layout = QVBoxLayout(dialog)
        introduction = QLabel(
            "Actual PowerPoint output before or after this action."
            if result.mime == "image/png"
            else "Execution report: checks performed and the reason for this action's availability."
        )
        introduction.setWordWrap(True)
        layout.addWidget(introduction)
        if result.mime == "image/png":
            buffer = QBuffer()
            buffer.setData(QByteArray(result.content))
            buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            reader = QImageReader(buffer)
            dimensions = reader.size()
            if (
                not dimensions.isValid()
                or dimensions.width() > 8192
                or dimensions.height() > 8192
                or dimensions.width() * dimensions.height() > 16_777_216
            ):
                buffer.close()
                self.notice.setText("Evidence unavailable: the image exceeds its display limit.")
                return
            image = reader.read()
            buffer.close()
            if (
                image.isNull()
                or image.width() != dimensions.width()
                or image.height() != dimensions.height()
            ):
                self.notice.setText("Evidence unavailable: the image is invalid.")
                return
            preview = QLabel()
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            preview.setPixmap(
                QPixmap.fromImage(image).scaled(
                    720,
                    480,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            layout.addWidget(preview, 1)
        else:
            text = QPlainTextEdit()
            text.setReadOnly(True)
            try:
                value = json.loads(result.content)
                text.setPlainText(json.dumps(value, ensure_ascii=False, indent=2))
            except (ValueError, UnicodeDecodeError):
                text.setPlainText("Evidence report is not valid JSON.")
            layout.addWidget(text, 1)
        close = QPushButton("Close")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        self._evidence_dialog = dialog
        self.notice.setText("Execution proof loaded.")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def shutdown(self) -> None:
        self.closing = True
        self.clear_private()
        self.runner.stop()
