"""Keyboard-accessible library with serialized requests and invalidated late results."""

import time
from collections.abc import Callable
from math import ceil
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from buddy_desktop.account_widget import AccountWidget
from buddy_desktop.config import AppConfig
from buddy_desktop.library_service import (
    DeckSummary,
    LibraryAccessExpired,
    LibraryPage,
    LibraryService,
    ProcessingResult,
)
from buddy_desktop.presentation_service import (
    DownloadedPDF,
    PresentationService,
    SavedPresentation,
    UploadRejected,
)
from buddy_desktop.tasks import TaskRunner
from buddy_desktop.views import StatePanel
from buddy_desktop.visuals import command, icon

STATUS_TEXT = {
    "uploaded": ("Awaiting processing", "No processing job has been scheduled yet."),
    "queued": ("Queued", "Waiting for a processing worker."),
    "processing": ("Processing", "A processing job is running."),
    "failed": ("Failed", "Processing failed. Contact support to retry the job, then refresh."),
    "ready": ("Ready", "Processing completed; analysis and rendered assets are available."),
    "incomplete": (
        "Assets unavailable",
        "The job completed, but analysis or rendered assets are missing. Contact support.",
    ),
}
PROCESSING_TEXT = {
    "uploaded": ("Uploaded", "The PDF is saved and waiting for a processing job."),
    "queued": ("Queued", "Waiting for a processing worker."),
    "inspecting_pdf_metadata": ("Inspecting PDF", "Checking page order and dimensions."),
    "analyzing": ("Analyzing", "Preparing slide summaries and suggested actions."),
    "validating": ("Validating", "Checking suggested actions against the source PDF."),
    "review_ready": ("Review ready", "Summaries and action evidence are available to review."),
    "partial": ("Partial results", "Some results are available; unavailable items show why."),
    "failed": ("Failed", "Processing failed. Contact support if retry is unavailable."),
    "cancelled": ("Cancelled", "Processing was cancelled. You can retry if allowed."),
}


