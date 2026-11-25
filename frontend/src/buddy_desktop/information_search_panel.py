"""A calm, animated side panel that keeps the live slide visible behind it."""

from urllib.parse import urlsplit

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QKeyEvent, QResizeEvent
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from buddy_desktop.information_search_service import InformationAnswer, InformationSource
from buddy_desktop.visuals import BuddyMark, command


class InformationSearchPanel(QFrame):
    close_requested = Signal()

    def __init__(self, *, reduce_motion: bool = False) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.reduce_motion = reduce_motion
        self._owner: QWidget | None = None
        self._closing = False
        self.setObjectName("informationSearchPanel")
        self.setAccessibleName("Web search answer")
        self.setMinimumWidth(320)
        self.setMaximumWidth(480)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(34)
        shadow.setOffset(-8, 0)
        shadow.setColor(QColor(0, 0, 0, 180))
        self.setGraphicsEffect(shadow)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 20, 20)
        root.setSpacing(14)
        header = QHBoxLayout()
        header.setSpacing(10)
        header.addWidget(BuddyMark(26))
        heading = QLabel("Quick answer")
        heading.setObjectName("informationSearchTitle")
        header.addWidget(heading, 1)
        self.close_button = QPushButton("×")
        command(self.close_button, "x")
        self.close_button.setObjectName("quietButton")
        self.close_button.setAccessibleName("Close web answer and return to presentation")
        self.close_button.clicked.connect(self.close_requested.emit)
        header.addWidget(self.close_button)
        root.addLayout(header)

        self.query = QLabel()
        self.query.setObjectName("informationSearchQuery")
        self.query.setWordWrap(True)
        self.query.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.query)

        self.state = QLabel("Searching the web…")
        self.state.setObjectName("informationSearchState")
        self.state.setWordWrap(True)
        root.addWidget(self.state)

        scroll_area = QScrollArea()
        scroll_area.setObjectName("informationSearchScroll")
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.content = QWidget()
        self.content.setObjectName("informationSearchContent")
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 4, 0)
        self.content_layout.setSpacing(12)
        scroll_area.setWidget(self.content)
        root.addWidget(scroll_area, 1)

        self.dismiss_hint = QLabel(
            "Say “back to the presentation” or close this panel to continue."
        )
        self.dismiss_hint.setObjectName("informationSearchHint")
        self.dismiss_hint.setWordWrap(True)
        root.addWidget(self.dismiss_hint)

        self._animation = QPropertyAnimation(self, b"pos", self)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.finished.connect(self._animation_finished)
        self.close_requested.connect(self.close_panel)

    def present(self, owner: QWidget, query: str) -> None:
        if self._owner is not owner:
            self.setParent(owner, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
            self._owner = owner
        self._closing = False
        self._animation.stop()
        self._clear_content()
        self.query.setText(query)
        self.query.show()
        self.state.setText("Searching the web…")
        self.state.show()
        self.dismiss_hint.show()
        self._place(show=True)

    def show_answer(self, result: InformationAnswer) -> None:
        self._clear_content()
        self.state.hide()
        answer = QLabel(result.answer)
        answer.setObjectName("informationSearchAnswer")
        answer.setWordWrap(True)
        answer.setTextFormat(Qt.TextFormat.PlainText)
        answer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.content_layout.addWidget(answer)
        sources_title = QLabel("Sources")
        sources_title.setObjectName("informationSearchSourcesTitle")
        self.content_layout.addWidget(sources_title)
        for source in result.sources:
            self.content_layout.addWidget(self._source_card(source))
        self.content_layout.addStretch(1)

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

    def _place(self, *, show: bool) -> None:
        owner = self._owner
        if owner is None:
            return
        width = min(460, max(320, round(owner.width() * 0.36)))
        height = max(360, owner.height() - 48)
        self.resize(width, height)
        global_top_left = owner.mapToGlobal(QPoint(owner.width() - width - 18, 24))
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
        title = QPushButton(source.title)
        command(title, "external-link")
        title.setObjectName("informationSearchSourceLink")
        title.setAccessibleName("Open source: " + source.title)
        title.setToolTip(source.url)
        title.clicked.connect(lambda _checked=False, url=source.url: self._open_source(url))
        layout.addWidget(title)
        snippet = QLabel(source.snippet)
        snippet.setObjectName("informationSearchSourceSnippet")
        snippet.setWordWrap(True)
        snippet.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(snippet)
        return card

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

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self.isVisible() and not self._closing:
            self._place(show=False)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close_requested.emit()
            event.accept()
        else:
            super().keyPressEvent(event)
