"""Reusable, accessible shell views. Domain workflows are separate features."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from buddy_desktop.visuals import BuddyMark, command


class StatePanel(QWidget):
    """One consistent surface for loading, unavailable, success, and error states."""

    retry_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statePanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 4, 12, 4)
        layout.setSpacing(10)
        self.title = QLabel()
        self.title.setObjectName("stateTitle")
        self.title.setWordWrap(True)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.retry = QPushButton("Try again")
        command(self.retry, "refresh-cw")
        self.retry.setAccessibleName("Retry backend connection")
        self.retry.clicked.connect(self.retry_requested.emit)
        layout.addWidget(self.title)
        layout.addWidget(self.message)
        layout.addWidget(self.retry, alignment=Qt.AlignmentFlag.AlignLeft)

    def show_state(self, title: str, message: str, *, retry: bool = False) -> None:
        self.title.setText(title)
        self.message.setText(message)
        self.setAccessibleName(title)
        self.setAccessibleDescription(message)
        self.retry.setVisible(retry)


class Page(QWidget):
    def __init__(self, title: str, description: str) -> None:
        super().__init__()
        self.setAccessibleName(title)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(28, 24, 28, 24)
        self.body.setSpacing(18)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        heading.setWordWrap(True)
        introduction = QLabel(description)
        introduction.setObjectName("pageDescription")
        introduction.setWordWrap(True)
        introduction.setVisible(bool(description))
        self.body.addWidget(heading)
        self.body.addWidget(introduction)


class HomePage(Page):
    check_requested = Signal()
    route_requested = Signal(int)

    def __init__(self, api_url: str) -> None:
        super().__init__(
            "A little preparation.\nA lot more presence.",
            "Your presentations, with Buddy beside you.",
        )
        self.setObjectName("homePage")
        welcome = QHBoxLayout()
        self.mascot = BuddyMark(124)
        welcome.addWidget(self.mascot)
        greeting = QVBoxLayout()
        badge = QLabel("Ready when you are")
        badge.setObjectName("eyebrow")
        greeting.addWidget(badge)
        title = QLabel("Make room for your next idea.")
        title.setObjectName("sectionTitle")
        title.setWordWrap(True)
        greeting.addWidget(title)
        self.workspace_state = QLabel("Start with a PDF, or bring your PowerPoint into Library.")
        self.workspace_state.setObjectName("muted")
        self.workspace_state.setWordWrap(True)
        greeting.addWidget(self.workspace_state)
        welcome.addLayout(greeting, 1)
        self.body.addLayout(welcome)
        actions = QGridLayout()
        actions.setSpacing(12)
        self.library_action = QPushButton("Your library")
        self.present_action = QPushButton("Open the stage")
        for column, (button, image, route) in enumerate(
            ((self.library_action, "library-big", 1), (self.present_action, "presentation", 2))
        ):
            button.setObjectName("workspaceAction")
            command(button, image)
            button.clicked.connect(lambda checked=False, row=route: self.route_requested.emit(row))
            actions.addWidget(button, 0, column)
        self.body.addLayout(actions)
        guidance = QLabel(
            "Import privately in Library. Open a local PDF in Present, even without an account."
        )
        guidance.setObjectName("muted")
        guidance.setWordWrap(True)
        self.body.addWidget(guidance)
        self.account_slot = QWidget()
        self.account_layout = QVBoxLayout(self.account_slot)
        self.account_layout.setContentsMargins(0, 16, 0, 0)
        self.connection_section = QWidget()
        connection_layout = QVBoxLayout(self.connection_section)
        connection_layout.setContentsMargins(0, 16, 0, 0)
        section = QLabel("Connection")
        section.setObjectName("sectionTitle")
        connection_layout.addWidget(section)
        self.connection = StatePanel()
        self.connection.show_state(
            "Connection not checked", "Check that your configured Buddy backend is available."
        )
        self.endpoint = QLabel(api_url)
        self.endpoint.setWordWrap(True)
        self.endpoint.setTextFormat(Qt.TextFormat.PlainText)
        self.endpoint.setAccessibleName("Configured backend address")
        self.endpoint.setObjectName("muted")
        self.endpoint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._endpoint_url = api_url
        self.set_endpoint(api_url)
        self.check = QPushButton("Check connection")
        self.check.setAccessibleName("Check backend connection")
        command(self.check, "wifi")
        self.check.clicked.connect(self.check_requested.emit)
        self.connection.retry_requested.connect(self.check_requested.emit)
        connection_layout.addWidget(self.connection)
        connection_layout.addWidget(self.endpoint)
        connection_layout.addWidget(self.check, alignment=Qt.AlignmentFlag.AlignLeft)
        connection_layout.addStretch()
        self.sections = QGridLayout()
        self.sections.setHorizontalSpacing(32)
        self.sections.addWidget(self.account_slot, 0, 0)
        self.sections.addWidget(self.connection_section, 0, 1)
        self.sections.setColumnStretch(0, 3)
        self.sections.setColumnStretch(1, 2)
        self.body.addLayout(self.sections)
        self.body.addStretch()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        narrow = self.width() < 760
        position = self.sections.getItemPosition(self.sections.indexOf(self.connection_section))
        assert isinstance(position, tuple)
        if position[:2] != ((1, 0) if narrow else (0, 1)):
            self.sections.removeWidget(self.connection_section)
            self.sections.addWidget(self.connection_section, 1 if narrow else 0, 0 if narrow else 1)
            self.sections.setColumnStretch(1, 0 if narrow else 2)
        self.set_endpoint(self._endpoint_url)

    def set_endpoint(self, url: str) -> None:
        self._endpoint_url = url
        self.endpoint.setToolTip(url)
        self.endpoint.setAccessibleDescription(url)
        self.endpoint.setText(
            self.endpoint.fontMetrics().elidedText(
                url, Qt.TextElideMode.ElideMiddle, max(1, self.endpoint.width())
            )
        )


class UnavailablePage(Page):
    def __init__(self, title: str, message: str) -> None:
        super().__init__(title, message)
        self.state = StatePanel()
        self.state.show_state("This workspace is not available yet", message)
        self.body.addWidget(self.state)
        self.body.addStretch()


class SettingsPage(Page):
    save_requested = Signal(str)

    def __init__(self, api_url: str, *, environment_override: bool) -> None:
        super().__init__("Settings", "")
        heading = QLabel("Backend connection")
        heading.setObjectName("sectionTitle")
        self.body.addWidget(heading)
        self.url = QLineEdit(api_url)
        self.url.setAccessibleName("Backend URL")
        self.url.setPlaceholderText("https://your-buddy-backend.example")
        self.url.setEnabled(not environment_override)
        self.url.setMaxLength(2048)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        label = QLabel("&Backend URL")
        label.setBuddy(self.url)
        form.addRow(label, self.url)
        self.body.addLayout(form)
        self.save = QPushButton("Save settings")
        command(self.save, "save", primary=True)
        self.save.setEnabled(not environment_override)
        self.save.clicked.connect(lambda: self.save_requested.emit(self.url.text()))
        self.url.returnPressed.connect(self.save.click)
        self.body.addWidget(self.save, alignment=Qt.AlignmentFlag.AlignLeft)
        self.feedback = QLabel(
            "Managed by BUDDY_API_URL. Update the environment and restart to change it."
            if environment_override
            else "Use HTTPS for a remote backend, or HTTP for a local development service."
        )
        self.feedback.setWordWrap(True)
        self.feedback.setAccessibleName("Settings status")
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.body.addWidget(self.feedback)
        appearance = QLabel("Appearance")
        appearance.setObjectName("sectionTitle")
        self.body.addWidget(appearance)
        self.reduce_motion = QCheckBox("Reduce motion")
        self.reduce_motion.setAccessibleName("Reduce mascot motion")
        self.body.addWidget(self.reduce_motion)
        self.body.addStretch()