class LibraryWidget(QWidget):
    activity_changed = Signal(bool)
    present_requested = Signal(object)
    review_requested = Signal(object, str, str, str)
    private_invalidated = Signal()
    upload_progress = Signal(object, str, int, int)
    signin_requested = Signal()

    def __init__(self, config: AppConfig, account: AccountWidget) -> None:
        super().__init__()
        self.setAccessibleName("Presentation library")
        self.config = config
        self.account = account
        self.runner = TaskRunner(self)
        self.runner.succeeded.connect(self.completed)
        self.runner.failed.connect(self.failed)
        self.runner.idle.connect(self.idle)
        self.generation = 0
        self.request_generation = -1
        self.page = 1
        self.total = 0
        self.active = False
        self.closing = False
        self.pending = False
        self.account_busy = False
        self.last_page: LibraryPage | None = None
        self.account_key: tuple[int, str | None, str] | None = None
        self.upload_keys: dict[tuple[Path, int, int], UUID] = {}
        self.upload_request: tuple[tuple[Path, int, int], UUID] | None = None
        self.upload_cancelled: Event | None = None
        self.upload_service: PresentationService | None = None
        self.retry_path: Path | None = None
        self.upload_progress.connect(self.update_upload_progress)
        # The window header names this route; the page starts with its tools.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 8, 32, 24)
        layout.setSpacing(14)
        self.toolbar = QWidget()
        controls = QHBoxLayout(self.toolbar)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(8)
        search_label = QLabel("&Search")
        self.search = QLineEdit()
        self.search.setMaxLength(256)
        self.search.setPlaceholderText("Search decks")
        self.search.setAccessibleName("Search decks")
        self.search.addAction(icon("search"), QLineEdit.ActionPosition.LeadingPosition)
        search_label.setBuddy(self.search)
        search_label.hide()
        controls.addWidget(self.search, 1)
        self.sort = QComboBox()
        self.sort.setAccessibleName("Sort decks")
        for title, key in [
            ("Newest", "newest"),
            ("Oldest", "oldest"),
            ("Title A to Z", "title_asc"),
            ("Title Z to A", "title_desc"),
        ]:
            self.sort.addItem(title, key)
        controls.addWidget(self.sort)
        self.refresh = QPushButton("&Refresh")
        self.refresh.setText("")
        self.refresh.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.refresh.setToolTip("Refresh library")
        self.refresh.setAccessibleName("Refresh library")
        command(self.refresh, "refresh-cw", compact=True)
        self.refresh.setObjectName("quietButton")
        self.refresh.setToolTip("Refresh library")
        self.refresh.clicked.connect(self.reload)
        controls.addWidget(self.refresh)
        layout.addWidget(self.toolbar)
        self.selection_actions = QWidget()
        actions = QHBoxLayout(self.selection_actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(16)
        self.upload_button = QPushButton("Upload")
        self.upload_button.setAccessibleName("Upload PDF or PPTX")
        command(self.upload_button, "folder-open", primary=True)
        self.upload_button.setToolTip("Upload a PDF or PowerPoint (PPTX) file")
        self.upload_button.clicked.connect(self.choose_upload)
        controls.addWidget(self.upload_button)
        self.open_button = QPushButton("Present")
        self.open_button.setAccessibleName("Present PDF")
        command(self.open_button, "presentation", primary=True)
        self.open_button.setToolTip("Present the selected deck")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.open_selected)
        self.review_button = QPushButton("Review")
        self.review_button.setAccessibleName("Review saved presentation")
        command(self.review_button, "search", primary=True)
        self.review_button.setToolTip("Review saved summaries and validation evidence")
        self.review_button.setEnabled(False)
        self.review_button.clicked.connect(self.review_selected)
        self.cancel_job_button: QPushButton = QPushButton("Cancel processing")
        self.cancel_job_button.setAccessibleName("Cancel processing job")
        command(self.cancel_job_button, "x")
        self.cancel_job_button.clicked.connect(lambda: self.operate_selected_job("cancel"))
        self.retry_job_button: QPushButton = QPushButton("Retry processing")
        self.retry_job_button.setAccessibleName("Retry processing job")
        command(self.retry_job_button, "refresh-cw")
        self.retry_job_button.clicked.connect(lambda: self.operate_selected_job("retry"))
        self.cancel_job_button.hide()
        self.retry_job_button.hide()
        # Presentation action follows the selected deck details.
        self.upload_panel = QWidget()
        upload_layout = QVBoxLayout(self.upload_panel)
        upload_layout.setContentsMargins(0, 0, 0, 0)
        self.upload_status = QLabel()
        self.upload_status.setWordWrap(True)
        self.upload_status.setTextFormat(Qt.TextFormat.PlainText)
        self.upload_status.setAccessibleName("Upload status")
        upload_layout.setSpacing(8)
        upload_layout.addWidget(self.upload_status)
        self.upload_bar = QProgressBar()
        self.upload_bar.setObjectName("uploadProgress")
        self.upload_bar.setAccessibleName("Presentation transfer progress")
        upload_layout.addWidget(self.upload_bar)
        upload_actions = QHBoxLayout()
        self.cancel_upload_button = QPushButton("Cancel upload")
        command(self.cancel_upload_button, "x")
        self.cancel_upload_button.clicked.connect(self.cancel_upload)
        self.retry_upload_button = QPushButton("Retry upload")
        command(self.retry_upload_button, "refresh-cw")
        self.retry_upload_button.clicked.connect(self.retry_upload)
        for button in (self.cancel_upload_button, self.retry_upload_button):
            button.setAccessibleName(button.text())
            upload_actions.addWidget(button)
        upload_actions.addStretch()
        upload_layout.addLayout(upload_actions)
        layout.addWidget(self.upload_panel)
        self.upload_panel.hide()
        self.state = StatePanel(centered=True)
        self.state.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.state.retry.setAccessibleName("Retry library request")
        self.state.retry_requested.connect(self.reload)
        layout.addWidget(self.state)
        self.signin_button = QPushButton("Sign in")
        self.signin_button.setAccessibleName("Sign in to Buddy")
        command(self.signin_button, "log-in", primary=True)
        self.signin_button.setIcon(QIcon())
        self.signin_button.clicked.connect(self.signin_requested.emit)
        state_layout = self.state.layout()
        assert isinstance(state_layout, QVBoxLayout)
        state_layout.addWidget(self.signin_button, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.table = QTableWidget(0, 4)
        self.table.setObjectName("presentationLibrary")
        self.table.setHorizontalHeaderLabels(["Name", "Status", "Added", "Type"])
        self.table.setAccessibleName("Saved presentations")
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.setMinimumHeight(220)
        self.table.verticalHeader().setDefaultSectionSize(48)
        self.table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setHighlightSections(False)
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        layout.addWidget(self.table, 1)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setTabChangesFocus(True)
        self.detail.setFixedHeight(68)
        self.detail.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        # Long titles still scroll by wheel or keyboard, without a bar beside two lines of text.
        self.detail.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.detail.setAccessibleName("Selected deck processing status")
        # Selected-deck details sit beside the action that uses them.
        actions.addWidget(self.detail, 1)
        actions.addWidget(self.cancel_job_button, alignment=Qt.AlignmentFlag.AlignTop)
        actions.addWidget(self.retry_job_button, alignment=Qt.AlignmentFlag.AlignTop)
        actions.addWidget(self.review_button, alignment=Qt.AlignmentFlag.AlignTop)
        actions.addWidget(self.open_button, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.selection_actions)
        self.empty_space = QWidget()
        layout.addWidget(self.empty_space, 1)
        self.pager = QWidget()
        pager = QHBoxLayout(self.pager)
        pager.setContentsMargins(0, 0, 0, 0)
        self.previous = QPushButton("&Previous")
        self.next = QPushButton("&Next")
        for button, standard_icon, tooltip in [
            (self.previous, QStyle.StandardPixmap.SP_ArrowBack, "Previous page"),
            (self.next, QStyle.StandardPixmap.SP_ArrowForward, "Next page"),
        ]:
            button.setText("")
            button.setIcon(self.style().standardIcon(standard_icon))
            button.setToolTip(tooltip)
        self.previous.setAccessibleName("Previous library page")
        self.next.setAccessibleName("Next library page")
        command(self.previous, "chevron-left", compact=True)
        command(self.next, "chevron-right", compact=True)
        self.previous.setToolTip("Previous page")
        self.next.setToolTip("Next page")
        self.previous.clicked.connect(lambda: self.move_page(-1))
        self.next.clicked.connect(lambda: self.move_page(1))
        self.count = QLabel()
        self.count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.count.setAccessibleName("Library page count")
        pager.addWidget(self.previous)
        pager.addWidget(self.count, 1)
        pager.addWidget(self.next)
        layout.addWidget(self.pager)
        self.table.currentCellChanged.connect(self.selection_changed)
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(300)
        self.debounce.timeout.connect(self.reload)
        self.search.textChanged.connect(self.filter_changed)
        self.search.returnPressed.connect(self.reload)
        self.sort.currentIndexChanged.connect(self.filter_changed)
        self.poll = QTimer(self)
        self.poll.setInterval(15000)
        self.poll.timeout.connect(lambda: self.reload(retain_rows=True))
        account.activity_changed.connect(self.account_activity)
        account.session_changed.connect(self.account_changed)
        self.account_changed()

    def configure(self, config: AppConfig) -> None:
        self.config = config
        self.page = 1
        self.account_changed()

    def clear(self) -> None:
        self.table.setRowCount(0)
        self.table.hide()
        self.detail.clear()
        self.detail.hide()
        self.selection_actions.hide()
        self.empty_space.show()
        self.count.clear()
        self.last_page = None
        self.total = 0
        self.previous.setEnabled(False)
        self.next.setEnabled(False)
        self.open_button.setEnabled(False)
        self.review_button.setEnabled(False)
        self.cancel_job_button.hide()
        self.retry_job_button.hide()

    def invalidate(self) -> None:
        self.generation += 1
        self.poll.stop()
        self.clear()
        self.state.show()

    def token(self) -> str | None:
        session = self.account.session
        if self.account_busy or session is None or session.cancelled.is_set() or not session.tokens:
            return None
        if time.monotonic() >= session.tokens.expires - 30:
            return None
        return session.tokens.access

    def account_activity(self, busy: bool) -> None:
        self.account_busy = busy
        if busy:
            self.clear_upload(clear_keys=False)
            self.invalidate()
            self.pending = False
            self.refresh.setEnabled(False)
            self.upload_button.setEnabled(False)
            self.state.show_state(
                "Checking account", "Library access is paused during account changes."
            )

    def account_changed(self) -> None:
        self.invalidate()
        key = (
            (id(self.account.session), self.account.account_id, self.config.api_url)
            if self.token()
            else None
        )
        if key is None or key != self.account_key:
            self.private_invalidated.emit()
        if key != self.account_key:
            self.clear_upload()
            self.page = 1
            self.upload_keys.clear()
        self.account_key = key
        self._access_layout()
        self.refresh.setEnabled(self.token() is not None)
        self.upload_button.setEnabled(self.token() is not None and not self.runner.busy)
        if not self.token():
            self.state.show_state(
                "Sign in to view your decks",
                "Decks you upload are saved to your Buddy account.",
            )
        elif self.active:
            self.reload()

    def _access_layout(self) -> None:
        signed_in = bool(self.account.session and self.account.session.tokens)
        self.toolbar.setVisible(signed_in)
        self.pager.setVisible(signed_in)
        self.signin_button.setVisible(not signed_in)
        self.signin_button.setEnabled(not self.account_busy)

    def activate(self, active: bool) -> None:
        self.active = active
        self.invalidate()
        self.pending = False
        if active:
            self.reload()

    def filter_changed(self, _value: object = None) -> None:
        self.page = 1
        self.invalidate()
        self.state.show_state("Loading library", "")
        self.debounce.start()

    def move_page(self, delta: int) -> None:
        self.page = max(1, min(ceil(self.total / 20), self.page + delta))
        self.reload()

    def reload(self, *, retain_rows: bool = False) -> None:
        self.debounce.stop()
        if self.closing or not self.active:
            return
        if retain_rows and self.last_page is not None and self.token() and not self.runner.busy:
            self.generation += 1
            self.poll.stop()
        else:
            self.invalidate()
        access = self.token()
        self._access_layout()
        if access is None:
            self.refresh.setEnabled(False)
            self.state.show_state(
                "Sign in to view your decks",
                "Decks you upload are saved to your Buddy account.",
            )
            self.account.check_session()
            return
        if self.runner.busy:
            self.pending = True
            return
        self.pending = False
        service = LibraryService(self.config)
        search, sort, page = self.search.text().strip(), str(self.sort.currentData()), self.page
        self.request_generation = self.generation
        if not retain_rows or self.last_page is None:
            self.state.show_state("Loading library", "")
        self.refresh.setEnabled(False)
        self.activity_changed.emit(True)

        def work() -> LibraryPage | LibraryAccessExpired:
            try:
                return service.list(access, search=search, sort=sort, page=page)
            except LibraryAccessExpired as exc:
                return exc

        self.runner.submit(work)

    def current(self) -> bool:
        return not self.closing and self.active and self.request_generation == self.generation

    def completed(self, _identifier: int, result: object) -> None:
        if isinstance(result, LibraryAccessExpired) and self.upload_request and self.retry_path:
            # Navigation must not hide an expired upload session. Identity changes
            # clear retry_path, so a late old-account response cannot expire a new one.
            self.account.expire_session(str(result))
            return
        if isinstance(result, UploadRejected):
            if self.upload_request:
                fingerprint, key = self.upload_request
                if self.upload_keys.get(fingerprint) == key:
                    del self.upload_keys[fingerprint]
            self.failed(_identifier, str(result))
            return
        # A confirmed upload retires its key even if a routine account check or
        # navigation invalidated the library response while transfer was running.
        if isinstance(result, SavedPresentation) and self.upload_request:
            fingerprint, key = self.upload_request
            if self.upload_keys.get(fingerprint) == key:
                del self.upload_keys[fingerprint]
            if self.retry_path is not None:
                self.upload_status.setText("Presentation saved. Refresh Library to see it.")
                self.upload_bar.setRange(0, 100)
                self.upload_bar.setValue(100)
                self.retry_path = None
                self.retry_upload_button.hide()
        if not self.current():
            return
        if isinstance(result, LibraryAccessExpired):
            self.account.expire_session(str(result))
            return
        if isinstance(result, SavedPresentation):
            self.pending = True
            self.state.show_state("Presentation saved", "Refreshing your library…")
            return
        if isinstance(result, DownloadedPDF):
            self.state.hide()
            self.present_requested.emit(result)
            return
        if isinstance(result, ProcessingResult):
            self.pending = True
            self.state.show_state("Processing updated", "Refreshing the saved job status…")
            return
        if not isinstance(result, LibraryPage):
            self.failed(_identifier, "The backend returned an invalid library response.")
            return
        last_page = max(1, ceil(result.total / result.page_size))
        if not result.items and self.page > last_page:
            self.page = last_page
            self.pending = True
            return
        selected_id = None
        selected_row = self.table.currentRow()
        if self.last_page and 0 <= selected_row < len(self.last_page.items):
            selected_id = self.last_page.items[selected_row].id
        self.last_page = result
        self.total = result.total
        if not result.items:
            self.state.show_state(
                "No matching decks" if self.search.text().strip() else "No saved decks",
                "Try another search."
                if self.search.text().strip()
                else "Your saved presentations will appear here.",
            )
        else:
            self.state.hide()
            self.table.setRowCount(len(result.items))
            for row, deck in enumerate(result.items):
                status, guidance = self.deck_status_text(deck)
                for column, text in enumerate(
                    [
                        deck.title,
                        status,
                        deck.created_at.astimezone().strftime("%Y-%m-%d %H:%M"),
                        deck.source_format.upper(),
                    ]
                ):
                    item = QTableWidgetItem(text)
                    item.setToolTip(
                        deck.title if column == 0 else guidance if column == 1 else text
                    )
                    if column == 0:
                        item.setIcon(icon("presentation"))
                    elif column == 1:
                        item.setForeground(
                            QColor(
                                {
                                    "ready": "#8ED5B6",
                                    "failed": "#FF9B94",
                                    "processing": "#FFB477",
                                    "queued": "#C4B9F3",
                                }.get(deck.status, "#B9B9C4")
                            )
                        )
                    self.table.setItem(row, column, item)
            self.table.show()
            self.detail.show()
            self.selection_actions.show()
            self.empty_space.hide()
            self.table.selectRow(
                next((i for i, deck in enumerate(result.items) if deck.id == selected_id), 0)
            )
            self.selection_changed()
            if any(
                item.processing.state
                in {"queued", "inspecting_pdf_metadata", "analyzing", "validating"}
                if item.processing is not None
                else item.status in {"queued", "processing"}
                for item in result.items
            ):
                self.poll.start()
        self.count.setText(
            f"Page {self.page} of {max(1, ceil(result.total / 20))} · {result.total} decks"
        )
        self.previous.setEnabled(self.page > 1)
        self.next.setEnabled(self.page * 20 < result.total)

    def selection_changed(self, *_args: object) -> None:
        row = self.table.currentRow()
        if self.last_page and 0 <= row < len(self.last_page.items):
            deck = self.last_page.items[row]
            _label, guidance = self.deck_status_text(deck)
            processing = deck.processing
            self.cancel_job_button.setVisible(bool(processing and processing.can_cancel))
            self.cancel_job_button.setEnabled(not self.runner.busy)
            self.retry_job_button.setVisible(bool(processing and processing.can_retry))
            self.retry_job_button.setEnabled(not self.runner.busy)
            if deck.source_format == "pptx":
                guidance = (
                    "Open Review for PowerPoint summaries and actions. Once processing finishes, "
                    "use Open saved PowerPoint there to display the slideshow. " + guidance
                )
            elif deck.can_present:
                guidance = "PDF is available to present. " + guidance
            self.open_button.setEnabled(deck.can_present and not self.runner.busy)
            self.open_button.setToolTip(
                "For PowerPoint, use Review then Open saved PowerPoint."
                if deck.source_format == "pptx"
                else "Present this PDF in Buddy."
            )
            self.review_button.setEnabled(self.token() is not None and not self.runner.busy)
            attempts = f" Processing attempts: {deck.attempts}." if deck.attempts else ""
            if processing is not None and processing.stage:
                guidance += f" Current stage: {processing.stage.replace('_', ' ')}."
            if processing is not None and processing.progress:
                guidance += f" Progress: {processing.progress}%."
            self.detail.setPlainText(deck.title + "\n" + guidance + attempts)

    @staticmethod
    def deck_status_text(deck: DeckSummary) -> tuple[str, str]:
        processing = deck.processing
        if processing is not None:
            if deck.source_format == "pptx":
                if processing.state == "inspecting_pdf_metadata":
                    return "Inspecting PowerPoint", "Reading slide order, dimensions and notes."
                if processing.state == "validating":
                    return "Validating", "Checking suggested actions in desktop PowerPoint."
                if processing.state == "uploaded":
                    return "Uploaded", "The PowerPoint is saved and waiting for processing."
            if processing.state == "failed" and processing.error_code == "analysis_unavailable":
                return (
                    "Results unavailable",
                    "Saved analysis does not match this presentation. Refresh or upload it again.",
                )
            return PROCESSING_TEXT.get(processing.state, STATUS_TEXT[deck.status])
        return STATUS_TEXT[deck.status]

    def operate_selected_job(self, operation: str) -> None:
        row = self.table.currentRow()
        if not self.last_page or not 0 <= row < len(self.last_page.items):
            return
        deck = self.last_page.items[row]
        processing = deck.processing
        access = self.token()
        if (
            processing is None
            or processing.job_id is None
            or access is None
            or self.runner.busy
            or (operation == "cancel" and not processing.can_cancel)
            or (operation == "retry" and not processing.can_retry)
        ):
            return
        job_id = processing.job_id
        assert job_id is not None
        self.poll.stop()
        self.request_generation = self.generation
        self.pending = True
        self.activity_changed.emit(True)
        service = LibraryService(self.config)

        def work() -> object:
            try:
                return service.operate_job(access, job_id, operation)
            except LibraryAccessExpired as exc:
                return exc

        self.runner.submit(work)

    def review_selected(self) -> None:
        row = self.table.currentRow()
        if self.last_page and 0 <= row < len(self.last_page.items):
            deck = self.last_page.items[row]
            if self.token():
                self.review_requested.emit(deck.id, deck.title, deck.status, deck.source_format)

    def failed(self, _identifier: int, message: str) -> None:
        if self.upload_request and self.retry_path is not None and not self.closing:
            self.upload_status.setText(message)
            self.upload_bar.hide()
            self.retry_upload_button.show()
            self.state.hide()
            return
        if self.current():
            self.state.show_state("Library unavailable", message, retry=True)

    def idle(self) -> None:
        self.cancel_upload_button.hide()
        self.upload_cancelled = None
        self.upload_service = None
        self.upload_request = None
        self.activity_changed.emit(False)
        self.refresh.setEnabled(not self.closing and self.token() is not None)
        self.upload_button.setEnabled(not self.closing and self.token() is not None)
        self.retry_upload_button.setEnabled(not self.closing and self.token() is not None)
        self.selection_changed()
        if self.pending and self.active and not self.closing:
            self.reload()

    def choose_upload(self) -> None:
        if self.runner.busy or not self.token():
            return
        filename, _ = QFileDialog.getOpenFileName(
            self, "Upload presentation", "", "Presentations (*.pdf *.pptx)"
        )
        if filename:
            self.start_upload(Path(filename))

    def start_upload(self, path: Path) -> None:
        if self.runner.busy or not self.token() or self.closing:
            return
        try:
            info = path.stat()
        except OSError:
            self.state.show_state("File unavailable", "Choose a readable presentation file.")
            return
        fingerprint = (path, info.st_size, info.st_mtime_ns)
        key = self.upload_keys.setdefault(fingerprint, uuid4())
        self.upload_request = (fingerprint, key)
        self.retry_path = path
        cancelled = Event()
        self.upload_cancelled = cancelled
        self.upload_panel.show()
        self.upload_bar.show()
        self.upload_bar.setRange(0, 0)
        self.upload_status.setText("Preparing presentation…")
        self.cancel_upload_button.show()
        self.cancel_upload_button.setEnabled(True)
        self.retry_upload_button.hide()

        def upload(service: PresentationService) -> object:
            self.upload_service = service
            service.cancelled = cancelled
            service.progress = lambda phase, sent, total: self.upload_progress.emit(
                cancelled, phase, sent, total
            )
            try:
                return service.upload(path, key)
            except UploadRejected as exc:
                # Retire only on the GUI thread, through the normal completion path.
                return exc

        self.run_presentation(upload)

    def retry_upload(self) -> None:
        if self.retry_path is not None:
            self.start_upload(self.retry_path)

    def cancel_upload(self) -> None:
        if self.upload_cancelled is not None:
            self.upload_cancelled.set()
            if self.upload_service is not None:
                self.upload_service.cancel()
            self.cancel_upload_button.setEnabled(False)
            self.upload_status.setText("Stopping upload… The server may already have saved it.")

    def clear_upload(self, *, clear_keys: bool = True) -> None:
        self.cancel_upload()
        self.retry_path = None
        if clear_keys:
            self.upload_keys.clear()
        self.upload_panel.hide()

    def update_upload_progress(self, operation: object, phase: str, sent: int, total: int) -> None:
        if operation is not self.upload_cancelled or self.closing or self.retry_path is None:
            return
        if self.upload_cancelled is not None and self.upload_cancelled.is_set():
            return
        self.upload_status.setText(
            f"{phase}: {sent / 1048576:.1f} of {total / 1048576:.1f} MiB sent"
            if total
            else phase + "…"
        )
        self.upload_bar.setRange(0, total if total else 0)
        self.upload_bar.setValue(sent)

    def open_selected(self) -> None:
        row = self.table.currentRow()
        if self.last_page and 0 <= row < len(self.last_page.items):
            deck = self.last_page.items[row]
            if deck.can_present:
                self.run_presentation(lambda service: service.download(str(deck.id)))

    def run_presentation(self, operation: Callable[[PresentationService], object]) -> None:
        access = self.token()
        if self.runner.busy or not access or self.closing:
            return
        self.poll.stop()
        self.debounce.stop()
        self.request_generation = self.generation
        self.upload_button.setEnabled(False)
        self.open_button.setEnabled(False)
        self.refresh.setEnabled(False)
        self.state.show()
        self.state.show_state("Working…", "Transferring your private presentation…")
        if self.upload_request is not None:
            self.state.hide()
        self.activity_changed.emit(True)
        service = PresentationService(self.config, access)

        def work() -> object:
            try:
                return operation(service)
            except LibraryAccessExpired as exc:
                return exc

        self.runner.submit(work)

    def shutdown(self) -> None:
        self.clear_upload()
        self.closing = True
        self.pending = False
        self.debounce.stop()
        self.invalidate()
        self.runner.stop()
