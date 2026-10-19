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
        layout.setContentsMargins(22, 22, 22, 22)
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
        self.body.setContentsMargins(32, 28, 32, 28)
        self.body.setSpacing(18)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        heading.setWordWrap(True)
        introduction = QLabel(description)
        introduction.setObjectName("pageDescription")
        introduction.setWordWrap(True)
        introduction.setVisible(bool(description))
        heading.setVisible(bool(title))
        self.body.addWidget(heading)
        self.body.addWidget(introduction)


class HomePage(Page):
    route_requested = Signal(int)

    def __init__(self, api_url: str) -> None:
        super().__init__("", "")
        self.setObjectName("homePage")
        # Home is a starting point for work; account and service controls live in
        # the profile popover and Settings, respectively.
        self.body.addStretch(1)
        hero = QWidget()
        hero.setObjectName("homeHero")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(28, 30, 28, 30)
        hero_layout.setSpacing(16)
        copy = QVBoxLayout()
        copy.setSpacing(14)
        badge = QLabel("YOUR PRESENTATION COMPANION")
        badge.setObjectName("eyebrow")
        copy.addWidget(badge)
        title = QLabel("Less prep.\nMore presence.")
        title.setObjectName("heroTitle")
        title.setWordWrap(True)
        copy.addWidget(title)
        self.workspace_state = QLabel("A space for your slides. A companion for your moment.")
        self.workspace_state.setObjectName("pageDescription")
        self.workspace_state.setWordWrap(True)
        copy.addWidget(self.workspace_state)
        hero_layout.addLayout(copy, 1)
        self.mascot = BuddyMark(144)
        hero_layout.addWidget(self.mascot, alignment=Qt.AlignmentFlag.AlignVCenter)
        self.body.addWidget(hero)
        self.body.addSpacing(14)
        section = QLabel("Where would you like to start?")
        section.setObjectName("sectionTitle")
        self.body.addWidget(section)
        actions = QGridLayout()
        actions.setSpacing(16)
        self.library_action = QPushButton("Go to library")
        self.present_action = QPushButton("Open workspace")
        for column, (button, image, route, card_title, description) in enumerate(
            (
                (
                    self.library_action,
                    "library-big",
                    1,
                    "Your presentations",
                    "Import and keep your PDF and PowerPoint decks in one place.",
                ),
                (
                    self.present_action,
                    "presentation",
                    2,
                    "Take the stage",
                    "Open a local PDF and present. No account needed.",
                ),
            )
        ):
            card = QWidget()
            card.setObjectName("startCard")
            card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            content = QVBoxLayout(card)
            content.setContentsMargins(22, 22, 22, 22)
            content.setSpacing(12)
            label = QLabel(card_title)
            label.setObjectName("sectionTitle")
            content.addWidget(label)
            detail = QLabel(description)
            detail.setObjectName("muted")
            detail.setWordWrap(True)
            content.addWidget(detail)
            content.addStretch()
            command(button, image)
            button.setObjectName("quietButton")
            button.clicked.connect(lambda checked=False, row=route: self.route_requested.emit(row))
            content.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
            actions.addWidget(card, 0, column)
            actions.setColumnStretch(column, 1)
        self.body.addLayout(actions)
        self.body.addStretch(2)


class UnavailablePage(Page):
    def __init__(self, title: str, message: str) -> None:
        super().__init__(title, message)
        self.state = StatePanel()
        self.state.show_state("This workspace is not available yet", message)
        self.body.addWidget(self.state)
        self.body.addStretch()


class SettingsPage(Page):
    save_requested = Signal(str)
    check_requested = Signal()

    def __init__(self, api_url: str, *, environment_override: bool) -> None:
        super().__init__("Settings", "")
        appearance = QLabel("Appearance")
        appearance.setObjectName("sectionTitle")
        self.body.addWidget(appearance)
        self.reduce_motion = QCheckBox("Reduce motion")
        self.reduce_motion.setAccessibleName("Reduce mascot motion")
        self.body.addWidget(self.reduce_motion)
        self.body.addSpacing(16)
        heading = QLabel("Service connection")
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
        self.connection = StatePanel()
        self.connection.show_state(
            "Connection not checked", "Check the service if you are having trouble syncing."
        )
        self.connection.retry_requested.connect(self.check_requested.emit)
        self.body.addWidget(self.connection)
        self.endpoint = QLabel()
        self.endpoint.setObjectName("muted")
        self.endpoint.setTextFormat(Qt.TextFormat.PlainText)
        self.endpoint.setAccessibleName("Configured backend address")
        self.endpoint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.body.addWidget(self.endpoint)
        self._endpoint_url = api_url
        self.set_endpoint(api_url)
        self.check = QPushButton("Check connection")
        command(self.check, "wifi")
        self.check.clicked.connect(self.check_requested.emit)
        self.body.addWidget(self.check, alignment=Qt.AlignmentFlag.AlignLeft)
        self.body.addStretch()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
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
