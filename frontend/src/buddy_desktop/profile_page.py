"""A read-only verified profile, using the existing serialized account lifecycle."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from buddy_desktop.account_widget import AccountWidget
from buddy_desktop.auth import AccountProfile
from buddy_desktop.views import Page, StatePanel, centered_column
from buddy_desktop.visuals import command


class ProfilePage(Page):
    controls_requested = Signal()

    def __init__(self, account: AccountWidget) -> None:
        super().__init__("Profile", "")
        self.account = account
        self.active = False
        self.request_id: int | None = None
        self.profile_id: str | None = None
        self.waiting = False
        self.body.addSpacing(24)
        layout = centered_column(self.body, 520)
        layout.setSpacing(16)
        self.state = StatePanel(centered=True)
        self.state.retry.setAccessibleName("Retry profile request")
        self.state.retry_requested.connect(self.load)
        layout.addWidget(self.state)
        self.card = QWidget()
        self.card.setObjectName("group")
        self.card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card = QHBoxLayout(self.card)
        card.setContentsMargins(20, 20, 16, 20)
        card.setSpacing(16)
        self.avatar = QLabel()
        self.avatar.setObjectName("avatarLarge")
        self.avatar.setFixedSize(56, 56)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card.addWidget(self.avatar)
        identity = QVBoxLayout()
        identity.setSpacing(4)
        self.email = QLabel()
        self.email.setObjectName("cardTitle")
        self.email.setTextFormat(Qt.TextFormat.PlainText)
        self.email.setWordWrap(True)
        self.email.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.email.setMinimumWidth(0)
        self.email.setAccessibleName("Verified account email")
        self.email.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.email.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        identity.addWidget(self.email)
        self.verification = QLabel()
        self.verification.setObjectName("badge")
        self.verification.setAccessibleName("Email verification status")
        self.verification.setWordWrap(True)
        identity.addWidget(self.verification)
        card.addLayout(identity, 1)
        self.refresh = QPushButton("Refresh profile")
        command(self.refresh, "refresh-cw", compact=True)
        self.refresh.setObjectName("quietButton")
        self.refresh.clicked.connect(self.load)
        card.addWidget(self.refresh, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.card)
        self.controls = QPushButton("Manage account")
        self.controls.setCursor(Qt.CursorShape.PointingHandCursor)
        self.controls.clicked.connect(self.controls_requested.emit)
        layout.addWidget(self.controls, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.body.addStretch()
        account.profile_loaded.connect(self.loaded)
        account.profile_failed.connect(self.failed)
        account.activity_changed.connect(self.activity)
        account.session_changed.connect(self.session_changed)
        self.clear()

    def clear(self) -> None:
        self.request_id = None
        self.profile_id = None
        self.email.clear()
        self.verification.clear()
        self.avatar.clear()
        self.email.hide()
        self.verification.hide()
        self.card.hide()
        self._controls()

    def _controls(self) -> None:
        signed_in = bool(self.account.account_id)
        self.controls.setText("Manage account" if signed_in else "Sign in")
        self.controls.setAccessibleName(
            "Open your account menu" if signed_in else "Sign in to view your profile"
        )
        self.controls.setToolTip(self.controls.accessibleName())
        self.controls.setProperty("primary", not signed_in)
        self.controls.style().unpolish(self.controls)
        self.controls.style().polish(self.controls)

    def activate(self, active: bool) -> None:
        self.active = active
        self.waiting = False
        self.clear()
        if active:
            self.load()

    def load(self) -> None:
        self.clear()
        if not self.active:
            return
        # Sign-in and resume deliberately clear account_id until authorization
        # completes. Queue the active page before classifying it as signed out.
        if self.account.runner.busy:
            self.waiting = True
            self.refresh.setEnabled(False)
            self.state.show_state(
                "Checking account", "Your profile will load after the account check."
            )
            return
        self.waiting = False
        if not self.account.account_id:
            self.state.show_state("Sign in to view your profile", "")
            self.refresh.setEnabled(False)
            return
        self.refresh.setEnabled(False)
        self.state.show_state("Loading profile", "")
        self.request_id = self.account.start("profile")

    def loaded(self, identifier: int, result: object) -> None:
        if (
            not self.active
            or identifier != self.request_id
            or not isinstance(result, AccountProfile)
            or result.id != self.account.account_id
        ):
            return
        self.profile_id = result.id
        self.email.setText(result.email)
        self.verification.setText("Email verified")
        self.avatar.setText(result.email[:1].upper())
        self.email.show()
        self.verification.show()
        self.card.show()
        self.state.hide()
        self._controls()

    def failed(self, identifier: int, message: str) -> None:
        if not self.active or identifier != self.request_id:
            return
        self.clear()
        self.state.show_state("Profile unavailable", message, retry=bool(self.account.account_id))

    def activity(self, busy: bool) -> None:
        if busy and self.account.operation != "profile":
            self.clear()
            if self.active:
                self.waiting = True
                self.state.show_state("Checking account", "")
        self.refresh.setEnabled(not busy and bool(self.account.account_id))
        self.state.retry.setEnabled(not busy)

    def session_changed(self) -> None:
        self._controls()
        if not self.account.account_id:
            self.waiting = False
            self.clear()
            if self.active:
                self.state.show_state("Sign in to view your profile", self.account.message.text())
        elif self.profile_id and self.profile_id != self.account.account_id:
            self.clear()
            if self.active:
                self.state.show_state(
                    "Account changed", "Refresh to view the current account.", retry=True
                )
        elif self.active and self.waiting and not self.account.runner.busy:
            self.load()
