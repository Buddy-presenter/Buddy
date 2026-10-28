"""PowerPoint slide controls, drawing tools, and a large static slide reference."""

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from buddy_desktop.powerpoint_contract import (
    PowerPointAction,
    PowerPointActionResult,
    PowerPointError,
    PowerPointFrame,
    PresentationInfo,
)
from buddy_desktop.slide_contract import Region
from buddy_desktop.slide_surface import viewport_mapping
from buddy_desktop.visuals import command


class PowerPointPreview(QWidget):
    selection_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.info: PresentationInfo | None = None
        self.image = QImage()
        self.selection: tuple[tuple[float, float], tuple[float, float]] | None = None
        self.path: tuple[tuple[float, float], ...] = ()
        self.mode = "highlight_region"
        self.interactive = False
        self._anchor: tuple[float, float] | None = None
        # A bigger static reference makes annotation placement precise on normal
        # laptop displays. The page scrolls when the larger preview needs the room.
        self.setMinimumSize(420, 480)
        self.setAccessibleName("PowerPoint slide reference; drag to draw or select an area")
        self.setToolTip("Static slide reference. PowerPoint remains the presentation display.")

    def reset(self, info: PresentationInfo | None) -> None:
        self.info = info
        self.image = QImage()
        self.selection = None
        self.path = ()
        self._anchor = None
        self.update()

    def point(self, position: QPointF) -> tuple[float, float] | None:
        info = self.info
        if info is None or self.image.isNull():
            return None
        mapping = viewport_mapping(
            Region(0, 0, info.slide_width, info.slide_height),
            self.width(),
            self.height(),
            self.devicePixelRatioF(),
        )
        x = (position.x() - mapping.offset_x) / mapping.scale
        y = (position.y() - mapping.offset_y) / mapping.scale
        if not 0 <= x <= info.slide_width or not 0 <= y <= info.slide_height:
            return None
        return (x, y)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self.interactive and event.button() == Qt.MouseButton.LeftButton:
            self._anchor = self.point(event.position())
            self.path = (self._anchor,) if self._anchor is not None else ()
            self.selection = None
            self.selection_changed.emit(self.path)
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.interactive and self._anchor is not None:
            end = self.point(event.position())
            if end is None:
                return
            previous = self.path[-1]
            if end != previous and (end[0] - previous[0]) ** 2 + (end[1] - previous[1]) ** 2 >= 4:
                self.path = (*self.path, end)
            self.selection = (self._anchor, end)
            self.selection_changed.emit(self.path)
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.mouseMoveEvent(event)
            self._anchor = None

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#000000"))
        info = self.info
        if info is None or self.image.isNull():
            painter.setPen(QColor("#B5B8BA"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Slide preview unavailable")
            return
        mapping = viewport_mapping(
            Region(0, 0, info.slide_width, info.slide_height),
            self.width(),
            self.height(),
            self.devicePixelRatioF(),
        )
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.translate(mapping.offset_x, mapping.offset_y)
        painter.scale(mapping.scale, mapping.scale)
        painter.setClipRect(QRectF(0, 0, info.slide_width, info.slide_height))
        painter.drawImage(QRectF(0, 0, info.slide_width, info.slide_height), self.image)
        if self.selection is not None:
            (x1, y1), (x2, y2) = self.selection
            painter.setPen(QPen(QColor("#B0A69A"), 1.5 / mapping.scale, Qt.PenStyle.DashLine))
            if self.mode == "draw_freehand" and len(self.path) > 1:
                for a, b in zip(self.path, self.path[1:], strict=False):
                    painter.drawLine(QPointF(*a), QPointF(*b))
            elif self.mode in {"draw_line", "draw_arrow"}:
                painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
            else:
                painter.drawRect(QRectF(QPointF(x1, y1), QPointF(x2, y2)).normalized())


_TOOLS = (
    ("Draw line", "draw_line"),
    ("Draw arrow", "draw_arrow"),
    ("Outline box", "draw_rectangle"),
    ("Rounded frame", "draw_rounded_rectangle"),
    ("Diamond frame", "draw_diamond"),
    ("Triangle frame", "draw_triangle"),
    ("Hexagon frame", "draw_hexagon"),
    ("Star emphasis", "draw_star"),
    ("Circle / oval", "draw_oval"),
    ("Underline area", "draw_underline"),
    ("Freehand", "draw_freehand"),
    ("Translucent highlighter", "highlight_region"),
    ("Filled rectangle", "filled_rectangle"),
    ("Filled oval", "filled_oval"),
    ("Text box", "text_box"),
    ("Callout", "callout"),
    ("Erase selected area", "erase_region"),
    ("PowerPoint pen (draw on slide)", "pointer_pen"),
    ("PowerPoint arrow pointer", "pointer_arrow"),
)

_COLORS = (
    ("Black", 0x000000),
    ("Buddy orange", 0x3D84F6),
    ("Red", 0x0000FF),
    ("Yellow", 0x00FFFF),
    ("Blue", 0xE84E2E),
    ("Green", 0x00A000),
    ("White", 0xFFFFFF),
)


class PowerPointControls(QWidget):
    action_requested = Signal(object)
    preview_requested = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.info: PresentationInfo | None = None
        self.busy = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("PowerPoint controls")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        self.previous = QPushButton("Previous slide")
        command(self.previous, "chevron-left")
        self.previous.clicked.connect(lambda: self.request("previous_slide"))
        self.next = QPushButton("Next slide")
        command(self.next, "chevron-right")
        self.next.clicked.connect(lambda: self.request("next_slide"))
        self.target = QSpinBox()
        self.target.setAccessibleName("PowerPoint slide number")
        self.go = QPushButton("Go to slide")
        command(self.go, "arrow-right")
        self.go.clicked.connect(lambda: self.request("go_to_slide"))
        self.counter = QLabel()
        row = QHBoxLayout()
        for widget in (self.previous, self.next, self.target, self.go, self.counter):
            row.addWidget(widget)
        layout.addLayout(row)

        self.preview = PowerPointPreview()
        self.preview.selection_changed.connect(self._selection_changed)
        layout.addWidget(self.preview, 1)
        self.hint = QLabel(
            "Drag on the large slide reference, then apply. PowerPoint's native pen draws "
            "directly on the slideshow window."
        )
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        row = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.setAccessibleName("PowerPoint annotation tool")
        for label, value in _TOOLS:
            self.mode.addItem(label, value)
        self.mode.setCurrentIndex(self.mode.findData("highlight_region"))
        self.mode.currentIndexChanged.connect(self._mode_changed)
        row.addWidget(self.mode, 3)
        self.color = QComboBox()
        self.color.setAccessibleName("PowerPoint annotation color")
        for label, color_value in _COLORS:
            self.color.addItem(label, color_value)
        self.color.setCurrentIndex(self.color.findData(0x00FFFF))
        row.addWidget(self.color, 1)
        self.transparency = QDoubleSpinBox()
        self.transparency.setAccessibleName("Fill transparency percent")
        self.transparency.setRange(0, 95)
        self.transparency.setDecimals(0)
        self.transparency.setSuffix("% clear")
        self.transparency.setValue(62)
        row.addWidget(self.transparency, 1)
        layout.addLayout(row)

        row = QHBoxLayout()
        self.text_input = QLineEdit()
        self.text_input.setAccessibleName("PowerPoint text annotation")
        self.text_input.setPlaceholderText("Text for a text box or callout")
        self.text_input.setMaxLength(300)
        self.text_input.textChanged.connect(self.refresh)
        row.addWidget(self.text_input, 2)
        self.apply = QPushButton("Apply annotation")
        command(self.apply, "highlighter", primary=True)
        self.apply.clicked.connect(lambda: self.request(self._selected_kind()))
        row.addWidget(self.apply, 2)
        self.undo = QPushButton("Undo last")
        command(self.undo, "rotate-ccw")
        self.undo.clicked.connect(lambda: self.request("undo_annotation"))
        row.addWidget(self.undo, 1)
        self.erase = QPushButton("Erase all annotations")
        command(self.erase, "rotate-ccw")
        self.erase.clicked.connect(lambda: self.request("erase_drawing"))
        row.addWidget(self.erase, 2)
        self.reset = QPushButton("Reset annotations")
        command(self.reset, "rotate-ccw")
        self.reset.clicked.connect(lambda: self.request("reset_view"))
        row.addWidget(self.reset, 2)
        self.refresh_preview = QPushButton("Refresh preview")
        command(self.refresh_preview, "refresh-cw")
        self.refresh_preview.clicked.connect(lambda: self.preview_requested.emit(self.info))
        row.addWidget(self.refresh_preview, 2)
        layout.addLayout(row)

        self.coordinates = QPushButton("Coordinates (slide points)")
        self.coordinates.setCheckable(True)
        self.coordinate_panel = QWidget()
        fields_layout = QGridLayout(self.coordinate_panel)
        self.fields: dict[str, QDoubleSpinBox] = {}
        for i, (name, caption) in enumerate(
            (("x1", "From X"), ("y1", "From Y"), ("x2", "To X"), ("y2", "To Y"))
        ):
            field = QDoubleSpinBox()
            field.setDecimals(2)
            field.setAccessibleName("PowerPoint " + caption + " in slide points")
            field.valueChanged.connect(self._fields_changed)
            self.fields[name] = field
            fields_layout.addWidget(QLabel(caption), 0, i)
            fields_layout.addWidget(field, 1, i)
        self.coordinates.toggled.connect(self.coordinate_panel.setVisible)
        layout.addWidget(self.coordinates)
        layout.addWidget(self.coordinate_panel)
        self.coordinate_panel.hide()

        self.feedback = QLabel()
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        self.feedback.setAccessibleName("PowerPoint action result")
        layout.addWidget(self.feedback)
        self._mode_changed()
        self.setVisible(False)

    def _selected_kind(self) -> str:
        value = str(self.mode.currentData())
        return {
            "pointer_pen": "set_pointer_tool",
            "pointer_arrow": "set_pointer_tool",
        }.get(value, value)

    def set_info(self, info: PresentationInfo | None) -> None:
        previous = self.info
        changed = previous is None or info is None or previous.slide_key != info.slide_key
        self.info = info
        self.setVisible(info is not None)
        if changed:
            self.preview.reset(info)
            for name, field in self.fields.items():
                field.blockSignals(True)
                field.setRange(
                    0,
                    (info.slide_width if name.startswith("x") else info.slide_height)
                    if info
                    else 0,
                )
                field.setValue(0)
                field.blockSignals(False)
        if info is not None:
            if previous is None or changed or previous.slide_index != info.slide_index:
                self.target.setRange(1, info.slide_count)
                self.target.setValue(info.slide_index)
            text = f"Slide {info.slide_index} of {info.slide_count}"
            if self.counter.text() != text:
                self.counter.setText(text)
        self.refresh()

    def set_frame(self, frame: PowerPointFrame) -> None:
        if self.info is None or frame.info.slide_key != self.info.slide_key:
            return
        image = QImage.fromData(frame.png)
        if image.isNull() or image.width() > 4096 or image.height() > 4096:
            self.feedback.setText(
                "PowerPoint returned an invalid slide preview. Try Refresh preview."
            )
            return
        self.preview.image = image
        self.preview.path = ()
        self.preview.selection = None
        self.preview.update()
        self.refresh()

    def _mode_changed(self) -> None:
        value = str(self.mode.currentData())
        self.preview.mode = value
        pointer = value.startswith("pointer_")
        self.apply.setText(
            "Activate PowerPoint " + value.removeprefix("pointer_")
            if pointer
            else "Apply annotation"
        )
        self.text_input.setVisible(value in {"text_box", "callout"})
        self.transparency.setVisible(
            value in {"highlight_region", "filled_rectangle", "filled_oval"}
        )
        self.preview.update()
        if pointer:
            self.hint.setText(
                "Activate the native pen, then draw with the mouse over the PowerPoint slideshow. "
                "Select PowerPoint arrow pointer to return to navigation."
            )
        else:
            self.hint.setText(
                "Drag on the large slide reference to draw or select an area, then apply the "
                "annotation. Erase selected area removes Buddy's annotations there."
            )
        self.refresh()

    def _selection_changed(self, selection: object) -> None:
        points = selection if isinstance(selection, tuple) else ()
        self.preview.path = points
        values = (0.0, 0.0, 0.0, 0.0)
        if len(points) >= 2:
            (x1, y1), (x2, y2) = points[0], points[-1]
            values = (x1, y1, x2, y2)
            self.preview.selection = ((x1, y1), (x2, y2))
        else:
            self.preview.selection = None
        for field, value in zip(self.fields.values(), values, strict=True):
            field.blockSignals(True)
            field.setValue(value)
            field.blockSignals(False)
        self.refresh()

    def _fields_changed(self) -> None:
        x1, y1, x2, y2 = (field.value() for field in self.fields.values())
        self.preview.selection = ((x1, y1), (x2, y2))
        self.preview.path = ((x1, y1), (x2, y2))
        self.preview.update()
        self.refresh()

    def _action(self, kind: str) -> PowerPointAction:
        if self.info is None:
            raise PowerPointError("Connect to a PowerPoint slideshow first.")
        selection = self.preview.selection
        points = self.preview.path
        region = None
        if selection is not None:
            x1, y1 = selection[0]
            x2, y2 = selection[1]
            region = Region(min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
        color = int(self.color.currentData())
        transparency = float(self.transparency.value()) / 100
        pointer_tool = None
        selected = str(self.mode.currentData())
        if selected == "pointer_pen":
            pointer_tool = "pen"
        elif selected == "pointer_arrow":
            pointer_tool = "arrow"
        if kind in {"draw_line", "draw_arrow"}:
            points = (points[0], points[-1]) if len(points) >= 2 else ()
        if kind == "draw_freehand" and len(points) < 2:
            points = ()
        return PowerPointAction(
            kind,
            self.info,
            slide_number=self.target.value() if kind == "go_to_slide" else None,
            points=points if kind in {"draw_line", "draw_arrow", "draw_freehand"} else (),
            region=region
            if kind
            in {
                "draw_rectangle",
                "draw_oval",
                "draw_rounded_rectangle",
                "draw_diamond",
                "draw_triangle",
                "draw_hexagon",
                "draw_star",
                "draw_underline",
                "erase_region",
                "highlight_region",
                "filled_rectangle",
                "filled_oval",
                "text_box",
                "callout",
            }
            else None,
            color=color,
            fill_transparency=transparency,
            text=self.text_input.text() if kind in {"text_box", "callout"} else "",
            pointer_tool=pointer_tool if kind == "set_pointer_tool" else None,
        )

    def request(self, kind: str) -> None:
        if self.busy:
            return
        try:
            action = self._action(kind)
            action.validate()
        except PowerPointError as exc:
            self.feedback.setText(str(exc))
            return
        self.action_requested.emit(action)

    def completed(self, result: PowerPointActionResult) -> None:
        if self.info is None or result.info.slide_key != self.info.slide_key:
            return
        if result.action.kind in {
            "erase_region",
            "erase_drawing",
            "reset_view",
            "undo_annotation",
        }:
            self.preview.selection = None
            self.preview.path = ()
            self._selection_changed(None)
        self.preview.update()
        self.feedback.setText("PowerPoint action completed.")
        self.refresh()

    def refresh(self) -> None:
        info = self.info
        ready = info is not None and not self.busy
        self.previous.setEnabled(ready and info is not None and info.slide_index > 1)
        self.next.setEnabled(ready and info is not None and info.slide_index < info.slide_count)
        for widget in (
            self.target,
            self.go,
            self.erase,
            self.reset,
            self.undo,
            self.refresh_preview,
            self.mode,
            self.color,
            self.transparency,
            self.text_input,
        ):
            widget.setEnabled(ready)
        for field in self.fields.values():
            field.setEnabled(ready and not self.preview.image.isNull())
        self.preview.interactive = ready and not self.preview.image.isNull()
        selected = self.preview.interactive
        tool = str(self.mode.currentData())
        if tool in {"pointer_pen", "pointer_arrow"}:
            selected = ready
        try:
            self._action(self._selected_kind()).validate()
        except PowerPointError:
            selected = False
        self.apply.setEnabled(selected)
