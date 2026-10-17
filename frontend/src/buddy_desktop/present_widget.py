"""Local rendered-slide controls using the production six-command executor."""

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, Signal, Slot
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
)

from buddy_desktop.fullscreen import FullscreenPresentation
from buddy_desktop.slide_contract import Region
from buddy_desktop.slide_executor import Completion, SlideExecutor
from buddy_desktop.slide_surface import SlideSurface
from buddy_desktop.views import Page


class SelectionSurface(SlideSurface):
    selection_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.selection: Region | None = None
        self.selectable = False
        self._anchor: QPointF | None = None
        self.setMinimumSize(240, 180)
        self.setAccessibleName("Slide preview; use the region fields for keyboard selection")

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
        super().__init__("Present", "Open a PDF, select any area, and control your presentation.")
        self.body.setSpacing(10)
        self._closing = False
        self.fullscreen: FullscreenPresentation | None = None
        self._instance = 0
        self._committed_key: tuple[str, str, str] | None = None
        self.surface = SelectionSurface()
        self.executor = SlideExecutor(self.surface)
        self.executor.completed.connect(self._completed)
        self.executor.idle.connect(self._refresh)
        self.open_button = QPushButton("Open PDF…")
        self.open_button.clicked.connect(self._choose_manifest)
        self.previous_button = QPushButton("Previous")
        self.previous_button.clicked.connect(lambda: self.send("previous_slide"))
        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(lambda: self.send("next_slide"))
        self.target = QComboBox()
        self.target.setAccessibleName("Slide to go to")
        self.go_button = QPushButton("Go to slide")
        self.go_button.clicked.connect(self._go)
        self.counter = QLabel("No slides loaded")
        opening = QHBoxLayout()
        opening.addWidget(self.open_button)
        opening.addStretch()
        opening.addWidget(self.counter)
        self.fullscreen_button = QPushButton("Full screen")
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
        hint = QLabel(
            "Drag a rectangle around a word, phrase, formula, or any content. "
            "You can also enter the region below using the keyboard."
        )
        hint.setWordWrap(True)
        self.body.addWidget(hint)
        form = QGridLayout()
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
            form.addWidget(caption, 0, column)
            form.addWidget(field, 1, column)
            self.fields[name] = field
        self.body.addLayout(form)
        commands = QHBoxLayout()
        self.zoom_button = QPushButton("Zoom to selection")
        self.zoom_button.clicked.connect(lambda: self._region_action("zoom_region"))
        self.highlight_button = QPushButton("Highlight selection")
        self.highlight_button.clicked.connect(lambda: self._region_action("highlight_region"))
        self.reset_button = QPushButton("Reset")
        self.reset_button.clicked.connect(lambda: self.send("reset_view"))
        for button in (self.zoom_button, self.highlight_button, self.reset_button):
            commands.addWidget(button)
        self.body.addLayout(commands)
        self.feedback = QLabel(
            "Open a PDF to begin. PPTX playback is not supported yet; export it to PDF first."
        )
        self.feedback.setWordWrap(True)
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setAccessibleName("Presentation status")
        self.body.addWidget(self.feedback)
        self.surface.selection_changed.connect(self._selection_changed)
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
        ready = loaded and not self.executor.busy
        self.open_button.setEnabled(not self.executor.busy and not self._closing)
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
        self._refresh()
