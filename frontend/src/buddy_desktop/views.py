"""Reusable, accessible shell views. Domain workflows are separate features."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class StatePanel(QWidget):
    """One consistent surface for loading, unavailable, success, and error states."""

    retry_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statePanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        self.title = QLabel()
        self.title.setObjectName("stateTitle")
        self.title.setWordWrap(True)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.retry = QPushButton("Try again")
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
        self.body.setContentsMargins(32, 28, 32, 28)
        self.body.setSpacing(20)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        heading.setWordWrap(True)
        introduction = QLabel(description)
        introduction.setWordWrap(True)
        self.body.addWidget(heading)
        self.body.addWidget(introduction)


class HomePage(Page):
    check_requested = Signal()

    def __init__(self, api_url: str) -> None:
        super().__init__(
            "Your presentation companion",
            "Prepare your workspace, connect to Buddy, and keep control of your presentation.",
        )
        self.connection = StatePanel()
        self.connection.show_state(
            "Connection not checked", "Check that your configured Buddy backend is available."
        )
        self.endpoint = QLabel(api_url)
        self.endpoint.setWordWrap(True)
        self.endpoint.setTextFormat(Qt.TextFormat.PlainText)
        self.endpoint.setAccessibleName("Configured backend address")
        self.check = QPushButton("Check connection")
        self.check.setAccessibleName("Check backend connection")
        self.check.clicked.connect(self.check_requested.emit)
        self.connection.retry_requested.connect(self.check_requested.emit)
        self.body.addWidget(self.connection)
        self.body.addWidget(self.endpoint)
        self.body.addWidget(self.check, alignment=Qt.AlignmentFlag.AlignLeft)
        self.body.addStretch()


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
        super().__init__("Settings", "Choose the backend used by this desktop application.")
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
        self.body.addStretch()
