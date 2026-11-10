"""Local rendered-slide controls using the production six-command executor."""

import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import cast

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
)
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

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
from buddy_desktop.presentation_session_service import (
    PresentationSession,
    PresentationSessionService,
    SessionConflict,
    SessionState,
)
from buddy_desktop.services import ServiceError
from buddy_desktop.slide_contract import Region
from buddy_desktop.slide_executor import Completion, SlideExecutor
from buddy_desktop.slide_surface import SlideSurface
from buddy_desktop.tasks import TaskRunner
from buddy_desktop.views import Page, StatusLabel
from buddy_desktop.visuals import BuddyMark, command


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
        self.microphone = MicrophoneController(self)
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
        self.microphone.status_changed.connect(self._microphone_status_changed)
        self.microphone.devices_changed.connect(self._microphone_devices_changed)
        self.body.addWidget(self.microphone_panel)
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
        if message == "Microphone on" and self._powerpoint_info is None:
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

    @Slot()
    def _verify_microphone_pdf_session(self) -> None:
        if not self.microphone.enabled or self._powerpoint_info is not None:
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
        if self.microphone.enabled and self._powerpoint_info is None:
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
        if (
            self._closing
            or self._session_access is None
            or self._account_id is None
            or manifest is None
            or not manifest.account_bound
        ):
            return
        key = (self._account_id, manifest.deck_id)
        if self._session_deck_id != manifest.deck_id:
            self._session_generation += 1
            self._session = None
            self._session_pending_state = None
            self._session_attempt = None
            self._session_error = False
            self._session_ended = False
            self._session_deck_id = manifest.deck_id
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
            lambda: service.start(access, manifest.deck_id),
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
            autonomy_mode=(
                self._session.state.autonomy_mode
                if self._session is not None and self._session.state is not None
                else "manual"
            ),
            last_accepted_action_id=(
                self._session.state.last_accepted_action_id
                if self._session is not None and self._session.state is not None
                else None
            ),
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
        self._session_pending_state = state
        if not self.session_runner.busy:
            QTimer.singleShot(0, self._flush_session_state)

    @Slot()
    def _flush_session_state(self) -> None:
        if self._closing or self.session_runner.busy or self._session_pending_state is None:
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
            ):
                if self.microphone.enabled and self._powerpoint_info is None:
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
        if outcome != "success" or not isinstance(session, PresentationSession):
            self._session_error = True
            self.session_status.setText(message or "Could not sync the presentation session.")
            self._refresh()
            return
        self._session_error = False
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
        followup, self._session_followup = self._session_followup, None
        if followup == "ensure":
            self._ensure_session()
        elif followup == "read_conflict":
            self._read_latest_session(conflict=True)
        elif followup == "flush":
            self._flush_session_state()
        self._refresh()

    def _restore_session_state(self, state: SessionState | None, *, conflict: bool = False) -> None:
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
        previous = self._powerpoint_info
        if previous is None or (previous.path, previous.window_id) != (info.path, info.window_id):
            self.microphone.stop()
        self._powerpoint_info = info
        self._powerpoint_verified = True
        self._powerpoint_verification.start()
        self._powerpoint_path = info.path
        self.powerpoint_controls.set_info(info)
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
        self.microphone.stop()
        self._powerpoint_verification.stop()
        self._powerpoint_poll.stop()
        self._queued_powerpoint_action = None
        self._queued_powerpoint_preview = None
        self._powerpoint_info = None
        self._powerpoint_verified = False
        self._powerpoint_path = None
        self.powerpoint_controls.set_info(None)
        self.powerpoint_status.setText(
            reason
            or "PowerPoint slideshow disconnected. Connect again to explicitly select a slideshow."
        )

    @Slot()
    def _disconnect_powerpoint(self) -> None:
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
        self._powerpoint_verified = False
        self._powerpoint_path = None
        self.powerpoint_controls.set_info(None)
        self.powerpoint_status.setText(
            "Disconnected from Buddy. PowerPoint remains open and the slideshow is unchanged."
        )
        self._refresh()

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

    def enter_fullscreen(self) -> None:
        if self.fullscreen is not None:
            self.fullscreen.close()
            return
        if self.executor.manifest is None or self._closing:
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
        if manifest is None or slide is None or self._closing:
            return
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
        loaded = self.executor.manifest is not None and not self._closing
        account_bound = bool(self.executor.manifest and self.executor.manifest.account_bound)
        powerpoint_connected = self._powerpoint_info is not None
        show_pdf_surface = loaded or not powerpoint_connected
        self.surface.setVisible(show_pdf_surface)
        self.surface.empty.setVisible(show_pdf_surface and self.surface.slide is None)
        self.pdf_controls.setVisible(loaded or not powerpoint_connected)
        ready = loaded and not self.executor.busy
        self.open_button.setEnabled(not self.executor.busy and not self._closing)
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
        self.fullscreen_button.setEnabled(loaded)
        self.counter.setVisible(loaded)
        for widget in (self.previous_button, self.next_button, self.target, self.go_button):
            widget.setEnabled(ready)
        self.reset_button.setEnabled(loaded)
        for field in self.fields.values():
            field.setEnabled(ready)
        selected = ready and self.surface.selection is not None
        self.zoom_button.setEnabled(selected)
        self.highlight_button.setEnabled(selected)
        self.surface.selectable = ready
        session_controls = loaded and account_bound
        mic_available = self._microphone_session_available()
        self.microphone_device.setEnabled(bool(mic_available) and not self.microphone.enabled)
        self.microphone_button.setEnabled(
            bool(mic_available) and self.microphone_device.count() > 0
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
            and self._session_pending_state is not None
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
            return not self._closing and self._powerpoint_verified
        return not self._closing and pdf_active

    def hideEvent(self, event: QHideEvent) -> None:
        # A hidden presentation must not keep a microphone running out of view.
        if hasattr(self, "microphone") and not getattr(self, "_closing", False):
            self.microphone.stop()
        super().hideEvent(event)

    def shutdown(self) -> None:
        self._closing = True
        self._powerpoint_verification.stop()
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
