"""Reusable, accessible shell views. Domain workflows are separate features."""

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from buddy_desktop.visuals import BuddyMark, command, icon


class StatusLabel(QLabel):
    """Progress and errors that take no space when there is nothing to say."""

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setVisible(bool(text))


class StatePanel(QWidget):
    """One consistent surface for loading, unavailable, success, and error states."""

    retry_requested = Signal()
    state_changed = Signal()

    def __init__(self, parent: QWidget | None = None, *, centered: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("statePanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        alignment = Qt.AlignmentFlag.AlignHCenter if centered else Qt.AlignmentFlag.AlignLeft
        layout = QVBoxLayout(self)
        if centered:
            layout.setContentsMargins(24, 40, 24, 8)
        else:
            layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.title = QLabel()
        self.title.setObjectName("stateTitle" if centered else "sectionTitle")
        self.title.setWordWrap(True)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.message = QLabel()
        self.message.setObjectName("muted")
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        for label in (self.title, self.message):
            label.setAlignment(alignment)
        self.retry = QPushButton("Try again")
        command(self.retry, "refresh-cw")
        self.retry.setAccessibleName("Retry backend connection")
        self.retry.clicked.connect(self.retry_requested.emit)
        layout.addWidget(self.title)
        layout.addWidget(self.message)
        layout.addSpacing(10)
        layout.addWidget(self.retry, alignment=alignment)

    def show_state(self, title: str, message: str, *, retry: bool = False) -> None:
        self.title.setText(title)
        self.message.setText(message)
        self.message.setVisible(bool(message))
        self.setAccessibleName(title)
        self.setAccessibleDescription(message)
        self.retry.setVisible(retry)
        self.show()
        self.state_changed.emit()


class Page(QWidget):
    """A routed page. The window header shows the route title, so pages do not repeat it."""

    def __init__(self, title: str, description: str) -> None:
        super().__init__()
        self.setAccessibleName(title)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(32, 8, 32, 28)
        self.body.setSpacing(16)
        introduction = QLabel(description)
        introduction.setObjectName("pageDescription")
        introduction.setWordWrap(True)
        introduction.setVisible(bool(description))
        self.body.addWidget(introduction)


def centered_column(parent_layout: QVBoxLayout, max_width: int) -> QVBoxLayout:
    """A column that fills the page up to max_width and stays horizontally centered."""
    column = QWidget()
    column.setMaximumWidth(max_width)
    row = QHBoxLayout()
    row.addStretch(1)
    row.addWidget(column, 100)
    row.addStretch(1)
    parent_layout.addLayout(row)
    layout = QVBoxLayout(column)
    layout.setContentsMargins(0, 0, 0, 0)
    return layout


def group(title: str, parent_layout: QVBoxLayout) -> QVBoxLayout:
    """A titled, rounded settings group in the style of native preference panes."""
    heading = QLabel(title)
    heading.setObjectName("groupTitle")
    parent_layout.addWidget(heading)
    card = QWidget()
    card.setObjectName("group")
    card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    layout = QVBoxLayout(card)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(10)
    parent_layout.addWidget(card)
    return layout


def divider() -> QFrame:
    line = QFrame()
    line.setObjectName("divider")
    line.setFrameShape(QFrame.Shape.NoFrame)
    return line


class StartCard(QPushButton):
    """A whole-surface button: icon, title and one line of detail."""

    def __init__(self, image: str, title: str, detail: str) -> None:
        super().__init__()
        self.setObjectName("startCard")
        self.setAccessibleName(title)
        self.setAccessibleDescription(detail)
        self.setToolTip(detail)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(144)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        content = QVBoxLayout(self)
        content.setContentsMargins(20, 18, 20, 18)
        content.setSpacing(4)
        glyph = QLabel()
        glyph.setPixmap(icon(image, "#FFB27F").pixmap(QSize(22, 22)))
        content.addWidget(glyph)
        content.addStretch()
        heading = QLabel(title)
        heading.setObjectName("cardTitle")
        content.addWidget(heading)
        self.detail = QLabel(detail)
        self.detail.setObjectName("muted")
        self.detail.setWordWrap(True)
        content.addWidget(self.detail)
        for label in (glyph, heading, self.detail):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    # QPushButton sizes itself from text and icon only; size from the card content instead.
    def sizeHint(self) -> QSize:
        return QSize(300, 144)

    def minimumSizeHint(self) -> QSize:
        return QSize(200, 144)


class HomePage(Page):
    route_requested = Signal(int)

    def __init__(self, api_url: str) -> None:
        super().__init__("", "")
        self.setObjectName("homePage")
        # Home is a starting point for work; account and service controls live in
        # the profile menu and Settings, respectively.
        self.body.addStretch(2)
        layout = centered_column(self.body, 720)
        layout.setSpacing(0)
        self.mascot = BuddyMark(88)
        layout.addWidget(self.mascot, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(18)
        title = QLabel("What are you presenting today?")
        title.setObjectName("heroTitle")
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(title)
        layout.addSpacing(8)
        # Shows the open deck's position once slides are loaded.
        self.workspace_state = QLabel()
        self.workspace_state.setObjectName("muted")
        self.workspace_state.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.workspace_state.hide()
        layout.addWidget(self.workspace_state)
        layout.addSpacing(28)
        actions = QHBoxLayout()
        actions.setSpacing(14)
        self.present_action = StartCard(
            "presentation", "Present a PDF", "Open a file from this computer. No account needed."
        )
        self.library_action = StartCard(
            "library-big", "Library", "Upload and manage the decks saved to your account."
        )
        for button, route in ((self.present_action, 3), (self.library_action, 1)):
            button.clicked.connect(lambda checked=False, row=route: self.route_requested.emit(row))
            actions.addWidget(button, 1)
        layout.addLayout(actions)
        self.body.addStretch(3)


class UnavailablePage(Page):
    def __init__(self, title: str, message: str) -> None:
        super().__init__(title, "")
        self.state = StatePanel(centered=True)
        self.state.show_state("This workspace is not available yet", message)
        self.body.addWidget(self.state)
        self.body.addStretch()


class SettingsPage(Page):
    save_requested = Signal(str)
    check_requested = Signal()

    def __init__(self, api_url: str, *, environment_override: bool) -> None:
        super().__init__("Settings", "")
        self.body.setSpacing(10)
        appearance = group("Appearance", self.body)
        self.reduce_motion = QCheckBox("Reduce motion")
        self.reduce_motion.setAccessibleName("Reduce mascot motion")
        appearance.addWidget(self.reduce_motion)
        motion_hint = QLabel("Keeps Buddy still while work is in progress.")
        motion_hint.setObjectName("caption")
        appearance.addWidget(motion_hint)
        self.body.addSpacing(14)
        connection = group("Connection", self.body)
        label = QLabel("&Backend URL")
        connection.addWidget(label)
        self.url = QLineEdit(api_url)
        self.url.setAccessibleName("Backend URL")
        self.url.setPlaceholderText("https://your-buddy-backend.example")
        self.url.setEnabled(not environment_override)
        self.url.setMaxLength(2048)
        label.setBuddy(self.url)
        entry = QHBoxLayout()
        entry.setSpacing(8)
        entry.addWidget(self.url, 1)
        self.save = QPushButton("Save")
        command(self.save, "save", primary=True)
        self.save.setAccessibleName("Save settings")
        self.save.setEnabled(not environment_override)
        self.save.clicked.connect(lambda: self.save_requested.emit(self.url.text()))
        self.url.returnPressed.connect(self.save.click)
        entry.addWidget(self.save)
        connection.addLayout(entry)
        self.feedback = QLabel(
            "Managed by BUDDY_API_URL. Update the environment and restart to change it."
            if environment_override
            else "Use HTTPS, or HTTP for a local development server."
        )
        self.feedback.setObjectName("caption")
        self.feedback.setWordWrap(True)
        self.feedback.setAccessibleName("Settings status")
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        connection.addWidget(self.feedback)
        connection.addSpacing(6)
        connection.addWidget(divider())
        connection.addSpacing(6)
        status = QHBoxLayout()
        status.setSpacing(16)
        details = QVBoxLayout()
        details.setSpacing(2)
        self.connection = StatePanel()
        self.connection.show_state("Connection not checked", "Check that Buddy can reach it.")
        self.connection.retry_requested.connect(self.check_requested.emit)
        details.addWidget(self.connection)
        self.endpoint = QLabel()
        self.endpoint.setObjectName("caption")
        self.endpoint.setTextFormat(Qt.TextFormat.PlainText)
        self.endpoint.setAccessibleName("Configured backend address")
        self.endpoint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        details.addWidget(self.endpoint)
        status.addLayout(details, 1)
        self.check = QPushButton("Check connection")
        command(self.check, "wifi")
        self.check.clicked.connect(self.check_requested.emit)
        status.addWidget(self.check, alignment=Qt.AlignmentFlag.AlignTop)
        connection.addLayout(status)
        # The state's own retry replaces this command after a failure.
        self.connection.state_changed.connect(
            lambda: self.check.setVisible(not self.connection.retry.isVisibleTo(self.connection))
        )
        self._endpoint_url = api_url
        self.url.textChanged.connect(self._endpoint_visibility)
        self.set_endpoint(api_url)
        self.body.addStretch()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.set_endpoint(self._endpoint_url)

    def set_endpoint(self, url: str) -> None:
        self._endpoint_url = url
        self.endpoint.setToolTip(url)
        self.endpoint.setAccessibleDescription(url)
        prefix = "In use: "
        metrics = self.endpoint.fontMetrics()
        self.endpoint.setText(
            prefix
            + metrics.elidedText(
                url,
                Qt.TextElideMode.ElideMiddle,
                max(1, self.endpoint.width() - metrics.horizontalAdvance(prefix)),
            )
        )
        self._endpoint_visibility()

    def _endpoint_visibility(self, *_args: object) -> None:
        # Only worth showing while the field holds an unsaved, different address.
        self.endpoint.setVisible(self.url.text().strip().rstrip("/") != self._endpoint_url)
