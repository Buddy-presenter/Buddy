"""Local rendered-slide controls using the production six-command executor."""

import sys
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
from buddy_desktop.slide_contract import Region
from buddy_desktop.slide_executor import Completion, SlideExecutor
from buddy_desktop.slide_surface import SlideSurface
from buddy_desktop.tasks import TaskRunner
from buddy_desktop.views import Page
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
        empty_layout.addWidget(BuddyMark(64), alignment=Qt.AlignmentFlag.AlignCenter)
        empty_title = QLabel("No slides loaded")
        empty_title.setObjectName("sectionTitle")
        empty_layout.addWidget(empty_title, alignment=Qt.AlignmentFlag.AlignCenter)
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
        super().__init__(
            "Present", "Present PDFs in Buddy, or launch desktop PowerPoint on Windows."
        )
        self.body.setSpacing(10)
        self._closing = False
        self.fullscreen: FullscreenPresentation | None = None
        self._instance = 0
        self._committed_key: tuple[str, str, str] | None = None
        self.powerpoint = PowerPointAutomation()
        self.powerpoint_runner = TaskRunner(self)
        self.powerpoint_runner.succeeded.connect(self._powerpoint_succeeded)
        self.powerpoint_runner.failed.connect(self._powerpoint_failed)
        self._powerpoint_pending: dict[int, tuple[str, str | None]] = {}
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
        self.go_button = QPushButton("Go to slide")
        command(self.go_button, "arrow-right", compact=True)
        self.go_button.clicked.connect(self._go)
        self.counter = QLabel("No slides loaded")
        opening = QHBoxLayout()
        opening.addWidget(self.open_button)
        opening.addStretch()
        opening.addWidget(self.counter)
        self.fullscreen_button = QPushButton("Full screen")
        command(self.fullscreen_button, "scan")
        self.fullscreen_button.setToolTip("F11: full screen. Esc: exit. Arrows or Space: navigate.")
        self.fullscreen_button.clicked.connect(self.enter_fullscreen)
        opening.addWidget(self.fullscreen_button)
        self.body.addLayout(opening)
        navigation = QHBoxLayout()
        for widget in (
            self.previous_button,
            self.next_button,
            self.target,
            self.go_button,
        ):
            navigation.addWidget(widget)
        self.body.addLayout(navigation)
        self.body.addWidget(self.surface, 1)
        self.surface.setToolTip(
            "Drag to select a region, or open Coordinates for numeric selection"
        )
        self.coordinates_button = QPushButton("Coordinates")
        command(self.coordinates_button, "scan")
        self.coordinates_button.setCheckable(True)
        self.coordinates_button.setAccessibleName("Show region coordinates")
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
        self.body.addWidget(self.coordinate_panel)
        self.coordinate_panel.hide()
        self.coordinates_button.toggled.connect(self.coordinate_panel.setVisible)
        commands = QHBoxLayout()
        self.zoom_button = QPushButton("Zoom to selection")
        command(self.zoom_button, "scan")
        self.zoom_button.clicked.connect(lambda: self._region_action("zoom_region"))
        self.highlight_button = QPushButton("Highlight selection")
        command(self.highlight_button, "highlighter", primary=True)
        self.highlight_button.clicked.connect(lambda: self._region_action("highlight_region"))
        self.reset_button = QPushButton("Reset")
        command(self.reset_button, "rotate-ccw", compact=True)
        self.reset_button.clicked.connect(lambda: self.send("reset_view"))
        for button in (self.zoom_button, self.highlight_button, self.reset_button):
            commands.addWidget(button)
        commands.addWidget(self.coordinates_button)
        self.body.addLayout(commands)
        self.powerpoint_open_button = QPushButton("Open PowerPoint…")
        command(self.powerpoint_open_button, "presentation", primary=True)
        self.powerpoint_open_button.setAccessibleName("Open a PowerPoint presentation")
        self.powerpoint_open_button.clicked.connect(self._choose_powerpoint)
        self.powerpoint_connect_button = QPushButton("Connect to slideshow…")
        command(self.powerpoint_connect_button, "link-2")
        self.powerpoint_connect_button.clicked.connect(self._list_powerpoint_slideshows)
        self.powerpoint_disconnect_button = QPushButton("Disconnect")
        command(self.powerpoint_disconnect_button, "x", compact=True)
        self.powerpoint_disconnect_button.clicked.connect(self._disconnect_powerpoint)
        powerpoint_row = QHBoxLayout()
        powerpoint_row.addWidget(self.powerpoint_open_button)
        powerpoint_row.addWidget(self.powerpoint_connect_button)
        powerpoint_row.addWidget(self.powerpoint_disconnect_button)
        powerpoint_row.addStretch()
        self.body.addLayout(powerpoint_row)
        self.powerpoint_status = QLabel()
        self.powerpoint_status.setObjectName("muted")
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
        self.body.addWidget(self.powerpoint_status)
        self.feedback = QLabel("Open a PDF to begin, or start a PowerPoint slideshow on Windows.")
        self.feedback.setWordWrap(True)
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setAccessibleName("Presentation status")
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
        if sys.platform != "win32" or self._closing or self.powerpoint_runner.busy:
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
            request = self.powerpoint_runner.submit(operation)
        except (KeyError, RuntimeError):
            self.powerpoint_status.setText("PowerPoint is busy. Try again when it is ready.")
            return
        self._powerpoint_pending[request] = (action, path)
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
                self._attach_powerpoint_info(result)
            else:
                self._powerpoint_lost()
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
        else:
            self.powerpoint_status.setText(message)
        self._refresh()

    def _attach_powerpoint_info(self, info: PresentationInfo) -> None:
        self._powerpoint_info = info
        self._powerpoint_path = info.path
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
        self._powerpoint_info = None
        self._powerpoint_path = None
        self.powerpoint_status.setText(
            reason
            or "PowerPoint slideshow disconnected. Connect again to explicitly select a slideshow."
        )

    @Slot()
    def _disconnect_powerpoint(self) -> None:
        self._powerpoint_poll.stop()
        self._powerpoint_info = None
        self._powerpoint_path = None
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
            else "Ready."
        )
        self._refresh()

    @Slot()
    def _refresh(self) -> None:
        loaded = self.executor.manifest is not None and not self._closing
        self.surface.empty.setVisible(self.surface.slide is None)
        ready = loaded and not self.executor.busy
        self.open_button.setEnabled(not self.executor.busy and not self._closing)
        # Fast status checks should not repeatedly disable and re-enable the visible
        # controls. Only user-initiated launch/connect/list operations lock them.
        powerpoint_action_busy = self._closing or any(
            action != "status" for action, _path in self._powerpoint_pending.values()
        )
        self.powerpoint_open_button.setEnabled(
            sys.platform == "win32" and not powerpoint_action_busy
        )
        self.powerpoint_connect_button.setEnabled(
            sys.platform == "win32" and not powerpoint_action_busy
        )
        self.powerpoint_disconnect_button.setEnabled(
            self._powerpoint_info is not None and not powerpoint_action_busy
        )
        self.fullscreen_button.setEnabled(loaded)
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
        if self.fullscreen is not None:
            self.fullscreen.close()
        self.executor.close()
        self._powerpoint_poll.stop()
        self.powerpoint_runner.stop()
        self._refresh()
