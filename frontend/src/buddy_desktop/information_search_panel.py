"""A calm, animated side panel that keeps the live slide visible behind it."""

from urllib.parse import urlsplit

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QKeyEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from buddy_desktop.information_search_service import (
    InformationAnswer,
    InformationImage,
    InformationSource,
)
from buddy_desktop.visuals import command, icon

MARGIN = 10
SCROLLBAR = 8


class InformationSearchPanel(QFrame):
    close_requested = Signal()

    def __init__(self, *, reduce_motion: bool = False) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.reduce_motion = reduce_motion
        self._owner: QWidget | None = None
        self._area: QRect | None = None
        self._closing = False
        self.setObjectName("informationSearchPanel")
        self.setAccessibleName("Web search answer")
        self.setMinimumWidth(320)
        self.setMaximumWidth(480)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        # The panel is its own window: paint only the rounded shape so the black background
        # follows the rounded orange border instead of showing square corners (#250).
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # No drop shadow: Qt draws it as a rectangle, which shows outside the rounded
        # corners. The orange border alone outlines the panel (#250).

        # Results fill the panel: no logo, title, query echo or footer. The close button
        # floats over the top-right corner (#250).
        root = QVBoxLayout(self)
        root.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        root.setSpacing(6)
        self.close_button = QPushButton(self)
        command(self.close_button, "x", compact=True)
        self.close_button.setObjectName("informationSearchClose")
        self.close_button.setFixedSize(28, 28)
        self.close_button.setAccessibleName("Close web results and return to presentation")
        self.close_button.setToolTip(
            "Close (Esc). You can also say “close the panel” or “back to the presentation”."
        )
        self.close_button.clicked.connect(self.close_requested.emit)

        # Search and web icons, centred at the top of the panel.
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(10)
        header.addStretch(1)
        for name, label in (("search", "Search"), ("globe", "Web")):
            mark = QLabel()
            mark.setObjectName("informationSearchMark")
            mark.setPixmap(icon(name, "#F5843D").pixmap(22, 22))
            mark.setAccessibleName(label)
            header.addWidget(mark)
        header.addStretch(1)
        root.addLayout(header)

        self.state = QLabel("Searching the web…")
        self.state.setObjectName("informationSearchState")
        self.state.setWordWrap(True)
        root.addWidget(self.state)

        scroll_area = QScrollArea()
        scroll_area.setObjectName("informationSearchScroll")
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        # Content wraps to the panel width; never scroll sideways.
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll = scroll_area
        scroll_area.setStyleSheet("background: #000000;")
        scroll_area.viewport().setStyleSheet("background: #000000;")
        self.content = QWidget()
        self.content.setObjectName("informationSearchContent")
        self.content_layout = QVBoxLayout(self.content)
        # Leave room on the right of the first lines for the floating close button.
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(8)
        scroll_area.setWidget(self.content)
        root.addWidget(scroll_area, 1)

        self._animation = QPropertyAnimation(self, b"pos", self)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.finished.connect(self._animation_finished)
        self.close_requested.connect(self.close_panel)

    def present(self, owner: QWidget | QRect, query: str, *, images: bool = False) -> None:
        """Show over a Buddy window, or over a screen area such as PowerPoint's slideshow.

        Over another application's slideshow the panel stays on top without taking focus,
        so the presenter's clicker and keyboard keep controlling PowerPoint (#250).
        """
        if isinstance(owner, QRect):
            if self._owner is not None or self._area is None:
                self.hide()
                self.setParent(
                    None,
                    Qt.WindowType.Tool
                    | Qt.WindowType.FramelessWindowHint
                    | Qt.WindowType.WindowStaysOnTopHint
                    | Qt.WindowType.WindowDoesNotAcceptFocus,
                )
                self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
                self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self._owner, self._area = None, QRect(owner)
        elif self._owner is not owner:
            self.hide()
            self.setParent(owner, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self._owner, self._area = owner, None
        self._closing = False
        self._animation.stop()
        self._clear_content()
        self.setAccessibleDescription(("Images for " if images else "Web results for ") + query)
        self.state.setText("Finding images…" if images else "Searching the web…")
        self.state.show()
        self._place(show=True)

    def show_answer(self, result: InformationAnswer) -> None:
        """Answer text, a few images and source links; or an image grid on its own."""
        self._clear_content()
        self.state.hide()
        if result.answer:
            answer = QLabel(result.answer)
            answer.setObjectName("informationSearchAnswer")
            answer.setWordWrap(True)
            answer.setTextFormat(Qt.TextFormat.PlainText)
            answer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            answer.setContentsMargins(0, 0, 30, 0)
            self.content_layout.addWidget(answer)
        if result.images:
            self.content_layout.addWidget(self._image_grid(result.images))
        elif not result.answer:
            self.state.setText("No images could be loaded for that search.")
            self.state.show()
        if result.sources:
            sources_title = QLabel("Sources")
            sources_title.setObjectName("informationSearchSourcesTitle")
            self.content_layout.addWidget(sources_title)
            for source in result.sources:
                self.content_layout.addWidget(self._source_card(source))
        self.content_layout.addStretch(1)

    def _content_width(self) -> int:
        """Width available to panel content inside the margins and vertical scrollbar."""
        return self.width() - 2 * MARGIN - SCROLLBAR - 6

    def _image_grid(self, images: tuple[InformationImage, ...]) -> QWidget:
        grid_widget = QWidget()
        grid_widget.setObjectName("informationSearchImages")
        grid = QGridLayout(grid_widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(8)
        columns = 3
        # Fit three tiles inside the scroll area, leaving room for its vertical scrollbar.
        available = self._content_width() - 8 * (columns - 1)
        side = max(72, available // columns)
        for index, image in enumerate(images):
            pixmap = QPixmap()
            if image.data is None or not pixmap.loadFromData(image.data):
                continue
            # Fill the whole 4:3 tile (crop the centre) so no black bands are left.
            height = round(side * 0.75)
            ratio = self.devicePixelRatioF()
            target = QSize(round(side * ratio), round(height * ratio))
            filled = pixmap.scaled(
                target,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            filled = filled.copy(
                (filled.width() - target.width()) // 2,
                (filled.height() - target.height()) // 2,
                target.width(),
                target.height(),
            )
            filled.setDevicePixelRatio(ratio)
            tile = QToolButton()
            tile.setObjectName("informationSearchImage")
            tile.setIcon(filled)
            tile.setIconSize(QSize(side, height))
            tile.setFixedSize(side, height)
            tile.setCursor(Qt.CursorShape.PointingHandCursor)
            label = image.title or image.source or "Web image"
            tile.setAccessibleName("Open image source: " + label)
            tile.setToolTip(f"{label}\n{image.source}" if image.source else label)
            tile.clicked.connect(
                lambda _checked=False, url=image.source_url: self._open_source(url)
            )
            grid.addWidget(tile, index // columns, index % columns)
        return grid_widget

    def show_error(self, message: str) -> None:
        self._clear_content()
        self.state.setText(message)
        self.state.show()

    def close_panel(self) -> None:
        if not self.isVisible() or self._closing:
            return
        self._closing = True
        self._animation.stop()
        if self.reduce_motion:
            self.hide()
            return
        self._animation.setDuration(180)
        self._animation.setStartValue(self.pos())
        self._animation.setEndValue(QPoint(self.x() + 30, self.y()))
        self._animation.start()

    def _target_area(self) -> QRect | None:
        if self._owner is not None:
            return QRect(self._owner.mapToGlobal(QPoint(0, 0)), self._owner.size())
        return self._area

    def _place(self, *, show: bool) -> None:
        area = self._target_area()
        if area is None:
            return
        # The top half of the right-hand side of the presentation (#250).
        width = min(460, max(320, round(area.width() * 0.36)))
        height = max(260, (area.height() - 32) // 2)
        self.resize(width, height)
        global_top_left = QPoint(area.right() + 1 - width - 16, area.top() + 16)
        end = global_top_left
        if show and not self.isVisible():
            self.move(end.x() + (0 if self.reduce_motion else 28), end.y())
            self.show()
            self.raise_()
        if self.reduce_motion:
            self.move(end)
        else:
            self._animation.stop()
            self._animation.setDuration(230)
            self._animation.setStartValue(self.pos())
            self._animation.setEndValue(end)
            self._animation.start()

    def _source_card(self, source: InformationSource) -> QFrame:
        card = QFrame()
        card.setObjectName("informationSearchSource")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        title = QPushButton()
        command(title, "external-link")
        title.setObjectName("informationSearchSourceLink")
        # One-line titles must not widen the panel: shorten with “…” to the card width and
        # keep the full title in the tooltip and accessible name.
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        title.setMinimumWidth(0)
        title.ensurePolished()  # measure with the themed (bold) font
        text_width = max(80, self._content_width() - 24 - 28 - 32)
        title.setText(
            title.fontMetrics().elidedText(source.title, Qt.TextElideMode.ElideRight, text_width)
        )
        title.setAccessibleName("Open source: " + source.title)
        title.setToolTip(source.title + "\n" + source.url)
        title.clicked.connect(lambda _checked=False, url=source.url: self._open_source(url))
        layout.addWidget(title)
        snippet = QLabel(source.snippet)
        snippet.setObjectName("informationSearchSourceSnippet")
        snippet.setWordWrap(True)
        snippet.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(snippet)
        return card

    def scroll_by(self, direction: int) -> None:
        """Scroll the results by most of a page: 1 down, -1 up (voice "scroll down/up")."""
        bar = self._scroll.verticalScrollBar()
        step = max(60, round(self._scroll.viewport().height() * 0.8))
        bar.setValue(bar.value() + direction * step)

    def _clear_content(self) -> None:
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            if item is not None:
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()

    @staticmethod
    def _open_source(url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme == "https" and parts.hostname and not parts.username and not parts.password:
            QDesktopServices.openUrl(QUrl(url))

    def _animation_finished(self) -> None:
        if self._closing:
            self.hide()

    def paintEvent(self, event: QPaintEvent) -> None:
        """Rounded black panel with a Buddy-orange border; corners stay transparent."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#F5843D"))
        pen.setWidthF(2)
        painter.setPen(pen)
        painter.setBrush(QColor("#000000"))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 18, 18)
        painter.end()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.close_button.move(self.width() - self.close_button.width() - 6, 6)
        self.close_button.raise_()
        if self.isVisible() and not self._closing:
            self._place(show=False)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close_requested.emit()
            event.accept()
        else:
            super().keyPressEvent(event)
