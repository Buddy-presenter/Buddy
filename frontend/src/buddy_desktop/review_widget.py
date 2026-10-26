"""Read-only desktop review of persisted PDF analyses and their validation evidence."""

import hashlib
import json
import time
from dataclasses import dataclass
from uuid import UUID

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt, Signal
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
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
    sizes: tuple[tuple[int, int], ...]
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


class PagePreview(QWidget):
    """Aspect-preserving read-only page image with a selected action region overlay."""

    def __init__(self) -> None:
        super().__init__()
        self._image = QImage()
        self._region: tuple[float, float, float, float] | None = None
        self.setMinimumSize(260, 200)
        self.setAccessibleName("Rendered PDF page")

    def set_page(self, image: QImage, region: tuple[float, float, float, float] | None) -> None:
        self._image = image
        self._region = region
        self.update()

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#06090D"))
        if self._image.isNull():
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
            pen = QPen(QColor("#F5843D"), 2)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(left + x * scale, top + y * scale, rw * scale, rh * scale))


class ReviewWidget(Page):
    """A review workspace that never edits or executes generated analysis actions."""

    activity_changed = Signal(bool)
    signin_requested = Signal()
    back_requested = Signal()

    def __init__(self, config: AppConfig, account: AccountWidget) -> None:
        super().__init__(
            "Review", "Inspect saved analysis, PDF pages and action validation evidence."
        )
        self.config, self.account = config, account
        self.runner = TaskRunner(self)
        self.runner.succeeded.connect(self.completed)
        self.runner.failed.connect(self.failed)
        self.runner.idle.connect(self.idle)
        self.generation = 0
        self.request_generation = -1
        self.account_key: tuple[int, str | None, str] | None = None
        self.deck_id: UUID | None = None
        self.deck_title = ""
        self.deck_status = "uploaded"
        self.analysis: ReviewAnalysis | None = None
        self.pdf_content: bytes | None = None
        self.sizes: tuple[tuple[int, int], ...] = ()
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
        self.heading = QLabel("Choose a PDF from your library to review.")
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
        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(14)
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

        self.preview = PagePreview()
        self.preview.setMinimumHeight(280)
        content_layout.addWidget(self.preview, 2)
        self.slide_summary_title = QLabel("Slide summary")
        self.slide_summary_title.setObjectName("groupTitle")
        content_layout.addWidget(self.slide_summary_title)
        self.slide_summary = QLabel()
        self.slide_summary.setWordWrap(True)
        self.slide_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.slide_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        content_layout.addWidget(self.slide_summary)

        self.action_table = QTableWidget(0, 3)
        self.action_table.setHorizontalHeaderLabels(["Purpose", "Type", "Review status"])
        self.action_table.setAccessibleName("Generated slide actions")
        self.action_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.action_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.action_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.action_table.horizontalHeader().setStretchLastSection(True)
        self.action_table.setMinimumHeight(130)
        self.action_table.itemSelectionChanged.connect(self.action_selected)
        content_layout.addWidget(self.action_table)
        self.action_detail = QPlainTextEdit()
        self.action_detail.setReadOnly(True)
        self.action_detail.setAccessibleName("Selected action details and validation")
        self.action_detail.setMaximumHeight(170)
        content_layout.addWidget(self.action_detail)
        evidence_row = QHBoxLayout()
        evidence_row.addWidget(QLabel("Validation evidence"))
        self.evidence_choice = QComboBox()
        self.evidence_choice.setAccessibleName("Available validation evidence")
        evidence_row.addWidget(self.evidence_choice, 1)
        self.open_evidence = QPushButton("Open evidence")
        command(self.open_evidence, "external-link")
        self.open_evidence.clicked.connect(self.load_evidence)
        evidence_row.addWidget(self.open_evidence)
        content_layout.addLayout(evidence_row)
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

    def set_deck(self, deck_id: UUID, title: str, status: str = "uploaded") -> None:
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
        self.slide_summary.clear()
        self.content.hide()
        self.pending_load = False
        if not keep_deck:
            self.deck_id = None
            self.deck_title = ""

    def load(self) -> None:
        deck_id, deck_status, access = self.deck_id, self.deck_status, self.token()
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
            "Loading saved review", "Checking saved analysis and downloading its private PDF."
        )
        self.signin.hide()
        service = ReviewService(self.config, access)

        def work() -> LoadedReview | LibraryAccessExpired:
            analysis: ReviewAnalysis | None = None
            notice = None
            try:
                analysis = service.analysis(deck_id)
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
            try:
                source = service.source(deck_id)
            except LibraryAccessExpired as exc:
                return exc
            content_hash = hashlib.sha256(source.content).hexdigest()
            if analysis is not None and (
                analysis.source_revision != source.revision
                or analysis.source_sha256 != content_hash
            ):
                analysis = None
                notice = (
                    "The saved analysis belongs to a different PDF revision. The PDF is "
                    "available to browse; action details are hidden."
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
        self.pending_operation = ""
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
            if result.analysis is not None:
                self.summary.setText(
                    f"Saved analysis version {result.analysis.version} · "
                    f"{result.analysis.publication.replace('_', ' ')}\n\n"
                    + self._summary_text(
                        result.analysis.presentation_summary, result.analysis.publication
                    )
                )
            self.content.show()
            self.state.hide()
            self._display_page()
        elif isinstance(result, EvidenceResult):
            self._show_evidence(result)
        elif isinstance(result, RenderedPage):
            self.page_index, self.image = result.index, result.image
            self._display_page()

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
        self.activity_changed.emit(False)

    def show_page(self, index: int) -> None:
        if self.runner.busy or self.pdf_content is None or not 0 <= index < len(self.sizes):
            return
        if index == self.page_index:
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
                    action.action_type.replace("_", " "),
                    action.state.replace("-", " "),
                )
            ):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, index)
                self.action_table.setItem(index, column, item)
        if self.action_rows:
            self.action_table.selectRow(0)
        else:
            self.action_detail.setPlainText("No generated actions are available for this slide.")
        self.preview.set_page(self.image, None)
        self.action_table.setAccessibleDescription(
            f"{len(self.action_rows)} actions on slide {self.page_index + 1}"
        )

    def action_selected(self) -> None:
        row = self.action_table.currentRow()
        if not 0 <= row < len(self.action_rows):
            self.preview.set_page(self.image, None)
            return
        action = self.action_rows[row]
        x, y, width, height = action.region
        self.preview.set_page(self.image, action.region)
        validation = (
            f"\nValidation profile: {action.profile}\nValidator: {action.validator_version}\n"
            f"Renderer: {action.renderer_version}\nValidation record: "
            f"{action.validation_id or 'not yet validated'}"
        )
        slide_id = self.analysis.slides[self.page_index].slide_id if self.analysis else "unknown"
        status = (
            "visually verified"
            if action.semantic_verified
            else "execution validated; visual verification pending"
            if action.state == "validated"
            else action.state.replace("-", " ")
        )
        lines = [
            f"Purpose: {action.purpose}",
            f"Type: {action.action_type}",
            f"Version: {action.version}",
            f"Target: slide {self.page_index + 1} ({slide_id})",
            f"Region (PDF source pixels): x={x:g}, y={y:g}, width={width:g}, height={height:g}",
            f"Status: {status} · "
            f"{'usable' if action.usable else 'not usable'}",
            f"Validation kind: {action.validation_kind}",
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
        dialog = QDialog(self)
        dialog.setWindowTitle("Validation evidence · " + result.kind.replace("_", " "))
        dialog.resize(760, 560)
        layout = QVBoxLayout(dialog)
        if result.mime == "image/png":
            buffer = QBuffer()
            buffer.setData(QByteArray(result.content))
            buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            reader = QImageReader(buffer, b"PNG")
            dimensions = reader.size()
            if (
                not dimensions.isValid()
                or dimensions.width() > 8192
                or dimensions.height() > 8192
                or dimensions.width() * dimensions.height() > 16_777_216
            ):
                buffer.close()
                self.state.show_state(
                    "Evidence unavailable", "The evidence image exceeds the display limit."
                )
                return
            image = reader.read()
            buffer.close()
            if (
                image.isNull()
                or image.width() != dimensions.width()
                or image.height() != dimensions.height()
            ):
                self.state.show_state("Evidence unavailable", "The evidence image is invalid.")
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
        dialog.exec()

    def shutdown(self) -> None:
        self.closing = True
        self.clear_private()
        self.runner.stop()
