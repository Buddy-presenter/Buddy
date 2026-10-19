"""Local rendered-slide controls using the production six-command executor."""

import sys
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from buddy_desktop.fullscreen import FullscreenPresentation
from buddy_desktop.powerpoint import PowerPointAutomation, PresentationInfo
from buddy_desktop.powerpoint_contract import (
    PowerPointAction,
    PowerPointActionResult,
    PowerPointError,
    PowerPointFrame,
)
from buddy_desktop.powerpoint_controls import PowerPointControls
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

    def __init__(self) -> None:
        super().__init__("Present", "")
        self.body.setSpacing(12)
        self._closing = False
        self.fullscreen: FullscreenPresentation | None = None
        self._instance = 0
        self._committed_key: tuple[str, str, str] | None = None
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
        self._powerpoint_poll = QTimer(self)
        self._powerpoint_poll.setInterval(500)
        self._powerpoint_poll.timeout.connect(self._poll_powerpoint)
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
        self.body.addWidget(self.pdf_controls)
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
        self._refresh()

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
        self._powerpoint_info = info
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

    def _powerpoint_lost(self, reason: str | None = None) -> None:
        self._powerpoint_poll.stop()
        self._queued_powerpoint_action = None
        self._queued_powerpoint_preview = None
        self._powerpoint_info = None
        self._powerpoint_path = None
        self.powerpoint_controls.set_info(None)
        self.powerpoint_status.setText(
            reason
            or "PowerPoint slideshow disconnected. Connect again to explicitly select a slideshow."
        )

    @Slot()
    def _disconnect_powerpoint(self) -> None:
        self._powerpoint_poll.stop()
        # Invalidate in-flight work: neither late success nor failure may restore
        # a connection or restart polling after the user explicitly disconnects.
        self._powerpoint_pending.clear()
        self._queued_powerpoint_action = None
        self._queued_powerpoint_preview = None
        self._queued_powerpoint_operation = None
        self._powerpoint_info = None
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
        self.surface.clear_selection()
        self._selection_changed(None)
        self.feedback.setText("Opening PDF…")
        self.executor.load_pdf(source, deck_id=deck_id, revision=revision)
        self._refresh()

    def load_manifest(self, path: Path) -> None:
        self.surface.clear_selection()
        self._selection_changed(None)
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
        )
        self.fullscreen.exited.connect(self._fullscreen_exited)
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

    def send(self, kind: str, params: dict[str, object] | None = None) -> None:
        manifest, slide = self.executor.manifest, self.surface.slide
        if manifest is None or slide is None or self._closing:
            return
        self._instance += 1
        self.executor.dispatch(
            {
                "version": "1.1",
                "instance_id": f"manual:{self._instance}",
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
        self._refresh()

    @Slot()
    def _refresh(self) -> None:
        loaded = self.executor.manifest is not None and not self._closing
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

    def shutdown(self) -> None:
        self._closing = True
        self._queued_powerpoint_action = None
        self._queued_powerpoint_preview = None
        self._queued_powerpoint_operation = None
        if self.fullscreen is not None:
            self.fullscreen.close()
        self.executor.close()
        self._powerpoint_poll.stop()
        self.powerpoint_runner.stop()
        self._refresh()
