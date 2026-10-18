"""A read-only verified profile, using the existing serialized account lifecycle."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QPushButton, QSizePolicy

from buddy_desktop.account_widget import AccountWidget
from buddy_desktop.auth import AccountProfile
from buddy_desktop.views import Page, StatePanel
from buddy_desktop.visuals import command


class ProfilePage(Page):
    controls_requested = Signal()

    def __init__(self, account: AccountWidget) -> None:
        super().__init__("Your profile", "Your verified Buddy account information.")
        self.account = account
        self.active = False
        self.request_id: int | None = None
        self.profile_id: str | None = None
        self.waiting = False
        self.refresh = QPushButton("Refresh profile")
        command(self.refresh, "refresh-cw")
        self.refresh.clicked.connect(self.load)
        self.body.addWidget(self.refresh, alignment=Qt.AlignmentFlag.AlignLeft)
        self.state = StatePanel()
        self.state.retry.setAccessibleName("Retry profile request")
        self.state.retry_requested.connect(self.load)
        self.body.addWidget(self.state)
        self.email = QLabel()
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
        self.body.addWidget(self.email)
        self.verification = QLabel()
        self.verification.setAccessibleName("Email verification status")
        self.verification.setWordWrap(True)
        self.body.addWidget(self.verification)
        guidance = QLabel(
            "To sign out, sign in again, or recover your password, "
            "open the existing account controls."
        )
        guidance.setWordWrap(True)
        self.body.addWidget(guidance)
        self.controls = QPushButton("Account controls")
        command(self.controls, "circle-user-round")
        self.controls.clicked.connect(self.controls_requested.emit)
        self.body.addWidget(self.controls, alignment=Qt.AlignmentFlag.AlignLeft)
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
        self.email.hide()
        self.verification.hide()

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
        if not self.account.account_id:
            self.state.show_state(
                "Sign in to view your profile", "Open Account controls to sign in."
            )
            self.refresh.setEnabled(False)
            return
        if self.account.runner.busy:
            self.waiting = True
            self.refresh.setEnabled(False)
            self.state.show_state(
                "Checking account", "Your profile will load after the account check."
            )
            return
        self.waiting = False
        self.refresh.setEnabled(False)
        self.state.show_state("Loading profile", "Checking your account information securely…")
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
        self.email.show()
        self.verification.show()
        self.state.show_state(
            "Account verified", "This information comes from your account provider."
        )

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
                self.state.show_state(
                    "Checking account", "Waiting for your account operation to finish."
                )
        self.refresh.setEnabled(not busy and bool(self.account.account_id))
        self.state.retry.setEnabled(not busy)

    def session_changed(self) -> None:
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
