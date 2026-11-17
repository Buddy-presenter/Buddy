"""Local rendered-slide controls using the production six-command executor."""

import hashlib
import logging
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from datetime import date
from pathlib import Path
from queue import Empty
from threading import Event
from typing import Any, cast
from uuid import UUID

from PySide6.QtCore import QPointF, QRectF, QSettings, Qt, QTimer, Signal, Slot
from PySide6.QtGui import (
    QAction,
    QColor,
    QGuiApplication,
    QHideEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QScreen,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from buddy_desktop.agent_loop import AgentLoop
from buddy_desktop.agent_service import AgentService
from buddy_desktop.config import AppConfig
from buddy_desktop.fullscreen import FullscreenPresentation
from buddy_desktop.library_service import LibraryAccessExpired
from buddy_desktop.microphone import MicrophoneController
from buddy_desktop.powerpoint import PowerPointAutomation, PresentationInfo
from buddy_desktop.powerpoint_contract import (
    PowerPointAction,
    PowerPointActionResult,
    PowerPointError,
    PowerPointFrame,
)
from buddy_desktop.powerpoint_controls import PowerPointControls
from buddy_desktop.powerpoint_present import PowerPointPresent
from buddy_desktop.powerpoint_session_binding import PowerPointSessionBinding
from buddy_desktop.presentation_session_service import (
    PresentationSession,
    PresentationSessionService,
    SessionAccessDenied,
    SessionConflict,
    SessionState,
)
from buddy_desktop.review_service import ReviewAnalysis, ReviewService
from buddy_desktop.services import ServiceError
from buddy_desktop.session_events import SessionEvent, SessionEventStream
from buddy_desktop.slide_contract import ContractError, Region, parse_action
from buddy_desktop.slide_executor import Completion, SlideExecutor
from buddy_desktop.slide_surface import SlideSurface
from buddy_desktop.speech import SpeechController
from buddy_desktop.tasks import TaskRunner
from buddy_desktop.transcript_context import ContextSnapshot, TranscriptContext
from buddy_desktop.transition_policy import (
    VERSION as POLICY_VERSION,
)
from buddy_desktop.transition_policy import (
    Prediction,
    TransitionPolicy,
)
from buddy_desktop.transition_policy import (
    Target as PolicyTarget,
)
from buddy_desktop.transition_policy import (
    View as PolicyView,
)
from buddy_desktop.views import Page, StatusLabel
from buddy_desktop.visuals import BuddyMark, command

logger = logging.getLogger(__name__)


def proposal_context(value: dict[str, Any], live: ContextSnapshot) -> ContextSnapshot | None:
    """Bind a result to the snapshot it analyzed while allowing normal continuation."""
    submitted = value.get("_client_context")
    if submitted is None:
        return live
    if not isinstance(submitted, ContextSnapshot) or not submitted.matches_decision(live):
        return None
    return submitted


class SelectionSurface(SlideSurface):
    selection_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.selection: Region | None = None
        self.selectable = False
        self._anchor: QPointF | None = None
        self.setMinimumSize(240, 180)
        self.setAccessibleName("Slide preview; use the region fields for keyboard selection")
        self.empty = QWidget(self)
        empty_layout = QVBoxLayout(self.empty)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.setSpacing(6)
        empty_layout.addWidget(BuddyMark(64), alignment=Qt.AlignmentFlag.AlignCenter)
        empty_layout.addSpacing(8)
        empty_title = QLabel("Open a PDF to start presenting")
        empty_title.setObjectName("sectionTitle")
        empty_layout.addWidget(empty_title, alignment=Qt.AlignmentFlag.AlignCenter)
        empty_hint = QLabel("Or use desktop PowerPoint below to present a .pptx file.")
        empty_hint.setObjectName("muted")
        empty_layout.addWidget(empty_hint, alignment=Qt.AlignmentFlag.AlignCenter)
        layout = QVBoxLayout(self)
        layout.addWidget(self.empty)

    def clear_selection(self) -> None:
        self._anchor = None
        self.selection = None
        self.update()

    def _source_point(self, point: QPointF, *, clamp: bool = False) -> QPointF | None:
        mapping, slide = self.mapping(), self.slide
        if mapping is None or slide is None:
            return None
        x = (point.x() - mapping.offset_x) / mapping.scale
        y = (point.y() - mapping.offset_y) / mapping.scale
        if not clamp and not (0 <= x <= slide.width and 0 <= y <= slide.height):
            return None
        return QPointF(max(0, min(slide.width, x)), max(0, min(slide.height, y)))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self.selectable and event.button() == Qt.MouseButton.LeftButton:
            anchor = self._source_point(event.position())
            if anchor is not None:
                self._anchor = anchor
                self.selection = None
                self.selection_changed.emit(None)
                self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.selectable and self._anchor is not None:
            point = self._source_point(event.position(), clamp=True)
            if point is None:
                return
            x, y = min(self._anchor.x(), point.x()), min(self._anchor.y(), point.y())
            width, height = abs(point.x() - self._anchor.x()), abs(point.y() - self._anchor.y())
            self.selection = Region(x, y, width, height) if width > 0 and height > 0 else None
            self.selection_changed.emit(self.selection)
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.mouseMoveEvent(event)
            self._anchor = None

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        mapping, selection = self.mapping(), self.selection
        if mapping is None or selection is None:
            return
        painter = QPainter(self)
        painter.setPen(QPen(QColor("#B0A69A"), 2, Qt.PenStyle.DashLine))
        painter.drawRect(
            QRectF(
                mapping.point(selection.x, selection.y),
                mapping.point(selection.x + selection.width, selection.y + selection.height),
            )
        )


class PresentWidget(Page):
    """Open local manifests explicitly; no fabricated library or processing integration."""

    session_access_expired = Signal(str)
    session_event_received = Signal(object)
    transition_observed = Signal(object)

    def __init__(self, config: AppConfig, settings: QSettings) -> None:
        super().__init__("Present", "")
        self.config = config
        self.settings = settings
        self.body.setSpacing(12)
        self._closing = False
        self.fullscreen: FullscreenPresentation | None = None
        self._instance = 0
        self._committed_key: tuple[str, str, str] | None = None
        self._account_id: str | None = None
        self._session_access: str | None = None
        self._session: PresentationSession | None = None
        self._session_deck_id: str | None = None
        self._session_attempt: tuple[str, str] | None = None
        self._session_error = False
        self._session_ended = False
        self._session_generation = 0
        self._session_request: tuple[str, int, SessionState | None] | None = None
        self._session_pending_state: SessionState | None = None
        self._session_restore_state: SessionState | None = None
        self._session_restore_action: str | None = None
        self._session_conflict_read = False
        self._restore_conflict = False
        self._restore_focus_applied = False
        self._session_followup: str | None = None
        self.session_service = PresentationSessionService(config)
        self.session_runner = TaskRunner(self)
        self.session_runner.succeeded.connect(self._session_succeeded)
        self.session_runner.failed.connect(self._session_failed)
        self.session_runner.idle.connect(self._session_idle)
        self.session_events = SessionEventStream(self)
        self.session_events.received.connect(self._session_event_received)
        self.session_events.failed.connect(self._session_stream_failed)
        self._stream_snapshot: PresentationSession | None = None
        self.microphone = MicrophoneController(self)
        self.speech = SpeechController(self)
        self.transcript_context = TranscriptContext()
        self.agent = AgentLoop(AgentService(config), self, self.transcript_context.snapshot)
        self.policy = TransitionPolicy()
        self.agent.unavailable.connect(self.policy.clear_pending)
        self.policy_runner = TaskRunner(self)
        self.policy_runner.succeeded.connect(self._policy_checked)
        self.policy_runner.failed.connect(self._policy_failed)
        self._policy_check: tuple[Prediction, ContextSnapshot, str] | None = None
        self._policy_latency: dict[str, dict[str, int]] = {}
        self._deferred_agent_proposal: tuple[dict[str, Any], int] | None = None
        self._deferred_transition_check: tuple[Prediction, ContextSnapshot, str] | None = None
        self._policy_execution: Prediction | None = None
        self._policy_before: SessionState | None = None
        self._policy_undo: tuple[str, str, SessionState] | None = None
        self._policy_undo_restoring = False
        self._pending_native_undo = False
        self._accepted_action_id: str | None = None
        self._agent_session_id: str | None = None
        self._context_connected = False
        self._context_expiry = QTimer(self)
        self._context_expiry.setInterval(1000)
        self._context_expiry.timeout.connect(self.transcript_context.snapshot)
        self._context_expiry.start()
        self.speech.cleared.connect(self.transcript_context.invalidate)
        self.session_events.connection_changed.connect(self._context_connection_changed)
        self.microphone.format_ready.connect(self._speech_start)
        self.microphone.audio_received.connect(self._speech_audio)
        self.microphone.audio_gap.connect(self.speech.audio_gap)
        self._microphone_pdf_generation = 0
        self._microphone_pdf_request_generation = 0
        self._microphone_pdf_poll = QTimer(self)
        self._microphone_pdf_poll.setInterval(5000)
        self._microphone_pdf_poll.timeout.connect(self._verify_microphone_pdf_session)
        self._microphone_pdf_deadline = QTimer(self)
        self._microphone_pdf_deadline.setSingleShot(True)
        self._microphone_pdf_deadline.setInterval(15000)
        self._microphone_pdf_deadline.timeout.connect(self._microphone_pdf_unverified)
        self.powerpoint = PowerPointAutomation()
        self.powerpoint_runner = TaskRunner(self)
        self.powerpoint_runner.succeeded.connect(self._powerpoint_succeeded)
        self.powerpoint_runner.failed.connect(self._powerpoint_failed)
        self.powerpoint_runner.idle.connect(self._powerpoint_idle)
        self._powerpoint_pending: dict[int, tuple[str, str | None]] = {}
        self._queued_powerpoint_action: PowerPointAction | None = None
        self._queued_powerpoint_preview: PresentationInfo | None = None
        self._queued_powerpoint_operation: tuple[str, str | None] | None = None
        self._powerpoint_path: str | None = None
        self._powerpoint_info: PresentationInfo | None = None
        self._powerpoint_binding: PowerPointSessionBinding | None = None
        self._native_present: PowerPointPresent | None = None
        self._native_guard = Event()
        self._native_policy_context: ContextSnapshot | None = None
        self._generated_analysis: ReviewAnalysis | None = None
        self._generated_request: tuple[str, int, str, str | None] | None = None
        self.generated_runner = TaskRunner(self)
        self.generated_runner.succeeded.connect(self._generated_loaded)
        self.generated_runner.failed.connect(self._generated_failed)
        self._native_completion: (
            tuple[Prediction | None, SessionState | None, str | None] | None
        ) = None
        self._powerpoint_verified = False
        self._powerpoint_poll = QTimer(self)
        self._powerpoint_poll.setInterval(500)
        self._powerpoint_poll.timeout.connect(self._poll_powerpoint)
        self._powerpoint_verification = QTimer(self)
        self._powerpoint_verification.setSingleShot(True)
        self._powerpoint_verification.setInterval(5000)
        self._powerpoint_verification.timeout.connect(self._powerpoint_verification_expired)
        self.surface = SelectionSurface()
        self.executor = SlideExecutor(self.surface)
        self.executor.completed.connect(self._completed)
        self.executor.idle.connect(self._refresh)
        self.executor.pdf_warmup_progress.connect(self._pdf_warmup_progress)
        self.executor.pdf_warmup_finished.connect(self._pdf_warmup_finished)
        self._pdf_warming = False
        self.open_button = QPushButton("Open PDF…")
        command(self.open_button, "folder-open", primary=True)
        self.open_button.clicked.connect(self._choose_manifest)
        self.previous_button = QPushButton("Previous")
        command(self.previous_button, "chevron-left", compact=True)
        self.previous_button.clicked.connect(lambda: self.send("previous_slide"))
        self.next_button = QPushButton("Next")
        command(self.next_button, "chevron-right", compact=True)
        self.next_button.clicked.connect(lambda: self.send("next_slide"))
        self.target = QComboBox()
        self.target.setAccessibleName("Slide to go to")
        self.target.setToolTip("Choose a slide to jump to")
        self.target.setMinimumWidth(116)
        self.target.setPlaceholderText("Go to slide")
        self.go_button = QPushButton("Go to slide")
        command(self.go_button, "arrow-right", compact=True)
        self.go_button.clicked.connect(self._go)
        self.counter = QLabel("No slides loaded")
        self.counter.setObjectName("muted")
        self.counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.counter.setMinimumWidth(96)
        opening = QHBoxLayout()
        opening.setSpacing(8)
        opening.addWidget(self.open_button)
        opening.addStretch()
        opening.addWidget(self.target)
        opening.addWidget(self.go_button)
        opening.addSpacing(8)
        self.fullscreen_button = QPushButton("Full screen")
        command(self.fullscreen_button, "scan")
        self.fullscreen_button.setToolTip("F11: full screen. Esc: exit. Arrows or Space: navigate.")
        self.fullscreen_button.clicked.connect(self.enter_fullscreen)
        opening.addWidget(self.fullscreen_button)
        self.display_button = QPushButton()
        command(self.display_button, "presentation", compact=True)
        self.display_button.setAccessibleName("Choose audience display")
        self.display_button.setToolTip("Choose which monitor shows the presentation")
        self.display_button.setMaximumWidth(44)
        self.display_menu = QMenu(self.display_button)
        self.display_button.setMenu(self.display_menu)
        opening.addWidget(self.display_button)
        self.display_selector = QComboBox()
        self.display_selector.setAccessibleName("Audience display")
        self.display_selector.currentIndexChanged.connect(self._display_changed)
        self.display_selector.hide()
        self.body.addLayout(opening)
        self.body.addWidget(self.surface, 1)
        self.surface.setToolTip("Drag to select a region, or use Coordinates for exact values")
        self.coordinates_button = QPushButton("Coordinates")
        command(self.coordinates_button, "settings-2", compact=True)
        self.coordinates_button.setCheckable(True)
        self.coordinates_button.setAccessibleName("Show region coordinates")
        self.coordinates_button.setToolTip("Enter selection coordinates")
        self.coordinate_panel = QWidget()
        form = QGridLayout(self.coordinate_panel)
        form.setContentsMargins(0, 0, 0, 0)
        self.fields: dict[str, QDoubleSpinBox] = {}
        for column, (name, label) in enumerate(
            (
                ("x", "&Left"),
                ("y", "&Top"),
                ("width", "&Width"),
                ("height", "&Height"),
            )
        ):
            field = QDoubleSpinBox()
            field.setDecimals(2)
            field.setRange(0, 16384)
            field.setAccessibleName(label.replace("&", "") + " in slide coordinates")
            field.valueChanged.connect(self._fields_changed)
            caption = QLabel(label)
            caption.setBuddy(field)
            row, pair = divmod(column, 2)
            form.addWidget(caption, row, pair * 2)
            form.addWidget(field, row, pair * 2 + 1)
            self.fields[name] = field
        self.coordinate_panel.hide()
        self.coordinates_button.toggled.connect(self.coordinate_panel.setVisible)
        self.pdf_controls = QWidget()
        pdf_actions = QVBoxLayout(self.pdf_controls)
        pdf_actions.setContentsMargins(0, 0, 0, 0)
        pdf_actions.setSpacing(8)
        commands = QHBoxLayout()
        commands.setSpacing(8)
        commands.addWidget(self.previous_button)
        commands.addWidget(self.counter)
        commands.addWidget(self.next_button)
        commands.addStretch()
        self.zoom_button = QPushButton("Zoom")
        self.zoom_button.setAccessibleName("Zoom to selection")
        command(self.zoom_button, "search")
        self.zoom_button.setToolTip("Zoom to the selected area (Z in full screen)")
        self.zoom_button.clicked.connect(lambda: self._region_action("zoom_region"))
        self.highlight_button = QPushButton("Highlight")
        self.highlight_button.setAccessibleName("Highlight selection")
        command(self.highlight_button, "highlighter")
        self.highlight_button.setToolTip("Highlight the selected area (H in full screen)")
        self.highlight_button.clicked.connect(lambda: self._region_action("highlight_region"))
        self.reset_button = QPushButton("Reset view")
        command(self.reset_button, "rotate-ccw", compact=True)
        self.reset_button.clicked.connect(lambda: self.send("reset_view"))
        for button in (self.zoom_button, self.highlight_button, self.reset_button):
            commands.addWidget(button)
        commands.addWidget(self.coordinates_button)
        pdf_actions.addLayout(commands)
        self.pdf_warmup_status = QLabel()
        self.pdf_warmup_status.setObjectName("muted")
        self.pdf_warmup_status.setAccessibleName("PDF page preparation status")
        self.pdf_warmup_status.setWordWrap(True)
        self.pdf_warmup_status.hide()
        pdf_actions.addWidget(self.pdf_warmup_status)
        self.pdf_warmup_progress_bar = QProgressBar()
        self.pdf_warmup_progress_bar.setObjectName("uploadProgress")
        self.pdf_warmup_progress_bar.setAccessibleName("PDF pages prepared")
        self.pdf_warmup_progress_bar.setTextVisible(False)
        self.pdf_warmup_progress_bar.setRange(0, 1)
        self.pdf_warmup_progress_bar.setValue(0)
        self.pdf_warmup_progress_bar.hide()
        pdf_actions.addWidget(self.pdf_warmup_progress_bar)
        pdf_actions.addWidget(self.coordinate_panel)
        session_row = QHBoxLayout()
        self.session_status = StatusLabel()
        self.session_status.setObjectName("caption")
        self.session_status.setTextFormat(Qt.TextFormat.PlainText)
        self.session_status.setWordWrap(True)
        self.session_status.setAccessibleName("Saved presentation session status")
        session_row.addWidget(self.session_status, 1)
        self.session_retry_button = QPushButton("Retry sync")
        self.session_retry_button.clicked.connect(self._retry_session)
        self.session_retry_button.hide()
        session_row.addWidget(self.session_retry_button)
        self.session_button = QPushButton("End session")
        self.session_button.clicked.connect(self._session_button_clicked)
        session_row.addWidget(self.session_button)
        pdf_actions.addLayout(session_row)
        self.body.addWidget(self.pdf_controls)
        self.microphone_panel = QWidget()
        microphone_layout = QVBoxLayout(self.microphone_panel)
        microphone_layout.setContentsMargins(0, 0, 0, 0)
        microphone_layout.setSpacing(8)
        microphone_row = QHBoxLayout()
        self.microphone_device = QComboBox()
        self.microphone_device.setAccessibleName("Microphone input device")
        self.microphone_device.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.microphone_device.setMinimumContentsLength(8)
        self.microphone_device.currentIndexChanged.connect(self._microphone_device_changed)
        microphone_row.addWidget(self.microphone_device, 1)
        self.microphone_button = QPushButton("Start microphone")
        self.microphone_button.setAccessibleName("Start microphone")
        self.microphone_button.setToolTip("Start or stop microphone capture")
        self.microphone_button.clicked.connect(self._microphone_toggle)
        microphone_row.addWidget(self.microphone_button)
        microphone_layout.addLayout(microphone_row)
        self.microphone_compatibility = QCheckBox("Use microphone compatibility mode")
        self.microphone_compatibility.setAccessibleName("Windows microphone compatibility mode")
        self.microphone_compatibility.setToolTip(
            "Use this if normal recording fails. While listening, "
            "Buddy may need the selected microphone to itself."
        )
        self.microphone_compatibility.setVisible(sys.platform == "win32")
        self.microphone_compatibility.setChecked(
            sys.platform == "win32"
            and bool(settings.value("microphone/compatibility", False, type=bool))
        )
        self.microphone.set_compatibility(self.microphone_compatibility.isChecked())
        self.microphone_compatibility.toggled.connect(self._microphone_compatibility_changed)
        microphone_layout.addWidget(self.microphone_compatibility)
        microphone_controls = QHBoxLayout()
        self.microphone_mute = QPushButton("Mute")
        self.microphone_mute.setAccessibleName("Mute microphone")
        self.microphone_mute.setToolTip("Mute or unmute microphone capture")
        self.microphone_mute.setCheckable(True)
        self.microphone_mute.toggled.connect(self._microphone_mute_changed)
        microphone_controls.addWidget(self.microphone_mute)
        self.microphone_pause = QPushButton("Pause")
        self.microphone_pause.setAccessibleName("Pause microphone")
        self.microphone_pause.setToolTip("Pause or resume microphone capture")
        self.microphone_pause.setCheckable(True)
        self.microphone_pause.toggled.connect(self._microphone_pause_changed)
        microphone_controls.addWidget(self.microphone_pause)
        microphone_controls.addStretch()
        microphone_layout.addLayout(microphone_controls)
        self.microphone_status = StatusLabel()
        self.microphone_status.setAccessibleName("Microphone status")
        self.microphone_status.setText("Microphone off")
        self.microphone_status.setWordWrap(True)
        microphone_layout.addWidget(self.microphone_status)
        self.speech_language = QComboBox()
        self.speech_language.setAccessibleName("Speech recognition language")
        for label, code in (
            ("English (US)", "en-US"),
            ("English (UK)", "en-GB"),
            ("Arabic", "ar"),
            ("French", "fr"),
            ("German", "de"),
            ("Spanish", "es"),
        ):
            self.speech_language.addItem(label, code)
        self.speech_language.currentIndexChanged.connect(lambda _index: self.microphone.stop())
        microphone_layout.addWidget(self.speech_language)
        self.speech_status = QLabel(
            "Saved PDF and PowerPoint sessions: audio is sent to Deepgram (EU). "
            "Transcript text is available for session sync for five minutes."
        )
        self.speech_status.setTextFormat(Qt.TextFormat.PlainText)
        self.speech_status.setWordWrap(True)
        self.speech_status.setAccessibleName("Speech recognition status and privacy")
        microphone_layout.addWidget(self.speech_status)
        self.speech_text = QPlainTextEdit()
        self.speech_text.setReadOnly(True)
        self.speech_text.setMaximumHeight(88)
        self.speech_text.setPlaceholderText("The latest partial or final transcript appears here.")
        self.speech_text.setAccessibleName("Latest speech transcript")
        microphone_layout.addWidget(self.speech_text)
        self.speech.status_changed.connect(self.speech_status.setText)
        self.speech.transcript_received.connect(self._speech_transcript)
        self.speech.cleared.connect(self.speech_text.clear)
        self.microphone.status_changed.connect(self._microphone_status_changed)
        self.microphone.devices_changed.connect(self._microphone_devices_changed)
        self.body.addWidget(self.microphone_panel)
        self.agent_enabled = QCheckBox("Live suggestions")
        self.agent_enabled.setAccessibleName("Enable live presentation suggestions")
        self.agent_enabled.toggled.connect(self.agent.set_enabled)
        self.agent_enabled.toggled.connect(self._suggestions_toggled)
        microphone_layout.addWidget(self.agent_enabled)
        agent_privacy = QLabel(
            "When enabled, recognized speech and slide summaries are sent to Cohere. "
            "Suggestions leave slide changes manual unless you enable automatic transitions."
        )
        agent_privacy.setWordWrap(True)
        agent_privacy.setObjectName("muted")
        microphone_layout.addWidget(agent_privacy)
        self.agent_status = QLabel("Suggestions off")
        self.agent_status.setWordWrap(True)
        self.agent_status.setTextFormat(Qt.TextFormat.PlainText)
        self.agent_status.setAccessibleName("Live suggestion status")
        microphone_layout.addWidget(self.agent_status)
        self.agent_proposal = QLabel("")
        self.agent_proposal.setWordWrap(True)
        self.agent_proposal.setTextFormat(Qt.TextFormat.PlainText)
        self.agent_proposal.setAccessibleName("Latest presentation suggestion")
        microphone_layout.addWidget(self.agent_proposal)
        self.agent.status.connect(self.agent_status.setText)
        self.agent.proposed.connect(self._agent_proposed)
        policy_row = QHBoxLayout()
        self.policy_enabled = QCheckBox("Automatic transitions")
        self.policy_enabled.setAccessibleName("Allow automatic presentation transitions")
        self.policy_enabled.setToolTip(
            "Uses clear speech to run one eligible action from the current presentation. "
            "Manual controls always take priority."
        )
        self.policy_enabled.toggled.connect(self._policy_toggled)
        microphone_layout.addWidget(self.policy_enabled)
        self.policy_pause_button = QPushButton("Resume automatic moves")
        self.policy_pause_button.setAccessibleName("Resume automatic moves")
        self.policy_pause_button.setToolTip("Resume automatic moves (Ctrl+Shift+P)")
        self.policy_pause_button.setShortcut("Ctrl+Shift+P")
        self.policy_pause_button.setEnabled(False)
        self.policy_pause_button.clicked.connect(self._toggle_policy_pause)
        microphone_layout.addWidget(self.policy_pause_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.policy_undo_button = QPushButton("Undo automatic move")
        command(self.policy_undo_button, "rotate-ccw")
        self.policy_undo_button.setShortcut("Ctrl+Shift+Z")
        self.policy_undo_button.setEnabled(False)
        self.policy_undo_button.clicked.connect(self._undo_policy_move)
        policy_row.addWidget(self.policy_undo_button)
        policy_row.addStretch()
        microphone_layout.addLayout(policy_row)
        self.policy_status = StatusLabel("Automatic transitions off")
        self.policy_status.setWordWrap(True)
        self.policy_status.setTextFormat(Qt.TextFormat.PlainText)
        self.policy_status.setAccessibleName("Automatic transition status")
        microphone_layout.addWidget(self.policy_status)
        self.generated_actions = QComboBox()
        self.generated_actions.setAccessibleName("Saved analysis actions for the current slide")
        microphone_layout.addWidget(self.generated_actions)
        generated_row = QHBoxLayout()
        self.generated_reload = QPushButton("Load saved actions")
        self.generated_reload.setAccessibleName("Load saved analysis actions")
        self.generated_reload.setToolTip("Load eligible saved actions for the current slide")
        self.generated_reload.clicked.connect(lambda: self._load_generated(None))
        self.generated_apply = QPushButton("Apply saved action")
        self.generated_apply.setAccessibleName("Apply selected saved action")
        self.generated_apply.setToolTip(
            "Apply the selected validated action to the current presentation"
        )
        self.generated_apply.clicked.connect(self._apply_generated)
        self.generated_actions.currentIndexChanged.connect(lambda _: self._refresh_generated())
        generated_row.addWidget(self.generated_reload)
        generated_row.addWidget(self.generated_apply)
        microphone_layout.addLayout(generated_row)
        self.generated_status = StatusLabel("Load current analysis to choose an action.")
        self.generated_status.setWordWrap(True)
        self.generated_status.setTextFormat(Qt.TextFormat.PlainText)
        microphone_layout.addWidget(self.generated_status)
        self._agent_tick = QTimer(self)
        self._agent_tick.setInterval(100)
        self._agent_tick.timeout.connect(self._update_agent)
        self._agent_tick.start()
        self.powerpoint_open_button = QPushButton("Open PowerPoint…")
        command(self.powerpoint_open_button, "presentation")
        self.powerpoint_open_button.setAccessibleName("Open a PowerPoint presentation")
        self.powerpoint_open_button.clicked.connect(self._choose_powerpoint)
        self.powerpoint_connect_button = QPushButton("Connect to slideshow…")
        command(self.powerpoint_connect_button, "link-2")
        self.powerpoint_connect_button.clicked.connect(self._list_powerpoint_slideshows)
        self.powerpoint_disconnect_button = QPushButton("Disconnect")
        self.powerpoint_disconnect_button.setAccessibleName("Disconnect from PowerPoint")
        command(self.powerpoint_disconnect_button, "x", compact=True)
        self.powerpoint_disconnect_button.clicked.connect(self._disconnect_powerpoint)
        self.powerpoint_panel = QWidget()
        self.powerpoint_panel.setObjectName("group")
        self.powerpoint_panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.powerpoint_panel.setAccessibleName("Desktop PowerPoint")
        powerpoint_layout = QVBoxLayout(self.powerpoint_panel)
        powerpoint_layout.setContentsMargins(18, 16, 18, 16)
        powerpoint_layout.setSpacing(10)
        powerpoint_title = QLabel("Desktop PowerPoint")
        powerpoint_title.setObjectName("sectionTitle")
        powerpoint_layout.addWidget(powerpoint_title)
        powerpoint_row = QHBoxLayout()
        powerpoint_row.setSpacing(8)
        powerpoint_row.addWidget(self.powerpoint_open_button)
        powerpoint_row.addWidget(self.powerpoint_connect_button)
        powerpoint_row.addWidget(self.powerpoint_disconnect_button)
        powerpoint_row.addStretch()
        powerpoint_layout.addLayout(powerpoint_row)
        self.powerpoint_status = StatusLabel()
        self.powerpoint_status.setObjectName("caption")
        self.powerpoint_status.setTextFormat(Qt.TextFormat.PlainText)
        self.powerpoint_status.setWordWrap(True)
        self.powerpoint_status.setAccessibleName("PowerPoint connection status")
        if sys.platform == "win32":
            self.powerpoint_status.setText(
                "PowerPoint is disconnected. Open a .pptx here or connect to a running slideshow."
            )
        else:
            self.powerpoint_status.setText(
                "Desktop PowerPoint control is available on Windows only."
            )
            self.powerpoint_open_button.setEnabled(False)
            self.powerpoint_connect_button.setEnabled(False)
        powerpoint_layout.addWidget(self.powerpoint_status)
        self.powerpoint_controls = PowerPointControls()
        self.powerpoint_controls.action_requested.connect(self._queue_powerpoint_action)
        self.powerpoint_controls.preview_requested.connect(self._queue_powerpoint_preview)
        powerpoint_layout.addWidget(self.powerpoint_controls)
        self.body.addWidget(self.powerpoint_panel)
        self.feedback = StatusLabel()
        self.feedback.setObjectName("caption")
        self.feedback.setWordWrap(True)
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setAccessibleName("Presentation status")
        self.feedback.setText("")
        self.body.addWidget(self.feedback)
        self.surface.selection_changed.connect(self._selection_changed)
        application = cast(QGuiApplication | None, QGuiApplication.instance())
        if application is not None:
            application.screenAdded.connect(self._refresh_displays)
            application.screenRemoved.connect(self._screen_removed)
        self._refresh_displays()
        self._microphone_devices_changed()
        self._refresh()

    def configure(self, config: AppConfig) -> None:
        self.microphone.stop()
        self.config = config
        self.session_service = PresentationSessionService(config)

    def set_account_identity(self, account_id: str | None, access: str | None) -> None:
        if account_id != self._account_id:
            self.invalidate_saved_powerpoint()
        if account_id != self._account_id or not access:
            self.microphone.stop()
        if account_id != self._account_id:
            self._session_generation += 1
            self._session = None
            self._session_pending_state = None
            self._session_restore_state = None
            self._session_restore_action = None
            self._session_attempt = None
            self._session_error = False
            self._session_ended = False
            self._session_deck_id = None
        self._account_id = account_id
        self._session_access = access if account_id and access else None
        if account_id and access:
            self._ensure_session()
        else:
            self.session_status.setText(
                "Sign in to sync this presentation's position."
                if self._account_id is None
                and self.executor.manifest is not None
                and self.executor.manifest.account_bound
                else ""
            )
        self._refresh()

    @Slot()
    def _microphone_devices_changed(self) -> None:
        current = self.microphone_device.currentData()
        self.microphone_device.blockSignals(True)
        self.microphone_device.clear()
        for device in self.microphone.inputs():
            self.microphone_device.addItem(device.description(), bytes(device.id().data()))
        index = self.microphone_device.findData(current)
        if index >= 0:
            self.microphone_device.setCurrentIndex(index)
        self.microphone_device.blockSignals(False)
        self._refresh()

    @Slot(int)
    def _microphone_device_changed(self, _index: int) -> None:
        if self.microphone.enabled:
            self.microphone.stop()

    @Slot()
    def _microphone_toggle(self) -> None:
        if self.microphone.enabled:
            self.microphone.stop()
        elif self._microphone_session_available():
            identifier = self.microphone_device.currentData()
            if isinstance(identifier, bytes):
                self.microphone.start(identifier)
        self._refresh()

    @Slot(bool)
    def _microphone_compatibility_changed(self, enabled: bool) -> None:
        self.microphone.set_compatibility(enabled)
        self.settings.setValue("microphone/compatibility", enabled)
        self._refresh()

    @Slot(bool)
    def _microphone_mute_changed(self, muted: bool) -> None:
        self.microphone_mute.setText("Unmute" if muted else "Mute")
        self.microphone_mute.setAccessibleName("Unmute microphone" if muted else "Mute microphone")
        self.microphone.set_muted(muted)

    @Slot(bool)
    def _microphone_pause_changed(self, paused: bool) -> None:
        self.microphone_pause.setText("Resume" if paused else "Pause")
        self.microphone_pause.setAccessibleName(
            "Resume microphone" if paused else "Pause microphone"
        )
        self.microphone.set_paused(paused)

    @Slot(str)
    def _microphone_status_changed(self, message: str) -> None:
        self.microphone_status.setText(message)
        if message != "Microphone on":
            self.speech.stop()
            self.speech_status.setText(
                "Transcription off. Saved presentation audio is sent to Deepgram (EU) "
                "and transcript text is available for session sync for five minutes."
            )
        if message == "Microphone on" and (
            self._powerpoint_info is None or self._powerpoint_binding is not None
        ):
            self._microphone_pdf_generation += 1
            self._microphone_pdf_poll.start()
            self._microphone_pdf_deadline.start()
        elif not self.microphone.enabled:
            self._microphone_pdf_generation += 1
            self._microphone_pdf_poll.stop()
            self._microphone_pdf_deadline.stop()
        if not self.microphone.enabled:
            self.microphone_mute.setChecked(False)
            self.microphone_pause.setChecked(False)
        self._refresh()

    @Slot(int)
    def _speech_start(self, sample_rate: int) -> None:
        if (
            not self._closing
            and self.microphone.enabled
            and (self._powerpoint_info is None or self._powerpoint_binding is not None)
            and (self._powerpoint_info is None or self._powerpoint_verified)
            and self._session is not None
            and self._session.status == "active"
            and self._session_access
            and self._session_pending_state is None
            and (
                self._powerpoint_binding is None
                or (
                    self._powerpoint_verified and self._session.state == self._current_local_state()
                )
            )
        ):
            self.speech.start(
                self.config,
                self._session_access,
                self._session.id,
                sample_rate,
                str(self.speech_language.currentData()),
            )
        elif self._powerpoint_info is not None:
            self.speech_status.setText(
                "Open a saved PowerPoint from Review and start its synced session "
                "before using transcription."
            )

    @Slot(int)
    def _speech_audio(self, _size: int) -> None:
        # Drain even when speech is unavailable: never accumulate/replay local capture.
        while True:
            try:
                packet = self.microphone.frames.get_nowait()
            except Empty:
                return
            if self.microphone.enabled:
                self.speech.send(packet)

    @Slot(int, str)
    def _context_connection_changed(self, generation: int, state: str) -> None:
        if generation == self.session_events.generation:
            self._context_connected = state == "connected"
            self._invalidate_transcript_context()

    @Slot()
    def _update_agent(self) -> None:
        session = self._session if isinstance(self._session, PresentationSession) else None
        identifier = session.id if session is not None else None
        if identifier != self._agent_session_id:
            self._agent_session_id = identifier
            self.agent_enabled.setChecked(False)
            self._policy_undo = None
            self._accepted_action_id = None
            self.policy_undo_button.setEnabled(False)
        connected = (
            not self._closing
            and session is not None
            and session.status == "active"
            and self._session_access is not None
            and (
                self._powerpoint_info is None
                or (self._powerpoint_binding is not None and self._powerpoint_verified)
            )
        )
        synced = (
            connected
            and session is not None
            and self._session_pending_state is None
            and (self._powerpoint_binding is None or session.state == self._current_local_state())
        )
        self.agent_enabled.setEnabled(connected)
        # A temporary connection/verification gap pauses proposals, but it must not
        # silently revoke the presenter's opt-in. Session identity changes below reset
        # consent for a genuinely new presentation.
        if self._powerpoint_info is not None and self._powerpoint_binding is None:
            # A locally opened Office show has no saved, owner-checked session contract.
            self.policy_enabled.setChecked(False)
        self.policy_enabled.setEnabled(
            (synced or self.policy.enabled) and self.agent_enabled.isChecked()
        )
        if not self.agent_enabled.isChecked():
            self.policy_enabled.setChecked(False)
        elif self.policy.enabled and not synced:
            self.policy_status.setText(
                "Automatic actions on; waiting for the presentation to sync."
            )
        self.policy_pause_button.setEnabled(self.policy.enabled or self.policy_enabled.isEnabled())
        self._retry_deferred_policy()
        context = self.transcript_context.snapshot()
        self.agent.update(
            self._session_access if synced else None,
            session,
            context if synced else replace(context, fresh=False),
        )

    @Slot(bool)
    def _suggestions_toggled(self, enabled: bool) -> None:
        if not enabled and hasattr(self, "policy_enabled"):
            self.policy_enabled.setChecked(False)

    @Slot(bool)
    def _policy_toggled(self, enabled: bool) -> None:
        label = "Pause automatic moves" if enabled else "Resume automatic moves"
        self.policy_pause_button.setText(label)
        self.policy_pause_button.setAccessibleName(label)
        self.policy_pause_button.setToolTip(f"{label} (Ctrl+Shift+P)")
        self._native_guard.clear()
        self._native_policy_context = None
        self.policy.set_enabled(enabled)
        self._policy_check = None
        self._deferred_agent_proposal = None
        self._deferred_transition_check = None
        self._invalidate_transcript_context()
        if not enabled and self._policy_execution is not None:
            self.executor.cancel()
        self.policy_status.setText(
            "Automatic actions on. Clear navigation and eligible speech-matched actions can run; "
            "ambiguous speech is skipped."
            if enabled
            else "Automatic transitions paused. Manual controls are available."
        )
        self._queue_session_state()

    @Slot()
    def _toggle_policy_pause(self) -> None:
        if self.policy.enabled:
            self.policy_enabled.setChecked(False)
        elif self.policy_enabled.isEnabled():
            self.policy_enabled.setChecked(True)

    def _policy_command(self, target: PolicyTarget) -> dict[str, object]:
        manifest, slide = self.executor.manifest, self.surface.slide
        if manifest is None or slide is None:
            raise ValueError
        index = manifest.index(target.slide_id)
        if target.tool in {"next_slide", "previous_slide"}:
            if index - self.executor.current_index != (1 if target.tool == "next_slide" else -1):
                raise ValueError
        elif target.tool != "go_to_slide" and target.slide_id != slide.slide_id:
            raise ValueError
        params: dict[str, object] = {}
        if target.tool == "go_to_slide":
            params["target_slide_id"] = target.slide_id
        elif target.region is not None:
            params["region"] = dict(zip(("x", "y", "width", "height"), target.region, strict=True))
        command_value: dict[str, object] = {
            "version": "1.1",
            "instance_id": "policy:check",
            "purpose": "Accepted live transition.",
            "deck_id": manifest.deck_id,
            "render_id": manifest.render_id,
            "slide_id": slide.slide_id,
            "type": target.tool,
            "params": params,
        }
        parse_action(command_value, manifest, self.executor.current_index)
        return command_value

    def _invalidate_transcript_context(self) -> None:
        if hasattr(self, "_native_guard"):
            self._native_guard.clear()
            self._native_policy_context = None
        self.transcript_context.invalidate()

    def _native_focus_view(self) -> str:
        focus = self._native_present.focus if self._native_present else None
        return (
            focus.tool
            if focus is not None and focus.tool in {"zoom_region", "highlight_region"}
            else "slide"
        )

    def _native_focus_region(self) -> dict[str, float] | None:
        focus = self._native_present.focus if self._native_present else None
        return (
            dict(zip(("x", "y", "width", "height"), focus.region, strict=True))
            if focus is not None and focus.region is not None
            else None
        )

    def _native_manual_intent(self) -> None:
        self._pending_native_undo = False
        if not hasattr(self, "policy"):
            return
        self._native_guard.clear()
        self._native_policy_context = None
        prediction = (
            self._policy_check[0]
            if self._policy_check is not None
            else self._native_completion[0]
            if self._native_completion is not None
            else None
        )
        if prediction is not None:
            self._policy_latency.pop(prediction.decision_id, None)
            self.policy.completed(prediction.decision_id, success=False, now_ns=time.monotonic_ns())
        self.policy.reset(manual=True, now_ns=time.monotonic_ns())
        self._policy_check = None
        self._policy_undo = None
        self._accepted_action_id = None
        self.policy_undo_button.setEnabled(False)
        self._invalidate_transcript_context()

    def _start_policy_latency(
        self, prediction: Prediction, context: ContextSnapshot, proposal_received_ns: int
    ) -> None:
        segments = context.decision_segments
        if not segments:
            return
        self._policy_latency[prediction.decision_id] = {
            "capture_end_ns": segments[-1].capture_end_ns,
            "proposal_received_ns": proposal_received_ns,
            "preflight_started_ns": time.monotonic_ns(),
        }
        while len(self._policy_latency) > 32:
            self._policy_latency.pop(next(iter(self._policy_latency)))

    def _policy_stage(self, prediction: Prediction, name: str, stamp_ns: int) -> None:
        timing = self._policy_latency.get(prediction.decision_id)
        if timing is not None:
            timing[name] = stamp_ns

    def _log_policy_preflight(self, prediction: Prediction, source_format: str) -> None:
        timing = self._policy_latency.get(prediction.decision_id)
        if timing is None:
            return
        execution_ns = timing.get("execution_started_ns", time.monotonic_ns())
        logger.info(
            "presentation_latency event=preflight_completed decision_id=%s "
            "source_format=%s proposal_to_preflight_ms=%d preflight_ms=%d",
            prediction.decision_id,
            source_format,
            max(0, (timing["preflight_started_ns"] - timing["proposal_received_ns"]) // 1_000_000),
            max(0, (execution_ns - timing["preflight_started_ns"]) // 1_000_000),
        )

    def _log_policy_completion(
        self,
        prediction: Prediction,
        context: ContextSnapshot | None,
        source_format: str,
        completed_ns: int,
        outcome: str,
    ) -> None:
        timing = self._policy_latency.pop(prediction.decision_id, None)
        segments = context.decision_segments if context is not None else ()
        capture_end_ns = (timing.get("capture_end_ns") if timing is not None else None) or (
            segments[-1].capture_end_ns if segments else None
        )
        if timing is None or capture_end_ns is None:
            return
        logger.info(
            "presentation_latency event=action_finished decision_id=%s "
            "source_format=%s tool=%s outcome=%s capture_to_action_ms=%d proposal_to_action_ms=%d "
            "preflight_ms=%d executor_ms=%d",
            prediction.decision_id,
            source_format,
            prediction.target.tool,
            outcome,
            max(0, (completed_ns - capture_end_ns) // 1_000_000),
            max(0, (completed_ns - timing["proposal_received_ns"]) // 1_000_000),
            max(
                0,
                (timing.get("execution_started_ns", completed_ns) - timing["preflight_started_ns"])
                // 1_000_000,
            ),
            max(
                0,
                (completed_ns - timing.get("execution_started_ns", completed_ns)) // 1_000_000,
            ),
        )

    def _consider_native_policy(self, value: dict[str, Any]) -> None:
        proposal_received_ns = time.monotonic_ns()
        native, session, info = self._native_present, self._session, self._powerpoint_info
        if not self.policy.enabled or native is None or session is None or info is None:
            return
        context = proposal_context(value, self.transcript_context.snapshot())
        if context is None:
            self.policy_status.setText("Speech changed before the action could run; repeat it.")
            return
        busy_reasons = []
        if self.powerpoint_runner.busy:
            busy_reasons.append("powerpoint_operation")
        if self.session_runner.busy:
            busy_reasons.append("session_sync")
        if self._stream_snapshot is not None:
            busy_reasons.append("session_event")
        if self._session_pending_state is not None:
            busy_reasons.append("pending_session_state")
        if (
            busy_reasons
            and not self.policy_runner.busy
            and self._policy_check is None
            and self._native_completion is None
        ):
            self._deferred_agent_proposal = (value, time.monotonic_ns())
            logger.warning(
                "presentation_policy event=proposal_deferred reason=%s",
                ",".join(busy_reasons),
            )
            self.policy_status.setText(
                "Suggestion found; waiting for PowerPoint/session sync to finish."
            )
            return
        state = self._current_local_state()
        if state is None:
            self.policy.clear_pending()
            return
        try:
            prediction = Prediction.from_validated(value, context)
            eligible: tuple[PolicyTarget, ...] = ()
            if prediction.target.tool != "no_action":
                native.command(prediction.target, info)
                eligible = (prediction.target,)
            reason = self.policy.consider(
                prediction,
                context,
                PolicyView(
                    session.id,
                    session.analysis_version_id,
                    native.binding.source_sha256,
                    "pptx",
                    session.state_version,
                    PolicyTarget.parse(
                        {
                            "tool": state.focus_view,
                            "slide_id": state.active_slide_id,
                            "region": state.focus_region,
                        }
                    ),
                    eligible,
                    busy=self.powerpoint_runner.busy
                    or self.session_runner.busy
                    or self.policy_runner.busy
                    or self._stream_snapshot is not None
                    or self._session_pending_state is not None,
                    connected=self._context_connected
                    and self._powerpoint_verified
                    and self.microphone.enabled
                    and session.state is not None
                    and session.state.autonomy_mode == "autonomous",
                ),
                now_ns=time.monotonic_ns(),
                today=date.today(),
            )
        except (ValueError, KeyError, TypeError, IndexError, PowerPointError):
            self.policy.clear_pending()
            self.policy_status.setText("Suggestion is unavailable for the current Office source.")
            return
        if reason != "accepted":
            self._log_policy_rejection(prediction, context, reason)
        self.policy_status.setText(
            "Checking the current PowerPoint action…"
            if reason == "accepted"
            else self._policy_rejection_message(reason)
        )
        if reason == "accepted" and self._session_access:
            access = self._session_access
            self._policy_check = (prediction, context, native.binding.source_sha256)
            self._start_policy_latency(prediction, context, proposal_received_ns)
            self.policy_runner.submit(
                lambda: self.agent.service.check_transition(access, session, value["target"])
            )

    def _native_policy_checked(
        self, prediction: Prediction, context: ContextSnapshot, source: str
    ) -> None:
        native, session, info = self._native_present, self._session, self._powerpoint_info
        reason = None
        if native is None or session is None or info is None:
            reason = "powerpoint_or_session_missing"
        elif not self.policy.enabled or self._closing:
            reason = "automatic_actions_paused_or_closing"
        elif not self._context_connected or not self._powerpoint_verified:
            reason = "powerpoint_session_disconnected"
        elif not self.microphone.enabled:
            reason = "microphone_stopped"
        elif session.state_version != prediction.state_version:
            reason = "session_state_version_changed"
        elif native.binding.source_sha256 != source:
            reason = "powerpoint_source_changed"
        else:
            reason = context.decision_mismatch_reason(self.transcript_context.snapshot())
            if reason is None and (
                self.powerpoint_runner.busy
                or self.session_runner.busy
                or self._session_pending_state is not None
            ):
                reason = "powerpoint_or_session_sync_in_progress"
        if reason is not None:
            logger.warning(
                "presentation_policy event=execution_check_rejected decision_id=%s "
                "reason=%s state_version_expected=%d state_version_current=%s",
                prediction.decision_id,
                reason,
                prediction.state_version,
                session.state_version if session is not None else "missing",
            )
            if reason == "powerpoint_or_session_sync_in_progress":
                self._deferred_transition_check = (prediction, context, source)
                self.policy_status.setText(
                    "Suggestion found; waiting for PowerPoint/session sync to finish."
                )
                return
            self._policy_latency.pop(prediction.decision_id, None)
            self.policy.completed(prediction.decision_id, success=False, now_ns=time.monotonic_ns())
            self.policy_status.setText(self._policy_rejection_status(reason))
            return
        assert native is not None and session is not None and info is not None
        try:
            command_value = native.command(prediction.target, info)
        except PowerPointError:
            self._policy_latency.pop(prediction.decision_id, None)
            self.policy.completed(prediction.decision_id, success=False, now_ns=time.monotonic_ns())
            self.policy_status.setText("That action is unavailable in the current PowerPoint view.")
            return
        self._native_completion = (
            prediction,
            self._current_local_state(),
            prediction.target.action_id,
        )
        self._native_policy_context = context
        self._policy_stage(prediction, "execution_started_ns", time.monotonic_ns())
        self._log_policy_preflight(prediction, "pptx")
        self._native_guard.set()
        deadline = context.decision_segments[-1].capture_end_ns + 5_000_000_000

        def guard() -> bool:
            return (
                self._native_guard.is_set()
                and self.policy.enabled
                and time.monotonic_ns() <= deadline
                and self._session is not None
                and self._session.id == session.id
                and self._session.state_version == session.state_version
            )

        self._submit_powerpoint_task(
            "policy",
            info.path,
            lambda: native.execute(
                self.powerpoint, command_value, identifier=prediction.target.action_id, guard=guard
            ),
        )

    def _native_completed(self, kind: str, result: PowerPointActionResult) -> None:
        context = self._native_policy_context
        self._native_policy_context = None
        metadata, self._native_completion = self._native_completion, None
        if metadata is None or self._powerpoint_binding is None or self._session is None:
            return
        prediction, before, identifier = metadata
        # Keep completion metadata until attach finishes, so observed policy movement
        # is not mistaken for a new manual Office navigation event.
        self._native_completion = metadata
        self._attach_powerpoint_info(result.info)
        self._native_completion = None
        self._accepted_action_id = identifier
        if prediction is not None:
            completed_ns = time.monotonic_ns()
            self._log_policy_completion(prediction, context, "pptx", completed_ns, "completed")
            self.policy.completed(prediction.decision_id, success=True, now_ns=completed_ns)
            if before is not None:
                self._policy_undo = (
                    self._session.id,
                    self._powerpoint_binding.source_sha256,
                    before,
                )
                self.policy_undo_button.setEnabled(True)
            self.transition_observed.emit(
                {
                    "policy_version": POLICY_VERSION,
                    "source_format": "pptx",
                    "decision_id": prediction.decision_id,
                    "target": {
                        "tool": prediction.target.tool,
                        "slide_id": prediction.target.slide_id,
                        "action_id": prediction.target.action_id,
                        "region": self._native_focus_region(),
                    },
                    "completed_ns": time.monotonic_ns(),
                    "animated": None,
                    "calibration_sha256": self.policy.calibration_sha256,
                    "settings_sha256": self.policy.settings_sha256,
                }
            )
        self._queue_session_state()
        self.generated_status.setText(
            "Native action completed."
            if kind != "undo_policy"
            else "Previous native view restored."
        )
        self._refresh()

    def _load_generated(self, identifier: str | None) -> None:
        session, access, state = self._session, self._session_access, self._current_local_state()
        if (
            self._closing
            or session is None
            or access is None
            or state is None
            or self.generated_runner.busy
        ):
            return
        self._generated_request = (
            session.id,
            session.state_version,
            state.active_slide_id,
            identifier,
        )
        source_format = "pptx" if self._powerpoint_binding else "pdf"
        service = ReviewService(self.config, access)
        self.generated_status.setText("Checking the current saved analysis…")
        self.generated_runner.submit(lambda: service.analysis(UUID(session.deck_id), source_format))
        self._refresh_generated()

    def _generated_failed(self, _request: int, message: str) -> None:
        self._generated_request = None
        self._generated_analysis = None
        self.generated_status.setText(message)
        self._refresh_generated()

    def _generated_loaded(self, _request: int, analysis: object) -> None:
        pending, self._generated_request = self._generated_request, None
        session, state = self._session, self._current_local_state()
        if (
            pending is None
            or session is None
            or state is None
            or self._closing
            or not isinstance(analysis, ReviewAnalysis)
        ):
            return
        session_id, version, slide_id, identifier = pending
        if (
            session.id != session_id
            or session.state_version != version
            or state.active_slide_id != slide_id
            or str(analysis.id) != session.analysis_version_id
            or str(analysis.deck_id) != session.deck_id
        ):
            self._generated_failed(0, "Presentation changed. Reload its saved actions.")
            return
        if self._powerpoint_binding is not None:
            binding = self._powerpoint_binding
            if (
                analysis.source_format != "pptx"
                or analysis.source_sha256 != binding.source_sha256
                or analysis.source_revision != binding.source_revision
                or analysis.render_id != binding.render_id
                or not analysis.office_safe
            ):
                self._generated_failed(0, "The PowerPoint source changed. Reopen it from Library.")
                return
            self._powerpoint_binding = replace(binding, slides=analysis.slides)
            if self._native_present is not None:
                self._native_present.binding = self._powerpoint_binding
        else:
            manifest = self.executor.manifest
            if (
                manifest is None
                or manifest.pdf_content is None
                or analysis.source_format != "pdf"
                or hashlib.sha256(manifest.pdf_content).hexdigest() != analysis.source_sha256
            ):
                self._generated_failed(0, "The PDF source changed. Reopen it from Library.")
                return
        self._generated_analysis = analysis
        self.generated_status.setText(
            "Choose a saved action. Unavailable actions cannot be applied."
        )
        self._refresh_generated()
        if identifier is None:
            return
        slide = next((s for s in analysis.slides if s.slide_id == slide_id), None)
        action = (
            next((a for a in slide.actions if a.instance_id == identifier), None) if slide else None
        )
        if (
            action is None
            or not action.usable
            or action.state != "validated"
            or (analysis.source_format == "pdf" and not action.semantic_verified)
        ):
            self.generated_status.setText("This action is no longer eligible.")
            return
        if self._native_present is not None and self._powerpoint_info is not None:
            if self.powerpoint_runner.busy:
                self.generated_status.setText(
                    "PowerPoint is busy. Apply the action again when ready."
                )
                return
            self._native_manual_intent()
            native, info = self._native_present, self._powerpoint_info
            try:
                command_value = native.saved_command(identifier, info)
            except PowerPointError as exc:
                self.generated_status.setText(str(exc))
                return
            self._native_completion = (None, state, identifier)
            self._native_guard.set()
            self._submit_powerpoint_task(
                "saved_action",
                info.path,
                lambda: native.execute(
                    self.powerpoint,
                    command_value,
                    identifier=identifier,
                    guard=self._native_guard.is_set,
                ),
            )
        else:
            self._accepted_action_id = identifier
            self.send(
                action.action_type,
                {"region": dict(zip(("x", "y", "width", "height"), action.region, strict=True))},
            )
            # send() clears manual accepted state; retain this exact saved identity.
            self._accepted_action_id = identifier
            self._queue_session_state()

    def _apply_generated(self) -> None:
        identifier = self.generated_actions.currentData()
        if isinstance(identifier, str):
            self._load_generated(identifier)

    def _refresh_generated(self) -> None:
        if not hasattr(self, "generated_actions"):
            return
        session = self._session
        state = self._current_local_state()
        analysis = self._generated_analysis
        key = (
            (session.id, session.analysis_version_id, state.active_slide_id, analysis)
            if session
            and state
            and analysis
            and str(analysis.id) == session.analysis_version_id
            and str(analysis.deck_id) == session.deck_id
            else None
        )
        if getattr(self, "_generated_key", None) != key:
            self._generated_key = key
            self.generated_actions.clear()
            if key and analysis is not None and state is not None:
                slide = next(
                    (s for s in analysis.slides if s.slide_id == state.active_slide_id), None
                )
                for action in slide.actions if slide else ():
                    eligible = (
                        action.usable
                        and action.state == "validated"
                        and (analysis.source_format == "pptx" or action.semantic_verified)
                    )
                    label = f"{action.purpose} · {action.action_type}" + (
                        ""
                        if eligible
                        else " · unavailable: " + ", ".join(action.reasons or (action.state,))
                    )
                    self.generated_actions.addItem(label, action.instance_id if eligible else None)
                    if not eligible:
                        item = cast(QStandardItemModel, self.generated_actions.model()).item(
                            self.generated_actions.count() - 1
                        )
                        if item is not None:
                            item.setEnabled(False)
        available = (
            session is not None
            and session.status == "active"
            and self._session_access is not None
            and state is not None
            and not self._closing
        )
        self.generated_reload.setEnabled(available and not self.generated_runner.busy)
        self.generated_apply.setEnabled(
            available
            and not self.generated_runner.busy
            and isinstance(self.generated_actions.currentData(), str)
        )

    def _consider_policy(self, value: dict[str, Any]) -> None:
        proposal_received_ns = time.monotonic_ns()
        self._deferred_agent_proposal = None
        if self._powerpoint_binding is not None:
            self._consider_native_policy(value)
            return
        session, manifest = self._session, self.executor.manifest
        if not self.policy.enabled or session is None or manifest is None:
            return
        context = proposal_context(value, self.transcript_context.snapshot())
        state = self._current_local_state()
        if context is None:
            self.policy_status.setText("Speech changed before the action could run; repeat it.")
            return
        if state is None or self._powerpoint_info is not None:
            return
        busy_reasons = []
        if self.executor.busy:
            busy_reasons.append("slide_executor")
        if self.session_runner.busy:
            busy_reasons.append("session_sync")
        if self._session_pending_state is not None:
            busy_reasons.append("pending_session_state")
        if self._session_restore_state is not None:
            busy_reasons.append("session_restore")
        if self._stream_snapshot is not None:
            busy_reasons.append("session_event")
        if (
            busy_reasons
            and not self.policy_runner.busy
            and self._policy_check is None
            and self._policy_execution is None
        ):
            self._deferred_agent_proposal = (value, time.monotonic_ns())
            logger.warning(
                "presentation_policy event=proposal_deferred reason=%s",
                ",".join(busy_reasons),
            )
            self.policy_status.setText(
                "Suggestion found; waiting for the slide/session sync to finish."
            )
            return
        try:
            prediction = Prediction.from_validated(value, context)
            eligible: tuple[PolicyTarget, ...] = ()
            if prediction.target.tool != "no_action":
                self._policy_command(prediction.target)
                eligible = (prediction.target,)
            reason = self.policy.consider(
                prediction,
                context,
                PolicyView(
                    session.id,
                    session.analysis_version_id,
                    manifest.deck_id + ":" + manifest.render_id,
                    "pdf",
                    session.state_version,
                    PolicyTarget.parse(
                        {
                            "tool": state.focus_view,
                            "slide_id": state.active_slide_id,
                            "region": state.focus_region,
                        }
                    ),
                    eligible,
                    busy=self.executor.busy
                    or self.session_runner.busy
                    or self.policy_runner.busy
                    or self._session_pending_state is not None
                    or self._session_restore_state is not None
                    or self._stream_snapshot is not None,
                    connected=self._context_connected
                    and self.microphone.enabled
                    and session.state is not None
                    and session.state.autonomy_mode == "autonomous",
                ),
                now_ns=time.monotonic_ns(),
                today=date.today(),
            )
        except (ValueError, TypeError, KeyError, IndexError, ContractError):
            self.policy.clear_pending()
            self.policy_status.setText("Suggestion cannot execute in the current view.")
            return
        if reason != "accepted":
            self._log_policy_rejection(prediction, context, reason)
        self.policy_status.setText(
            "Checking the selected action…"
            if reason == "accepted"
            else self._policy_rejection_message(reason)
        )
        if reason != "accepted":
            return
        access = self._session_access
        if not access:
            self._policy_latency.pop(prediction.decision_id, None)
            self._policy_failed(0, "Sign in again before enabling automatic transitions.")
            return
        self._policy_check = (prediction, context, manifest.render_id)
        self._start_policy_latency(prediction, context, proposal_received_ns)
        if prediction.target.tool in {"next_slide", "previous_slide", "go_to_slide"}:
            self.executor.prefetch_slide(prediction.target.slide_id)
        self.policy_runner.submit(
            lambda: self.agent.service.check_transition(access, session, value["target"])
        )
        self.policy_status.setText("Checking the current action before moving…")

    @staticmethod
    def _policy_rejection_message(reason: str) -> str:
        labels = {
            "disabled": "Suggestion found; automatic transitions are off, so it was not run.",
            "unsupported_format": (
                "Suggestion found; automatic transitions do not support this file."
            ),
            "busy": "Suggestion found; a slide action or sync is still running. It was not run.",
            "stale": (
                "Suggestion found, but speech or slide state changed before it could run. "
                "Repeat it."
            ),
            "no_action": "No action was selected; continue presenting manually.",
            "current_focus": "Suggestion found, but that view is already active.",
            "duplicate": "Suggestion found, but this speech segment was already handled.",
            "cooldown": (
                "Suggestion found just after the previous move; wait briefly and repeat it."
            ),
            "weak_evidence": (
                "Suggestion found, but the latest speech did not support running it confidently."
            ),
            "ineligible": (
                "Suggestion found, but that action is unavailable for the current slide."
            ),
        }
        return labels.get(reason, "Suggestion found, but it was not run; continue manually.")

    @staticmethod
    def _log_policy_rejection(
        prediction: Prediction, context: ContextSnapshot, reason: str
    ) -> None:
        segments = context.decision_segments
        request_age_ms = (
            max(0, (time.monotonic_ns() - segments[-1].capture_end_ns) // 1_000_000)
            if segments
            else -1
        )
        logger.warning(
            "presentation_policy event=proposal_rejected decision_id=%s reason=%s "
            "request_age_ms=%d state_version=%d",
            prediction.decision_id,
            reason,
            request_age_ms,
            prediction.state_version,
        )

    @Slot(int, object)
    def _policy_checked(self, _identifier: int, _result: object) -> None:
        pending, self._policy_check = self._policy_check, None
        if pending is None:
            return
        prediction, context, render = pending
        if self._powerpoint_binding is not None:
            self._native_policy_checked(prediction, context, render)
            return
        session, manifest = self._session, self.executor.manifest
        reason = self._policy_check_rejection(prediction, context, render, session, manifest)
        if reason is not None:
            transient = reason in {
                "slide_executor_busy",
                "session_sync_in_progress",
            }
            logger.warning(
                "presentation_policy event=execution_check_rejected decision_id=%s "
                "reason=%s state_version_expected=%d state_version_current=%s",
                prediction.decision_id,
                reason,
                prediction.state_version,
                session.state_version if session is not None else "missing",
            )
            if transient:
                self._deferred_transition_check = (prediction, context, render)
                self.policy_status.setText(
                    "Suggestion found; waiting for the slide/session sync to finish."
                )
                return
            self._policy_latency.pop(prediction.decision_id, None)
            self.policy.completed(prediction.decision_id, success=False, now_ns=time.monotonic_ns())
            self.policy_status.setText(self._policy_rejection_status(reason))
            return
        self._policy_before = self._current_local_state()
        self._policy_execution = prediction
        self._policy_stage(prediction, "execution_started_ns", time.monotonic_ns())
        self._log_policy_preflight(prediction, "pdf")
        command_value = self._policy_command(prediction.target)
        command_value["instance_id"] = "policy:" + prediction.decision_id

        def still_current() -> bool:
            return (
                self.policy.enabled
                and not self._closing
                and self._context_connected
                and self.microphone.enabled
                and self._session is not None
                and self._session.id == prediction.session_id
                and self._session.state_version == prediction.state_version
                and self._session_pending_state is None
                and self._stream_snapshot is None
                and context.matches_decision(self.transcript_context.snapshot())
            )

        self.executor.dispatch(command_value, commit_guard=still_current)

    def _policy_check_rejection(
        self,
        prediction: Prediction,
        context: ContextSnapshot,
        render: str,
        session: PresentationSession | None,
        manifest: Any,
    ) -> str | None:
        if not self.policy.enabled:
            return "automatic_actions_paused"
        if self._closing:
            return "presentation_closing"
        if session is None:
            return "session_missing"
        if manifest is None:
            return "slide_manifest_missing"
        if session.state_version != prediction.state_version:
            return "session_state_version_changed"
        if manifest.render_id != render:
            return "slide_render_changed"
        if not self._context_connected:
            return "session_disconnected"
        if not self.microphone.enabled:
            return "microphone_stopped"
        mismatch = context.decision_mismatch_reason(self.transcript_context.snapshot())
        if mismatch is not None:
            return mismatch
        if self.executor.busy:
            return "slide_executor_busy"
        if (
            self.session_runner.busy
            or self._session_pending_state is not None
            or self._session_restore_state is not None
            or self._stream_snapshot is not None
        ):
            return "session_sync_in_progress"
        return None

    @staticmethod
    def _policy_rejection_status(reason: str) -> str:
        if reason in {"slide_executor_busy", "session_sync_in_progress"}:
            return "Suggestion found; waiting for the slide/session sync to finish."
        if reason in {
            "stale_capture",
            "interim_prefix_corrected",
            "new_speech_corrected_prior_intent",
            "completed_speech_changed",
        }:
            return "Speech changed before the action could run; repeat the instruction."
        if reason in {
            "session_state_version_changed",
            "slide_render_changed",
            "session_disconnected",
        }:
            return "Presentation state changed before the action could run; repeat the instruction."
        if reason == "microphone_stopped":
            return "Microphone stopped before the action could run."
        return "Suggestion could not run in the current presentation state."

    def _retry_deferred_policy(self) -> None:
        deferred_check = self._deferred_transition_check
        deferred_proposal = self._deferred_agent_proposal
        if deferred_check is None and deferred_proposal is None:
            return
        now = time.monotonic_ns()
        if deferred_check is not None:
            prediction, context, render = deferred_check
            if self._powerpoint_binding is not None:
                self._retry_deferred_native_transition(prediction, context, render, now)
                return
            session, manifest = self._session, self.executor.manifest
            if now - context.decision_segments[-1].capture_end_ns > 5_000_000_000:
                self._deferred_transition_check = None
                self.policy.completed(prediction.decision_id, success=False, now_ns=now)
                logger.warning(
                    "presentation_policy event=deferred_action_dropped reason=stale_capture "
                    "decision_id=%s",
                    prediction.decision_id,
                )
                self.policy_status.setText(
                    "Suggestion expired while the slide was syncing; repeat it."
                )
                return
            if not self.policy.enabled or self._closing or not self.microphone.enabled:
                self._deferred_transition_check = None
                self.policy.completed(prediction.decision_id, success=False, now_ns=now)
                return
            mismatch = context.decision_mismatch_reason(self.transcript_context.snapshot())
            if mismatch is not None:
                self._deferred_transition_check = None
                self.policy.completed(prediction.decision_id, success=False, now_ns=now)
                logger.warning(
                    "presentation_policy event=deferred_action_dropped reason=%s decision_id=%s",
                    mismatch,
                    prediction.decision_id,
                )
                self.policy_status.setText(self._policy_rejection_status(mismatch))
                return
            reason = self._policy_check_rejection(prediction, context, render, session, manifest)
            if (
                reason in {"slide_executor_busy", "session_sync_in_progress"}
                or self.policy_runner.busy
            ):
                return
            if reason is not None:
                self._deferred_transition_check = None
                self.policy.completed(prediction.decision_id, success=False, now_ns=now)
                logger.warning(
                    "presentation_policy event=deferred_action_dropped reason=%s decision_id=%s",
                    reason,
                    prediction.decision_id,
                )
                self.policy_status.setText(self._policy_rejection_status(reason))
                return
            if session is None or not self._session_access:
                return
            self._deferred_transition_check = None
            self._policy_check = (prediction, context, render)
            access = self._session_access
            target = self._prediction_target_payload(prediction)
            self.policy_runner.submit(
                lambda: self.agent.service.check_transition(access, session, target)
            )
            logger.warning(
                "presentation_policy event=deferred_action_rechecked decision_id=%s",
                prediction.decision_id,
            )
            self.policy_status.setText("Rechecking the suggestion after slide/session sync…")
            return

        assert deferred_proposal is not None
        value, created_ns = deferred_proposal
        proposal_snapshot = value.get("_client_context")
        if (
            not isinstance(proposal_snapshot, ContextSnapshot)
            or not proposal_snapshot.decision_segments
            or now - proposal_snapshot.decision_segments[-1].capture_end_ns > 5_000_000_000
        ):
            self._deferred_agent_proposal = None
            logger.warning(
                "presentation_policy event=deferred_proposal_dropped reason=stale_capture"
            )
            self.policy_status.setText("Suggestion expired while the slide was syncing; repeat it.")
            return
        if not proposal_snapshot.matches_decision(self.transcript_context.snapshot()):
            self._deferred_agent_proposal = None
            logger.warning(
                "presentation_policy event=deferred_proposal_dropped reason=%s",
                proposal_snapshot.decision_mismatch_reason(self.transcript_context.snapshot()),
            )
            self.policy_status.setText("Speech changed before the action could run; repeat it.")
            return
        if (
            self.executor.busy
            or self.session_runner.busy
            or self.powerpoint_runner.busy
            or self._session_pending_state is not None
            or self._session_restore_state is not None
            or self._stream_snapshot is not None
            or self.policy_runner.busy
        ):
            return
        self._deferred_agent_proposal = None
        logger.warning(
            "presentation_policy event=deferred_proposal_rechecked wait_ms=%d",
            max(0, (now - created_ns) // 1_000_000),
        )
        self._consider_policy(value)

    def _retry_deferred_native_transition(
        self,
        prediction: Prediction,
        context: ContextSnapshot,
        source: str,
        now_ns: int,
    ) -> None:
        native, session = self._native_present, self._session
        if now_ns - context.decision_segments[-1].capture_end_ns > 5_000_000_000:
            self._deferred_transition_check = None
            self.policy.completed(prediction.decision_id, success=False, now_ns=now_ns)
            logger.warning(
                "presentation_policy event=deferred_action_dropped reason=stale_capture "
                "decision_id=%s",
                prediction.decision_id,
            )
            self.policy_status.setText(
                "Suggestion expired while PowerPoint was syncing; repeat it."
            )
            return
        if not self.policy.enabled or self._closing or not self.microphone.enabled:
            self._deferred_transition_check = None
            self.policy.completed(prediction.decision_id, success=False, now_ns=now_ns)
            return
        mismatch = context.decision_mismatch_reason(self.transcript_context.snapshot())
        if mismatch is not None:
            self._deferred_transition_check = None
            self.policy.completed(prediction.decision_id, success=False, now_ns=now_ns)
            logger.warning(
                "presentation_policy event=deferred_action_dropped reason=%s decision_id=%s",
                mismatch,
                prediction.decision_id,
            )
            self.policy_status.setText(self._policy_rejection_status(mismatch))
            return
        if (
            native is None
            or session is None
            or not self._context_connected
            or not self._powerpoint_verified
            or native.binding.source_sha256 != source
            or session.state_version != prediction.state_version
        ):
            if (
                self.powerpoint_runner.busy
                or self.session_runner.busy
                or self._session_pending_state is not None
            ):
                return
            self._deferred_transition_check = None
            self.policy.completed(prediction.decision_id, success=False, now_ns=now_ns)
            logger.warning(
                "presentation_policy event=deferred_action_dropped "
                "reason=powerpoint_or_session_state_changed decision_id=%s",
                prediction.decision_id,
            )
            self.policy_status.setText(
                "PowerPoint/session state changed before the action could run; "
                "repeat the instruction."
            )
            return
        if (
            self.powerpoint_runner.busy
            or self.session_runner.busy
            or self.policy_runner.busy
            or self._policy_check is not None
        ):
            return
        if not self._session_access:
            return
        self._deferred_transition_check = None
        self._policy_check = (prediction, context, source)
        access = self._session_access
        target = self._prediction_target_payload(prediction)
        self.policy_runner.submit(
            lambda: self.agent.service.check_transition(access, session, target)
        )
        logger.warning(
            "presentation_policy event=deferred_action_rechecked decision_id=%s",
            prediction.decision_id,
        )
        self.policy_status.setText("Rechecking the suggestion after PowerPoint/session sync…")

    @staticmethod
    def _prediction_target_payload(prediction: Prediction) -> dict[str, Any]:
        region = prediction.target.region
        return {
            "tool": prediction.target.tool,
            "slide_id": prediction.target.slide_id,
            "action_id": prediction.target.action_id,
            "region": (
                {"x": region[0], "y": region[1], "width": region[2], "height": region[3]}
                if region is not None
                else None
            ),
        }

    @Slot(int, str)
    def _policy_failed(self, _identifier: int, message: str) -> None:
        pending, self._policy_check = self._policy_check, None
        if pending is not None:
            prediction = pending[0]
            self._policy_latency.pop(prediction.decision_id, None)
            self.policy.completed(prediction.decision_id, success=False, now_ns=time.monotonic_ns())
        self.policy_status.setText(message)

    @Slot()
    def _undo_policy_move(self) -> None:
        if (
            self._powerpoint_binding is not None
            and self._native_present is not None
            and self.powerpoint_runner.busy
            and self._policy_undo is not None
        ):
            # Preview/status work may still be finishing after a successful move.
            # Keep the saved state until the native worker can actually restore it.
            self._pending_native_undo = True
            self.policy_enabled.setChecked(False)
            return
        self._pending_native_undo = False
        undo, self._policy_undo = self._policy_undo, None
        self.policy_undo_button.setEnabled(False)
        self.policy_enabled.setChecked(False)
        if self._powerpoint_binding is not None and self._native_present is not None:
            if undo is not None and self._session is not None and self._powerpoint_info is not None:
                session_id, source, state = undo
                if (
                    session_id == self._session.id
                    and source == self._powerpoint_binding.source_sha256
                    and not self.powerpoint_runner.busy
                ):
                    native, info = self._native_present, self._powerpoint_info
                    self._native_completion = (None, state, state.last_accepted_action_id)
                    self._submit_powerpoint_task(
                        "undo_policy",
                        info.path,
                        lambda: native.restore(self.powerpoint, info, state),
                    )
            return
        manifest = self.executor.manifest
        if undo is None or manifest is None or self._session is None:
            return
        session_id, render, state = undo
        if session_id != self._session.id or render != manifest.render_id:
            return
        self._accepted_action_id = state.last_accepted_action_id
        # Use the existing versioned restore path, including zoom/highlight and failures.
        self._policy_undo_restoring = True
        self._restore_session_state(state)

    @Slot(object)
    def _agent_proposed(self, result: object) -> None:
        if not isinstance(result, dict):
            # AgentLoop emits None while `_update_agent` deliberately marks its
            # context stale during session synchronization. Keep the proposal
            # evidence until the sync retry can revalidate it, or reject it as
            # stale/corrected. Clearing it here loses valid actions on the next
            # 100 ms timer tick.
            syncing = any(
                (
                    self.executor.busy,
                    self.session_runner.busy,
                    self.powerpoint_runner.busy,
                    self._session_pending_state is not None,
                    self._session_restore_state is not None,
                    self._stream_snapshot is not None,
                )
            )
            if not syncing:
                self._deferred_agent_proposal = None
            self.agent_proposal.clear()
            context = self.transcript_context.snapshot()
            candidates = context.decision_segments
            if not context.fresh or not candidates or not context.segments:
                self.policy.clear_pending()
            return
        target = result["target"]
        tool = target["tool"]
        if tool == "no_action":
            messages = {
                "budget": "Suggestion allowance reached; continue presenting manually.",
                "disabled": "Suggestions are not enabled on this server.",
                "paced": "Waiting for the next suggestion opportunity.",
                "deadline": (
                    "That decision timed out; Buddy is still listening for your next phrase."
                ),
                "stay": "Keep the current view.",
                "uncertain": "Keep the current view.",
            }
            message = messages.get(
                result["reason"], "No suggestion available; continue presenting manually."
            )
        else:
            manifest = self.executor.manifest
            slide_ids = (
                tuple(slide.slide_id for slide in self._powerpoint_binding.slides)
                if self._powerpoint_binding is not None
                else tuple(slide.slide_id for slide in manifest.slides)
                if manifest is not None
                else ()
            )
            page = (
                next(
                    (
                        index
                        for index, slide_id in enumerate(slide_ids, 1)
                        if slide_id == target["slide_id"]
                    ),
                    None,
                )
                if slide_ids
                else None
            )
            if page is None:
                self.policy.clear_pending()
                self.agent_proposal.setText("Suggestion unavailable; reopen the presentation.")
                return
            labels = {
                "next_slide": f"Next slide (slide {page})",
                "previous_slide": f"Previous slide (slide {page})",
                "go_to_slide": f"Go to slide {page}",
                "zoom_region": f"Focus region on slide {page}",
                "highlight_region": f"Highlight region on slide {page}",
                "reset_view": f"Show slide {page} in full",
            }
            message = f"Suggested: {labels[tool]}"
        self.agent_proposal.setText(message)
        self._consider_policy(result)

    @Slot(object)
    def _speech_transcript(self, event: dict[str, object]) -> None:
        if (
            self._session is not None
            and self._context_connected
            and self._session_pending_state is None
            and self._session_restore_state is None
        ):
            self.transcript_context.bind(
                self._session.id,
                self._session.state_version,
                str(self.speech_language.currentData()),
            )
            self.transcript_context.accept(event)
            self._revoke_native_guard_if_speech_changed()
            # Process each recognized update as soon as Qt receives it. The 100 ms
            # timer remains a recovery path for session/state changes, not a speech
            # dispatch delay.
            self._update_agent()
        kind = "Final" if event["final"] else "Partial"
        start = float(cast(float, event["start_seconds"]))
        end = float(cast(float, event["end_seconds"]))
        self.speech_text.setPlainText(
            f"{kind} • {start:.2f}–{end:.2f} s • {event['latency_ms']} ms\n{event['text']}"
        )

    def _revoke_native_guard_if_speech_changed(self) -> None:
        submitted = self._native_policy_context
        if submitted is not None and not submitted.matches_decision(
            self.transcript_context.snapshot()
        ):
            self._native_guard.clear()
            self._native_policy_context = None

    @Slot()
    def _verify_microphone_pdf_session(self) -> None:
        if not self.microphone.enabled or (
            self._powerpoint_info is not None and self._powerpoint_binding is None
        ):
            return
        if self._session is None or not self._session_access:
            self._microphone_pdf_unverified()
            return
        if self.session_runner.busy:
            return
        session, access = self._session, self._session_access
        self._microphone_pdf_request_generation = self._microphone_pdf_generation
        self._submit_session_task(
            "microphone_verify",
            self._session_generation,
            None,
            lambda: self.session_service.read(access, session.id),
        )

    @Slot()
    def _microphone_pdf_unverified(self) -> None:
        if self.microphone.enabled:
            self.microphone.stop()
            self.microphone_status.setText(
                "Microphone stopped. Presentation session could not be verified."
            )

    def _current_screen_key(self, screen: QScreen) -> str:
        return "|".join(
            (screen.name(), screen.serialNumber(), screen.manufacturer(), screen.model())
        )

    @Slot(object)
    def _refresh_displays(self, _screen: QScreen | None = None) -> None:
        selected_key = str(self.settings.value("presentation/audience_display", ""))
        screens = QGuiApplication.screens()
        self.display_selector.blockSignals(True)
        self.display_selector.clear()
        primary = QGuiApplication.primaryScreen()
        selected = -1
        for index, screen in enumerate(screens):
            label = f"Display {index + 1}: {screen.name() or 'Unnamed'}"
            if screen == primary:
                label += " (Primary)"
            self.display_selector.addItem(label, screen)
            if self._current_screen_key(screen) == selected_key:
                selected = index
        if selected < 0 and primary in screens:
            selected = screens.index(primary)
        if selected >= 0:
            self.display_selector.setCurrentIndex(selected)
        self.display_selector.blockSignals(False)
        multiple = len(screens) > 1
        self.display_button.setVisible(multiple)
        self.display_menu.clear()
        if selected >= 0:
            selected_screen = screens[selected]
            self.display_button.setToolTip(
                f"Audience display: {selected_screen.name() or 'Unnamed display'}. "
                "Click to choose another display."
            )
        for index, screen in enumerate(screens):
            label = f"Display {index + 1}: {screen.name() or 'Unnamed'}"
            if screen == primary:
                label += " (Primary)"
            action = QAction(label, self.display_menu)
            action.setCheckable(True)
            action.setChecked(index == selected)
            action.triggered.connect(
                lambda checked=False, target=screen: self._select_screen(target)
            )
            self.display_menu.addAction(action)

    def _select_screen(self, screen: QScreen) -> None:
        if screen not in QGuiApplication.screens():
            return
        index = self.display_selector.findData(screen)
        if index >= 0:
            self.display_selector.setCurrentIndex(index)

    @Slot(int)
    def _display_changed(self, index: int) -> None:
        screen = self.display_selector.itemData(index)
        if isinstance(screen, QScreen):
            self.settings.setValue(
                "presentation/audience_display", self._current_screen_key(screen)
            )
            self.display_button.setToolTip(
                f"Audience display: {screen.name() or 'Unnamed display'}. "
                "Click to choose another display."
            )

    @Slot(object)
    def _screen_removed(self, removed: QScreen) -> None:
        self._refresh_displays()
        if self.fullscreen is not None and self.fullscreen.target_screen == removed:
            screens = QGuiApplication.screens()
            fallback: QScreen | None = QGuiApplication.primaryScreen()
            if fallback not in screens:
                fallback = screens[0] if screens else None
            if fallback is not None:
                self.fullscreen.move_to_screen(fallback)
                self.session_status.setText(
                    "The audience display disconnected. Presentation moved to an available display."
                )

    def _ensure_session(self, *, force: bool = False) -> None:
        manifest = self.executor.manifest
        binding = self._powerpoint_binding
        deck_id = (
            binding.deck_id
            if binding is not None
            else (manifest.deck_id if manifest is not None and manifest.account_bound else None)
        )
        if (
            self._closing
            or self._session_access is None
            or self._account_id is None
            or deck_id is None
        ):
            return
        key = (self._account_id, deck_id)
        if self._session_deck_id != deck_id:
            self._session_generation += 1
            self._session = None
            self._session_pending_state = None
            self._session_attempt = None
            self._session_error = False
            self._session_ended = False
            self._session_deck_id = deck_id
        if (
            self._session is not None
            or (self._session_ended and not force)
            or (self._session_attempt == key and not force)
        ):
            return
        if self.session_runner.busy:
            # The runner has one slot. Defer this start until an older deck's
            # request has completed; the old generation's response is ignored.
            self._session_followup = "ensure"
            return
        self._session_error = False
        self._session_ended = False
        self.session_status.setText("Restoring saved presentation position…")
        access, generation, service = (
            self._session_access,
            self._session_generation,
            self.session_service,
        )
        submitted = self._submit_session_task(
            "start",
            generation,
            None,
            lambda: service.start(access, deck_id),
        )
        if submitted:
            self._session_attempt = key
        else:
            self._session_followup = "ensure"

    def _submit_session_task(
        self,
        kind: str,
        generation: int,
        state: SessionState | None,
        operation: Callable[[], PresentationSession],
    ) -> bool:
        if self.session_runner.busy:
            return False

        def work() -> tuple[str, PresentationSession | None, str | None]:
            try:
                return "success", operation(), None
            except SessionConflict:
                return "conflict", None, None
            except LibraryAccessExpired as exc:
                return "expired", None, str(exc)
            except SessionAccessDenied as exc:
                return "session_denied", None, str(exc)
            except ServiceError as exc:
                return "error", None, str(exc)

        try:
            self._session_request = (kind, generation, state)
            self.session_runner.submit(work)
        except RuntimeError:
            self._session_request = None
            return False
        return True

    def _current_local_state(self) -> SessionState | None:
        if self._powerpoint_binding is not None:
            info = self._powerpoint_info
            if info is None or not self._powerpoint_verified:
                return None
            try:
                slide_id = self._powerpoint_binding.slide_id(info)
            except PowerPointError:
                return None
            return SessionState(
                active_slide_id=slide_id,
                focus_view=self._native_focus_view(),
                focus_region=self._native_focus_region(),
                autonomy_mode="autonomous" if self.policy.enabled else "manual",
                last_accepted_action_id=self._accepted_action_id,
            )
        manifest, slide = self.executor.manifest, self.surface.slide
        if manifest is None or slide is None:
            return None
        region = self.surface.highlight or (
            self.surface.region if self.surface.region != slide.full else None
        )
        focus = (
            "highlight_region" if self.surface.highlight else "zoom_region" if region else "slide"
        )
        return SessionState(
            active_slide_id=slide.slide_id,
            focus_view=focus,
            focus_region=(
                {name: float(getattr(region, name)) for name in ("x", "y", "width", "height")}
                if region is not None
                else None
            ),
            autonomy_mode="autonomous" if self.policy.enabled else "manual",
            last_accepted_action_id=self._accepted_action_id,
        )

    def _queue_session_state(self) -> None:
        state = self._current_local_state()
        if state is None:
            return
        if self._session is None:
            if self._session_attempt is not None or self._session_followup == "ensure":
                self._session_pending_state = state
            return
        request = self._session_request
        in_flight_state = (
            request[2]
            if request is not None
            and request[0] == "update"
            and request[1] == self._session_generation
            else None
        )
        if state == self._session.state and in_flight_state is None:
            self._session_pending_state = None
            return
        self._invalidate_transcript_context()
        self._session_pending_state = state
        if not self.session_runner.busy:
            QTimer.singleShot(0, self._flush_session_state)

    @Slot()
    def _flush_session_state(self) -> None:
        if (
            self._closing
            or self.session_runner.busy
            or self._session_pending_state is None
            or self._session_restore_state is not None
        ):
            return
        if self._session is None or self._session.status != "active" or not self._session_access:
            return
        state = self._session_pending_state
        access, session, generation, service = (
            self._session_access,
            self._session,
            self._session_generation,
            self.session_service,
        )
        self.session_status.setText("Saving presentation position…")
        self._submit_session_task(
            "update",
            generation,
            state,
            lambda: service.update(access, session, state),
        )

    def _retry_session(self) -> None:
        self.session_events.stop()
        if self._session is None:
            self._ensure_session(force=True)
        elif self._session_pending_state is not None:
            self._flush_session_state()
        elif self._session_access:
            self._read_latest_session()

    def _read_latest_session(self, *, conflict: bool = False) -> None:
        if self.session_runner.busy or self._session is None or not self._session_access:
            return
        session, access = self._session, self._session_access
        self._session_conflict_read = conflict
        self._submit_session_task(
            "read",
            self._session_generation,
            None,
            lambda: self.session_service.read(access, session.id),
        )

    def _session_button_clicked(self) -> None:
        if self._session is not None and self._session.status == "active":
            self._end_session()
        else:
            self._ensure_session(force=True)

    def _end_session(self) -> None:
        if self.session_runner.busy or self._session is None or not self._session_access:
            return
        self.microphone.stop()
        if self._session_pending_state is not None:
            self.session_status.setText(
                "The latest position is not saved yet. Retry sync before ending this session."
            )
            return
        session, access = self._session, self._session_access
        self.session_status.setText("Ending presentation session…")
        self._submit_session_task(
            "end",
            self._session_generation,
            None,
            lambda: self.session_service.end(access, session),
        )

    @Slot(int, object)
    def _session_succeeded(self, _identifier: int, result: object) -> None:
        request, self._session_request = self._session_request, None
        if request is None:
            return
        kind, generation, sent_state = request
        if generation != self._session_generation or self._closing:
            return
        outcome, session, message = result if isinstance(result, tuple) else ("error", None, None)
        if kind == "microphone_verify":
            if self._microphone_pdf_request_generation != self._microphone_pdf_generation:
                if self._session_pending_state is not None:
                    self._session_followup = "flush"
                return
            if (
                outcome == "success"
                and isinstance(session, PresentationSession)
                and session.status == "active"
                and self._session is not None
                and session.id == self._session.id
                and (
                    self._powerpoint_binding is None
                    or (
                        session.analysis_version_id == self._powerpoint_binding.analysis_id
                        and session.deck_id == self._powerpoint_binding.deck_id
                    )
                )
            ):
                if self.microphone.enabled:
                    self._microphone_pdf_deadline.start()
                if self._session_pending_state is not None:
                    self._session_followup = "flush"
            else:
                self._microphone_pdf_unverified()
                if outcome == "expired":
                    self.session_access_expired.emit(
                        message or "Sign in again to sync this presentation."
                    )
                    self._session_access = None
                    self._session = None
                    self._session_pending_state = None
                    self._session_followup = None
                    self._refresh()
                    return
                if outcome == "session_denied":
                    self._session = None
                    self._session_pending_state = None
                    self._session_followup = None
                    self._session_attempt = None
                    self._session_ended = True
                    self._session_error = False
                    self.session_status.setText(
                        message
                        or (
                            "This presentation session is no longer available. "
                            "Buddy sign-in is active."
                        )
                    )
                    self._refresh()
                    return
                if self._session_pending_state is not None:
                    self._session_followup = "flush"
                if (
                    outcome == "success"
                    and isinstance(session, PresentationSession)
                    and session.status == "ended"
                ):
                    self._session = None
                    self._session_attempt = None
                    self._session_pending_state = None
                    self._session_followup = None
                    self._session_ended = True
                    self.session_status.setText("This session was ended by another controller.")
                    self._refresh()
            return
        if outcome == "conflict":
            self._session_pending_state = None
            self.session_status.setText(
                "Another controller changed this presentation. Loading its saved position…"
            )
            self._session_followup = "read_conflict"
            return
        if outcome == "expired":
            self.microphone.stop()
            self.session_access_expired.emit(message or "Sign in again to sync this presentation.")
            self._session_access = None
            self._session = None
            self.session_status.setText(message or "Sign in again to sync this presentation.")
            self._refresh()
            return
        if outcome == "session_denied":
            self.microphone.stop()
            self._session = None
            self._session_pending_state = None
            self._session_followup = None
            self._session_attempt = None
            self._session_ended = True
            self._session_error = False
            self.session_status.setText(
                message
                or "This presentation session is no longer available. Buddy sign-in is active."
            )
            self._refresh()
            return
        if outcome != "success" or not isinstance(session, PresentationSession):
            self._session_error = True
            self.session_status.setText(message or "Could not sync the presentation session.")
            self._refresh()
            return
        self._session_error = False
        if self._powerpoint_binding is not None and (
            session.deck_id != self._powerpoint_binding.deck_id
            or session.analysis_version_id != self._powerpoint_binding.analysis_id
        ):
            self.microphone.stop()
            self._session = None
            self._session_error = True
            self._session_pending_state = None
            self.session_status.setText(
                "The saved PowerPoint analysis changed. Reopen it from Library."
            )
            self._refresh()
            return
        self._session = session
        if kind == "start":
            if self._session_pending_state is None:
                self._restore_session_state(session.state)
            else:
                if session.state is not None:
                    pending_state = self._session_pending_state
                    self._session_pending_state = replace(
                        session.state,
                        active_slide_id=pending_state.active_slide_id,
                        focus_view=pending_state.focus_view,
                        focus_region=pending_state.focus_region,
                    )
                self.session_status.setText("Presentation session connected.")
                self._session_followup = "flush"
        elif kind == "update":
            if self._session_pending_state == sent_state:
                self._session_pending_state = None
            self.session_status.setText("Presentation position saved.")
            if self._session_pending_state is not None:
                self._session_followup = "flush"
        elif kind == "read":
            self._session_pending_state = None
            if session.status == "ended":
                self.microphone.stop()
                self._session = None
                self._session_attempt = None
                self._session_ended = True
                self.session_status.setText("This session was ended by another controller.")
                self._refresh()
                return
            self._restore_session_state(session.state, conflict=self._session_conflict_read)
            self._session_conflict_read = False
        elif kind == "end":
            self._session_pending_state = None
            self._session_attempt = None
            self._session = None
            self._session_ended = True
            self.session_status.setText(
                "Presentation session ended. Your saved position is retained."
            )
        self._refresh()

    @Slot(int, str)
    def _session_failed(self, _identifier: int, message: str) -> None:
        request, self._session_request = self._session_request, None
        if request is None or request[1] != self._session_generation or self._closing:
            return
        if request[0] == "microphone_verify":
            if self._microphone_pdf_request_generation == self._microphone_pdf_generation:
                self._microphone_pdf_unverified()
            if self._session_pending_state is not None:
                self._session_followup = "flush"
            return
        self._session_error = True
        self.session_status.setText(message)
        self._refresh()

    @Slot()
    def _session_idle(self) -> None:
        if self._closing:
            return
        self._apply_stream_snapshot()
        followup, self._session_followup = self._session_followup, None
        if followup == "ensure":
            self._ensure_session()
        elif followup == "read_conflict":
            self._read_latest_session(conflict=True)
        elif followup == "flush":
            self._flush_session_state()
        self._refresh()

    @Slot(int, object)
    def _session_event_received(self, generation: int, event: object) -> None:
        if generation != self.session_events.generation:
            return
        try:
            session = self._session
            if (
                self._closing
                or not isinstance(event, SessionEvent)
                or session is None
                or event.session_id != session.id
                or event.state_version < session.state_version
            ):
                return
            if event.snapshot is not None:
                incoming = event.snapshot
                if (
                    incoming.owner_id != session.owner_id
                    or incoming.deck_id != session.deck_id
                    or incoming.analysis_version_id != session.analysis_version_id
                ):
                    return
                if incoming.state_version > session.state_version or incoming.status == "ended":
                    self._invalidate_transcript_context()
                    self.policy.clear_pending()
                    if incoming.status == "ended":
                        self.microphone.stop()
                    self._stream_snapshot = incoming
                    self._apply_stream_snapshot()
            else:
                # Consumers of speech/decision notifications must check freshness;
                # this signal never executes a slide action.
                self.session_event_received.emit(event)
        finally:
            self.session_events.acknowledge(generation)

    def _apply_stream_snapshot(self) -> None:
        incoming = self._stream_snapshot
        if (
            incoming is None
            or self.session_runner.busy
            or self.executor.busy
            or self._session_restore_state is not None
        ):
            return
        self._stream_snapshot = None
        if (
            self._session is None
            or incoming.id != self._session.id
            or incoming.state_version <= self._session.state_version
        ):
            return
        self._invalidate_transcript_context()
        self._session = incoming
        if incoming.status == "ended":
            self.microphone.stop()
            self._session = None
            self._session_ended = True
            self._session_pending_state = None
            self._session_followup = None
            self.session_status.setText("This session was ended by another controller.")
        elif self._session_pending_state is None:
            self._restore_session_state(incoming.state, conflict=True)
        # An unsent manual change takes precedence over remote observations. Its
        # replacement still uses the newest saved version and server validation.
        self._refresh()

    @Slot(int, str, str)
    def _session_stream_failed(self, generation: int, message: str, reason: str) -> None:
        if generation != self.session_events.generation or self._closing:
            return
        self._context_connected = False
        self._invalidate_transcript_context()
        self.microphone.stop()
        self._session_error = True
        if reason == "auth_expired":
            self._session_access = None
            self._session = None
            self._session_pending_state = None
            self._session_followup = None
            self.session_access_expired.emit(message)
        elif reason in {"session_forbidden", "session_missing"}:
            # End only the inaccessible presentation session. These responses do
            # not prove that the user's Buddy authentication has expired.
            self._session_access = None
            self._session = None
            self._session_pending_state = None
            self._session_followup = None
            self._session_attempt = None
            self._session_ended = reason == "session_missing"
        self.session_status.setText(message)
        self._refresh()

    def _restore_session_state(self, state: SessionState | None, *, conflict: bool = False) -> None:
        if state is not None:
            self._accepted_action_id = state.last_accepted_action_id
        if self._powerpoint_binding is not None:
            local = self._current_local_state()
            if local is None:
                self.microphone.stop()
                self.session_status.setText("PowerPoint slideshow verification is required.")
                return
            if state != local:
                self._session_pending_state = local
                self._session_followup = "flush"
                self.session_status.setText("Syncing the live PowerPoint slide…")
            else:
                self.session_status.setText("PowerPoint presentation session connected.")
            return
        manifest = self.executor.manifest
        if state is None or manifest is None:
            self.session_status.setText(
                "Presentation session connected; starting at the first slide."
            )
            return
        try:
            target = manifest.index(state.active_slide_id)
        except (ValueError, KeyError):
            self.microphone.stop()
            self._session = None
            self._session_error = True
            self.session_status.setText(
                "Saved position does not match this deck. Reload the latest copy from your library."
            )
            return
        self._session_restore_state = state
        self._restore_conflict = conflict
        self._restore_focus_applied = False
        if target != self.executor.current_index:
            self.send("go_to_slide", {"target_slide_id": state.active_slide_id}, restoring=True)
        else:
            self._apply_restored_focus()

    def _apply_restored_focus(self) -> None:
        state = self._session_restore_state
        if state is None:
            return
        if state.focus_region is None:
            if self.surface.slide is not None and (
                self.surface.region != self.surface.slide.full or self.surface.highlight is not None
            ):
                self.send("reset_view", restoring=True)
                return
            self._finish_session_restore()
            return
        if self._restore_focus_applied:
            self._finish_session_restore()
            return
        region = Region(**state.focus_region)
        self.surface.selection = region
        self._selection_changed(region)
        self._restore_focus_applied = True
        self.send(state.focus_view, {"region": state.focus_region}, restoring=True)

    def _finish_session_restore(self) -> None:
        conflict = getattr(self, "_restore_conflict", False)
        self._restore_conflict = False
        self._session_restore_state = None
        self._session_restore_action = None
        self._restore_focus_applied = False
        if self._policy_undo_restoring:
            self._policy_undo_restoring = False
            self._queue_session_state()
        self.session_status.setText(
            "Synced the latest position from another controller."
            if conflict
            else "Saved presentation position restored."
        )
        if self._session_pending_state is not None:
            QTimer.singleShot(0, self._flush_session_state)

    @Slot()
    def _choose_powerpoint(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, "Open PowerPoint presentation", "", "PowerPoint presentations (*.pptx)"
        )
        if filename:
            self._submit_powerpoint("launch", filename)

    @Slot()
    def _list_powerpoint_slideshows(self) -> None:
        self._submit_powerpoint("list")

    def _submit_powerpoint(self, action: str, path: str | None = None) -> None:
        if sys.platform != "win32" or self._closing:
            return
        if action in {"launch", "connect"}:
            self._powerpoint_verified = False
            self._powerpoint_verification.stop()
            self.microphone.stop()
        if self.powerpoint_runner.busy:
            if action != "status" and self._queued_powerpoint_operation is None:
                self._queued_powerpoint_operation = (action, path)
                self._refresh()
            return
        if action != "status":
            self.powerpoint_status.setText(
                "Starting PowerPoint…" if action == "launch" else "Checking running slideshows…"
            )
        try:
            operation = {
                "launch": lambda: self.powerpoint.launch(path or ""),
                "list": self.powerpoint.list_slideshows,
                "connect": lambda: self.powerpoint.connect(path or ""),
                "status": lambda: self.powerpoint.status(path or ""),
            }[action]
            self._submit_powerpoint_task(action, path, operation)
        except (KeyError, RuntimeError):
            self.powerpoint_status.setText("PowerPoint is busy. Try again when it is ready.")
            return

    def _submit_powerpoint_task(
        self, action: str, path: str | None, operation: Callable[[], object]
    ) -> None:
        request = self.powerpoint_runner.submit(operation)
        self._powerpoint_pending[request] = (action, path)
        self._refresh()

    @Slot(object)
    def _queue_powerpoint_action(self, action: PowerPointAction) -> None:
        self._native_manual_intent()
        if self._closing or self._powerpoint_info is None or self._queued_powerpoint_action:
            return
        try:
            if not isinstance(action, PowerPointAction):
                raise PowerPointError("Unsupported PowerPoint action.")
            action.validate()
        except PowerPointError as exc:
            self.powerpoint_controls.feedback.setText(str(exc))
            return
        self._queued_powerpoint_action = action
        self._refresh()
        self._powerpoint_idle()

    @Slot(object)
    def _queue_powerpoint_preview(self, info: PresentationInfo | None) -> None:
        if not self._closing and info is not None and self._powerpoint_info is not None:
            self._queued_powerpoint_preview = info
            self._powerpoint_idle()

    @Slot()
    def _powerpoint_idle(self) -> None:
        if self._closing or self.powerpoint_runner.busy:
            return
        if self._pending_native_undo:
            self._undo_policy_move()
            return
        if self._queued_powerpoint_operation is not None:
            operation, path = self._queued_powerpoint_operation
            self._queued_powerpoint_operation = None
            self._submit_powerpoint(operation, path)
            return
        if self._powerpoint_info is None:
            return
        if self._queued_powerpoint_action is not None:
            action = self._queued_powerpoint_action
            self._queued_powerpoint_action = None
            self._submit_powerpoint_task(
                "command", action.target.path, lambda: self.powerpoint.execute(action)
            )
        elif self._queued_powerpoint_preview is not None:
            expected = self._queued_powerpoint_preview
            self._queued_powerpoint_preview = None
            self._submit_powerpoint_task(
                "preview", expected.path, lambda: self.powerpoint.preview(expected)
            )
        self._refresh()

    @Slot(int, object)
    def _powerpoint_succeeded(self, request: int, result: object) -> None:
        pending = self._powerpoint_pending.pop(request, None)
        if pending is None or self._closing:
            return
        action, path = pending
        if action in {"policy", "saved_action", "undo_policy"} and isinstance(
            result, PowerPointActionResult
        ):
            self._native_completed(action, result)
            return
        if action in {"launch", "connect"} and isinstance(result, PresentationInfo):
            self._attach_powerpoint_info(result)
        elif action == "list" and isinstance(result, tuple):
            presentations = tuple(item for item in result if isinstance(item, PresentationInfo))
            if not presentations:
                self.powerpoint_status.setText(
                    "No running PowerPoint slideshow was found. Start one in PowerPoint first."
                )
            else:
                labels = [item.choice_label for item in presentations]
                selected, accepted = QInputDialog.getItem(
                    self,
                    "Connect to a PowerPoint slideshow",
                    "Choose the presentation Buddy should track:",
                    labels,
                    0,
                    False,
                )
                if accepted:
                    index = labels.index(selected)
                    item = presentations[index]
                    self._submit_powerpoint("connect", item.path)
                else:
                    self.powerpoint_status.setText("PowerPoint connection cancelled.")
        elif action == "status":
            if isinstance(result, PresentationInfo):
                if (
                    self._powerpoint_info is not None
                    and self._powerpoint_info.window_id != result.window_id
                ):
                    self._powerpoint_lost(
                        "The PowerPoint slideshow changed. Connect again to select it."
                    )
                else:
                    self._attach_powerpoint_info(result)
            else:
                self._powerpoint_lost()
        elif action == "preview" and isinstance(result, PowerPointFrame):
            self.powerpoint_controls.set_frame(result)
        elif action == "command" and isinstance(result, PowerPointActionResult):
            self._attach_powerpoint_info(result.info)
            self.powerpoint_controls.completed(result)
            if result.action.kind in {
                "draw_line",
                "draw_arrow",
                "draw_rectangle",
                "draw_oval",
                "draw_underline",
                "draw_freehand",
                "highlight_region",
                "filled_rectangle",
                "filled_oval",
                "text_box",
                "callout",
                "undo_annotation",
                "erase_region",
                "erase_drawing",
                "reset_view",
            }:
                # Shape annotations are part of the live slide. Re-export it so the
                # larger reference matches what PowerPoint is actually showing.
                self._queued_powerpoint_preview = result.info
        self._refresh()

    @Slot(int, str)
    def _powerpoint_failed(self, request: int, message: str) -> None:
        pending = self._powerpoint_pending.pop(request, None)
        if pending is None or self._closing:
            return
        action, _ = pending
        if action in {"policy", "saved_action", "undo_policy"}:
            completion = self._native_completion
            self._native_completion = None
            self._native_guard.clear()
            context, self._native_policy_context = self._native_policy_context, None
            if completion is not None and completion[0] is not None:
                prediction = completion[0]
                self._log_policy_completion(
                    prediction, context, "pptx", time.monotonic_ns(), "failed"
                )
                self.policy.completed(
                    prediction.decision_id,
                    success=False,
                    now_ns=time.monotonic_ns(),
                )
            self.policy_status.setText(message)
            self.generated_status.setText(message)
            return
        if action == "status":
            # If the show cannot be verified, stop listening until an explicit restart.
            self._powerpoint_verified = False
            self._powerpoint_verification.stop()
            self.microphone.stop()
            # A transient COM failure does not mean the show disconnected. Keep the
            # last known connected line in place so retries cannot make it blink.
            if self._powerpoint_info is None:
                self.powerpoint_status.setText(
                    f"PowerPoint status check failed; retrying. {message}"
                )
            self._powerpoint_poll.start()
        elif action in {"command", "preview"}:
            self.powerpoint_controls.feedback.setText(message)
        else:
            self.powerpoint_status.setText(message)
        self._refresh()

    def _attach_powerpoint_info(self, info: PresentationInfo) -> None:
        binding = self._powerpoint_binding
        if binding is not None:
            try:
                binding.slide_id(info)
            except PowerPointError:
                self._powerpoint_lost(
                    "The slideshow differs from the saved analysis. Reopen it from Review."
                )
                self._refresh()
                return
        previous = self._powerpoint_info
        if (
            previous is not None
            and previous.slide_key != info.slide_key
            and self._native_completion is None
        ):
            self._native_manual_intent()
            if self._native_present is not None:
                self._native_present.focus = None
        if previous is None or (previous.path, previous.window_id) != (info.path, info.window_id):
            self.microphone.stop()
        self._powerpoint_info = info
        self._powerpoint_verified = True
        self._powerpoint_verification.start()
        self._powerpoint_path = info.path
        self.powerpoint_controls.set_info(info)
        if binding is not None and self._session_access is not None:
            if self._session is None:
                self._ensure_session()
            elif previous is None or previous.slide_key != info.slide_key:
                self._queue_session_state()
        if (previous is None or previous.slide_key != info.slide_key) and min(
            info.slide_width, info.slide_height
        ) > 0:
            self._queued_powerpoint_preview = info
        status = (
            f"Connected to {info.name} — slide {info.slide_index} of {info.slide_count}. "
            "PowerPoint is displaying the slideshow."
        )
        # Status checks run frequently. Re-setting identical text can trigger
        # QLabel/layout repaints and make the presentation controls appear to flicker.
        if self.powerpoint_status.text() != status:
            self.powerpoint_status.setText(status)
        if not self._powerpoint_poll.isActive():
            self._powerpoint_poll.start()

    def _poll_powerpoint(self) -> None:
        if self._powerpoint_path is not None:
            self._submit_powerpoint("status", self._powerpoint_path)

    @Slot()
    def _powerpoint_verification_expired(self) -> None:
        if self._powerpoint_info is not None:
            self._powerpoint_verified = False
            self.microphone.stop()
            self.microphone_status.setText(
                "Microphone stopped. PowerPoint connection could not be verified."
            )
            self._refresh()

    def _powerpoint_lost(self, reason: str | None = None) -> None:
        self._native_present = None
        self.policy_enabled.setChecked(False)
        self._native_manual_intent()
        if self._powerpoint_binding is not None:
            self._session_generation += 1
            self._session = None
            self._session_pending_state = None
            self._session_attempt = None
            self._session_deck_id = None
            self._session_followup = None
        self.microphone.stop()
        self._powerpoint_verification.stop()
        self._powerpoint_poll.stop()
        self._queued_powerpoint_action = None
        self._queued_powerpoint_preview = None
        self._powerpoint_info = None
        self._powerpoint_binding = None
        self._powerpoint_verified = False
        self._powerpoint_path = None
        self.powerpoint_controls.set_info(None)
        self.powerpoint_status.setText(
            reason
            or "PowerPoint slideshow disconnected. Connect again to explicitly select a slideshow."
        )

    @Slot()
    def _disconnect_powerpoint(self) -> None:
        self._native_manual_intent()
        self._native_present = None
        if self._powerpoint_binding is not None:
            self._session_generation += 1
            self._session = None
            self._session_pending_state = None
            self._session_attempt = None
            self._session_deck_id = None
            self._session_followup = None
        self.microphone.stop()
        self._powerpoint_verification.stop()
        with suppress(Exception):
            self.powerpoint.release_zoom()
        self._powerpoint_poll.stop()
        # Invalidate in-flight work: neither late success nor failure may restore
        # a connection or restart polling after the user explicitly disconnects.
        self._powerpoint_pending.clear()
        self._queued_powerpoint_action = None
        self._queued_powerpoint_preview = None
        self._queued_powerpoint_operation = None
        self._powerpoint_info = None
        self._powerpoint_binding = None
        self._powerpoint_verified = False
        self._powerpoint_path = None
        self.powerpoint_controls.set_info(None)
        self.powerpoint_status.setText(
            "Disconnected from Buddy. PowerPoint remains open and the slideshow is unchanged."
        )
        self._refresh()

    def bind_saved_powerpoint(self, binding: PowerPointSessionBinding) -> None:
        if self._closing or self._session_access is None or self._account_id is None:
            return
        self._disconnect_powerpoint()
        self._powerpoint_binding = binding
        self._native_present = PowerPointPresent(binding)
        self._session_generation += 1
        self._session = None
        self._session_attempt = None
        self._session_pending_state = None
        self._session_deck_id = None
        self._session_ended = False
        self.session_status.setText("Connecting the saved PowerPoint slideshow…")
        self._submit_powerpoint("connect", binding.path)
        self._refresh()

    def invalidate_saved_powerpoint(self) -> None:
        if self._powerpoint_binding is not None:
            self._disconnect_powerpoint()

    @Slot()
    def _choose_manifest(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, "Open presentation", "", "PDF presentations (*.pdf);;Slide manifests (*.json)"
        )
        if filename:
            if Path(filename).suffix.lower() == ".pdf":
                self.load_pdf(Path(filename))
            else:
                self.load_manifest(Path(filename))

    def load_pdf(
        self, source: Path | bytes, *, deck_id: str | None = None, revision: str | None = None
    ) -> None:
        self._pdf_warming = False
        self.pdf_warmup_status.hide()
        self.pdf_warmup_progress_bar.hide()
        self.invalidate_saved_powerpoint()
        self.microphone.stop()
        self._session_generation += 1
        self._session = None
        self._session_deck_id = deck_id
        self._session_attempt = None
        self._session_pending_state = None
        self._session_restore_state = None
        self._session_restore_action = None
        self._session_error = False
        self._session_ended = False
        self.surface.clear_selection()
        self._selection_changed(None)
        self.session_status.setText("")
        self.feedback.setText("Opening PDF…")
        self.executor.load_pdf(source, deck_id=deck_id, revision=revision)
        self._refresh()

    def load_manifest(self, path: Path) -> None:
        self._pdf_warming = False
        self.pdf_warmup_status.hide()
        self.pdf_warmup_progress_bar.hide()
        self.microphone.stop()
        self._session_generation += 1
        self._session = None
        self._session_deck_id = None
        self._session_attempt = None
        self._session_pending_state = None
        self._session_restore_state = None
        self._session_restore_action = None
        self.surface.clear_selection()
        self._selection_changed(None)
        self.session_status.setText("")
        self.feedback.setText("Loading slides…")
        self.executor.load_manifest(path)
        self._refresh()

    @Slot(int, int)
    def _pdf_warmup_progress(self, completed: int, total: int) -> None:
        if total <= 0:
            return
        self._pdf_warming = True
        self.pdf_warmup_status.setText(f"Preparing PDF pages… {completed} of {total}")
        self.pdf_warmup_status.show()
        self.pdf_warmup_progress_bar.setRange(0, total)
        self.pdf_warmup_progress_bar.setValue(min(completed, total))
        self.pdf_warmup_progress_bar.show()
        self._refresh()

    @Slot(str)
    def _pdf_warmup_finished(self, outcome: str) -> None:
        self._pdf_warming = False
        self.pdf_warmup_progress_bar.hide()
        if outcome == "skipped":
            self.pdf_warmup_status.setText(
                "This PDF is above Buddy’s full-preload limit. It is ready; "
                "pages will render as needed."
            )
            self.pdf_warmup_status.show()
        elif outcome == "partial":
            self.pdf_warmup_status.setText(
                "Some pages could not be preloaded. The presentation is ready; "
                "those pages will render as needed."
            )
            self.pdf_warmup_status.show()
        else:
            self.pdf_warmup_status.clear()
            self.pdf_warmup_status.hide()
        self._refresh()

    def enter_fullscreen(self) -> None:
        if self.fullscreen is not None:
            self.fullscreen.close()
            return
        if self.executor.manifest is None or self._closing or self._pdf_warming:
            return
        self.fullscreen = FullscreenPresentation(
            self.surface,
            self.body,
            self.send,
            self.zoom_button.click,
            self.highlight_button.click,
            self._fullscreen_go,
            self,
            self._selected_screen(),
        )
        self.fullscreen.exited.connect(self._fullscreen_exited)
        selected_screen = self._selected_screen()
        if selected_screen is not None:
            self.fullscreen.move_to_screen(selected_screen)
        self.fullscreen.showFullScreen()
        self.fullscreen.activateWindow()
        self.surface.setFocus()

    def _fullscreen_exited(self) -> None:
        self.fullscreen = None
        if not self._closing:
            self.fullscreen_button.setFocus()

    def _fullscreen_go(self) -> None:
        manifest = self.executor.manifest
        if manifest is None or self.executor.busy:
            return
        page, accepted = QInputDialog.getInt(
            self.fullscreen or self,
            "Go to slide",
            "Slide:",
            self.executor.current_index + 1,
            1,
            len(manifest.slides),
        )
        if accepted:
            self.send("go_to_slide", {"target_slide_id": manifest.slides[page - 1].slide_id})

    def send(
        self,
        kind: str,
        params: dict[str, object] | None = None,
        *,
        restoring: bool = False,
    ) -> None:
        manifest, slide = self.executor.manifest, self.surface.slide
        if (
            manifest is None
            or slide is None
            or self._closing
            or (self._pdf_warming and manifest.pdf_content is not None)
        ):
            return
        if not restoring:
            if self._policy_execution is not None:
                self.executor.cancel()
            if self._policy_check is not None:
                prediction = self._policy_check[0]
                self._policy_latency.pop(prediction.decision_id, None)
                self.policy.completed(
                    prediction.decision_id,
                    success=False,
                    now_ns=time.monotonic_ns(),
                )
                self._policy_check = None
            self.policy.reset(manual=True, now_ns=time.monotonic_ns())
            self._invalidate_transcript_context()
            self._policy_check = None
            self._policy_undo = None
            self.policy_undo_button.setEnabled(False)
            self._accepted_action_id = None
        self._instance += 1
        instance_id = f"manual:{self._instance}"
        if restoring and self._session_restore_state is not None:
            self._session_restore_action = instance_id
        self.executor.dispatch(
            {
                "version": "1.1",
                "instance_id": instance_id,
                "purpose": {
                    "next_slide": "Continue to the next slide.",
                    "previous_slide": "Revisit the previous slide.",
                    "go_to_slide": "Go directly to the selected slide.",
                    "zoom_region": "Focus on the selected content.",
                    "highlight_region": "Emphasize the selected content.",
                    "reset_view": "Restore the full slide and remove highlighting.",
                }[kind],
                "deck_id": manifest.deck_id,
                "render_id": manifest.render_id,
                "slide_id": slide.slide_id,
                "type": kind,
                "params": params or {},
            }
        )
        if kind == "reset_view":
            self.surface.clear_selection()
            self._selection_changed(None)
        self._refresh()

    @Slot()
    def _go(self) -> None:
        target = self.target.currentData()
        if isinstance(target, str):
            self.send("go_to_slide", {"target_slide_id": target})

    def _region_action(self, kind: str) -> None:
        self.send(kind, {"region": {name: field.value() for name, field in self.fields.items()}})

    @Slot(object)
    def _selection_changed(self, region: Region | None) -> None:
        for name, field in self.fields.items():
            field.blockSignals(True)
            field.setValue(getattr(region, name) if region is not None else 0)
            field.blockSignals(False)
        self._refresh()

    @Slot()
    def _fields_changed(self) -> None:
        values = {name: field.value() for name, field in self.fields.items()}
        self.surface.selection = (
            Region(**values) if values["width"] > 0 and values["height"] > 0 else None
        )
        self.surface.update()
        self._refresh()

    @Slot(object)
    def _completed(self, result: Completion) -> None:
        prediction = self._policy_execution
        if prediction is not None and result.instance_id == "policy:" + prediction.decision_id:
            self._policy_execution = None
            success = result.status == "completed"
            completed_ns = time.monotonic_ns()
            self._log_policy_completion(prediction, None, "pdf", completed_ns, result.status)
            self.policy.completed(prediction.decision_id, success=success, now_ns=completed_ns)
            if success:
                self._accepted_action_id = prediction.target.action_id
                manifest = self.executor.manifest
                if self._policy_before is not None and manifest is not None:
                    self._policy_undo = (
                        prediction.session_id,
                        manifest.render_id,
                        self._policy_before,
                    )
                    self.policy_undo_button.setEnabled(True)
                self.transition_observed.emit(
                    {
                        "policy_version": POLICY_VERSION,
                        "decision_id": prediction.decision_id,
                        "target": prediction.target,
                        "completed_ns": time.monotonic_ns(),
                        "animated": False,  # PDF commits immediately, without an animation.
                        "calibration_sha256": self.policy.calibration_sha256,
                        "settings_sha256": self.policy.settings_sha256,
                    }
                )
                self.policy_status.setText("Automatic move completed. Undo is available.")
            else:
                self.policy_status.setText(
                    "Buddy found the action but did not complete it. "
                    "Manual controls remain available; "
                    "repeat the request. Automatic actions are still on."
                )
            self._policy_before = None
        manifest, slide = self.executor.manifest, self.surface.slide
        key = (manifest.deck_id, manifest.render_id, slide.slide_id) if manifest and slide else None
        if result.status in {"failed", "cancelled"} or key != self._committed_key:
            self._committed_key = key
            self.surface.clear_selection()
            self._selection_changed(None)
        if manifest is not None:
            target = self.target.currentData()
            if [self.target.itemData(i) for i in range(self.target.count())] != [
                s.slide_id for s in manifest.slides
            ]:
                self.target.clear()
                for index, item in enumerate(manifest.slides):
                    self.target.addItem(f"Slide {index + 1}", item.slide_id)
            elif isinstance(target, str):
                self.target.setCurrentIndex(self.target.findData(target))
            self.counter.setText(
                f"Slide {self.executor.current_index + 1} of {len(manifest.slides)}"
            )
        self.feedback.setText(
            result.message
            if result.status in {"failed", "rejected"}
            else "Full slide restored."
            if result.status == "cancelled"
            else ""
        )
        if (
            result.status == "completed"
            and result.instance_id is None
            and manifest is not None
            and manifest.account_bound
        ):
            if self._session_access:
                self._ensure_session()
            else:
                self.session_status.setText("Sign in to sync this presentation's position.")
        self._refresh()
        if result.instance_id is not None and result.instance_id == self._session_restore_action:
            self._session_restore_action = None
            if result.status != "completed":
                self._session_restore_state = None
                self._session_error = True
                self.session_status.setText(
                    "Saved position could not be restored. Manual controls remain available."
                )
            else:
                QTimer.singleShot(0, self._apply_restored_focus)
            return
        if (
            result.status == "completed"
            and result.instance_id is not None
            and self._session_restore_state is None
        ):
            self._queue_session_state()

    def _selected_screen(self) -> QScreen | None:
        screen = self.display_selector.currentData()
        screens = QGuiApplication.screens()
        if isinstance(screen, QScreen) and screen in screens:
            return screen
        primary = QGuiApplication.primaryScreen()
        return primary if primary in screens else (screens[0] if screens else None)

    @Slot()
    def _refresh(self) -> None:
        self._refresh_generated()
        self.transcript_context.bind(
            self._session.id if isinstance(self._session, PresentationSession) else "",
            self._session.state_version if isinstance(self._session, PresentationSession) else 0,
            str(self.speech_language.currentData()),
        )
        active_session = (
            self._session
            if not self._closing
            and isinstance(self._session, PresentationSession)
            and self._session.status == "active"
            else None
        )
        generation = self.session_events.generation
        self.session_events.bind(self.config, self._session_access, active_session)
        if generation != self.session_events.generation:
            self._invalidate_transcript_context()
            self._stream_snapshot = None
        elif self._stream_snapshot is not None:
            QTimer.singleShot(0, self._apply_stream_snapshot)
        loaded = self.executor.manifest is not None and not self._closing
        account_bound = bool(self.executor.manifest and self.executor.manifest.account_bound)
        powerpoint_connected = self._powerpoint_info is not None
        show_pdf_surface = loaded or not powerpoint_connected
        self.surface.setVisible(show_pdf_surface)
        self.surface.empty.setVisible(show_pdf_surface and self.surface.slide is None)
        self.pdf_controls.setVisible(loaded or not powerpoint_connected)
        ready = loaded and not self.executor.busy and not self._pdf_warming
        self.open_button.setEnabled(
            not self.executor.busy and not self._pdf_warming and not self._closing
        )
        # Fast status checks should not repeatedly disable and re-enable the visible
        # controls. Only user-initiated launch/connect/list operations lock them.
        powerpoint_action_busy = (
            self._closing
            or self._queued_powerpoint_action is not None
            or self._queued_powerpoint_operation is not None
            or any(action != "status" for action, _path in self._powerpoint_pending.values())
        )
        self.powerpoint_controls.busy = powerpoint_action_busy
        self.powerpoint_controls.refresh()
        self.powerpoint_open_button.setEnabled(
            sys.platform == "win32" and not powerpoint_action_busy
        )
        self.powerpoint_connect_button.setEnabled(
            sys.platform == "win32" and not powerpoint_action_busy
        )
        self.powerpoint_disconnect_button.setEnabled(
            self._powerpoint_info is not None and not powerpoint_action_busy
        )
        for widget in (self.target, self.go_button, self.fullscreen_button):
            widget.setVisible(loaded)
        self.fullscreen_button.setEnabled(ready)
        self.counter.setVisible(loaded)
        for widget in (self.previous_button, self.next_button, self.target, self.go_button):
            widget.setEnabled(ready)
        self.reset_button.setEnabled(ready)
        for field in self.fields.values():
            field.setEnabled(ready)
        selected = ready and self.surface.selection is not None
        self.zoom_button.setEnabled(selected)
        self.highlight_button.setEnabled(selected)
        self.surface.selectable = ready
        session_controls = (loaded and account_bound) or self._powerpoint_binding is not None
        mic_available = self._microphone_session_available()
        self.microphone_device.setEnabled(bool(mic_available) and not self.microphone.enabled)
        self.microphone_button.setEnabled(
            bool(mic_available) and self.microphone_device.count() > 0 and not self._pdf_warming
        )
        self.microphone_button.setToolTip(
            "No microphone was detected."
            if self.microphone_device.count() == 0
            else "Wait for the saved presentation session to connect and sync the current slide."
            if self._powerpoint_binding is not None and not mic_available
            else "Start or stop microphone capture"
        )
        self.microphone_button.setText(
            "Stop microphone" if self.microphone.enabled else "Start microphone"
        )
        self.microphone_button.setAccessibleName(self.microphone_button.text())
        self.microphone_mute.setEnabled(bool(mic_available) and self.microphone.enabled)
        self.microphone_pause.setEnabled(bool(mic_available) and self.microphone.enabled)
        self.session_status.setVisible(session_controls)
        self.session_button.setVisible(session_controls and self._session_access is not None)
        self.session_button.setEnabled(
            session_controls and not self.session_runner.busy and self._session_access is not None
        )
        if self._session is not None and self._session.status == "active":
            self.session_button.setText("End session")
        elif self._session_ended:
            self.session_button.setText("Start session")
        elif self._session_error:
            self.session_button.setText("Retry session")
        else:
            self.session_button.setText("Connecting…")
        self.session_retry_button.setVisible(
            session_controls
            and (self._session_pending_state is not None or self._session_error)
            and self._session is not None
        )

    def _microphone_session_available(self) -> bool:
        pdf_active = (
            self.executor.manifest is not None
            and self.executor.manifest.account_bound
            and self._session is not None
            and self._session.status == "active"
            and self._session_access is not None
        )
        if self._powerpoint_info is not None:
            return (
                not self._closing
                and self._powerpoint_verified
                and (
                    self._powerpoint_binding is None
                    or (
                        self._session is not None
                        and self._session.status == "active"
                        and self._session_access is not None
                        and self._session_pending_state is None
                        and self._session.state == self._current_local_state()
                    )
                )
            )
        return not self._closing and pdf_active

    def hideEvent(self, event: QHideEvent) -> None:
        # A hidden presentation must not keep a microphone running out of view.
        if hasattr(self, "microphone") and not getattr(self, "_closing", False):
            self.microphone.stop()
            self.agent_enabled.setChecked(False)
        super().hideEvent(event)

    def shutdown(self) -> None:
        self._closing = True
        self.policy_enabled.setChecked(False)
        self.policy_runner.stop()
        self.generated_runner.stop()
        self._native_guard.clear()
        self._native_policy_context = None
        self._agent_tick.stop()
        self.agent.stop()
        self.session_events.stop()
        self._powerpoint_verification.stop()
        self.speech.stop()
        self.microphone.shutdown()
        with suppress(Exception):
            self.powerpoint.release_zoom()
        self._queued_powerpoint_action = None
        self._queued_powerpoint_preview = None
        self._queued_powerpoint_operation = None
        if self.fullscreen is not None:
            self.fullscreen.close()
        self.executor.close()
        self.session_runner.stop()
        self._powerpoint_poll.stop()
        self.powerpoint_runner.stop()
        self._refresh()

    def clear_session_binding(self) -> None:
        self._generated_analysis = None
        self._generated_request = None
        self._native_guard.clear()
        self._native_policy_context = None
        self.invalidate_saved_powerpoint()
        self.agent_enabled.setChecked(False)
        self.microphone.stop()
        self._session_generation += 1
        self._account_id = None
        self._session_access = None
        self._session = None
        self._session_deck_id = None
        self._session_attempt = None
        self._session_pending_state = None
        self._session_restore_state = None
        self._session_restore_action = None
        self._session_error = False
        self._session_ended = False
        self.session_status.setText("")
        self._refresh()
