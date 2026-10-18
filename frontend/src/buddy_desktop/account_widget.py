"""Accessible account controls with cancellable browser work off the Qt GUI thread."""

from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QTimer, Signal
from PySide6.QtWidgets import QGridLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from buddy_desktop.auth import AccountSession, NativeTokenStore
from buddy_desktop.services import ServiceError
from buddy_desktop.tasks import TaskRunner
from buddy_desktop.visuals import command
from buddy_identity.config import IdentityConfig, IdentityError


class AccountWidget(QWidget):
    activity_changed = Signal(bool)
    session_changed = Signal()

    def __init__(self, api_url: str) -> None:
        super().__init__()
        self.setObjectName("accountPanel")
        self.session: AccountSession | None = None
        self.account_id: str | None = None
        self.runner = TaskRunner(self)
        self.runner.succeeded.connect(self.completed)
        self.runner.failed.connect(self.failed)
        self.runner.idle.connect(self.idle)
        self.operation = ""
        self.closing = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        heading = QLabel("Account")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        self.message = QLabel("Sign in to your Buddy account.")
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        self.message.setObjectName("muted")
        layout.addWidget(self.message)
        buttons = QGridLayout()
        buttons.setSpacing(8)
        self.buttons: dict[str, QPushButton] = {}
        for index, (action, title) in enumerate(
            [
                ("signin", "Sign in"),
                ("signup", "Create account"),
                ("recovery", "Forgot password"),
                ("resume", "Resume session"),
                ("signout", "Sign out"),
            ]
        ):
            button = QPushButton(title)
            command(
                button,
                {
                    "signin": "log-in",
                    "signup": "user-plus",
                    "recovery": "key-round",
                    "resume": "rotate-ccw",
                    "signout": "log-out",
                }[action],
                primary=action == "signin",
            )
            button.clicked.connect(lambda checked=False, name=action: self.start(name))
            buttons.addWidget(button, index // 2, index % 2)
            self.buttons[action] = button
        layout.addLayout(buttons)
        self.cancel = QPushButton("Cancel sign-in")
        command(self.cancel, "x")
        self.cancel.clicked.connect(self.cancel_operation)
        self.cancel.setEnabled(False)
        buttons.addWidget(self.cancel, 2, 1)
        layout.addStretch()
        self.timer = QTimer(self)
        self.timer.setInterval(60000)
        self.timer.timeout.connect(self.check_session)
        self.configure(api_url)

    def configure(self, api_url: str) -> None:
        if self.runner.busy:
            return
        self.timer.stop()
        self.session = None
        self.account_id = None
        try:
            config = IdentityConfig.from_environment()
            if config:
                identity = api_url + "|" + config.issuer + "|" + config.client_id
                directory = Path(
                    QStandardPaths.writableLocation(
                        QStandardPaths.StandardLocation.AppLocalDataLocation
                    )
                )
                self.session = AccountSession(
                    config, api_url, NativeTokenStore(identity, directory)
                )
                self.message.setText("Sign in, create an account, or resume your saved session.")
            else:
                self.message.setText("Account access is not configured for this installation.")
        except IdentityError as exc:
            self.message.setText(str(exc))
        self.idle()

    def start(self, action: str) -> None:
        session = self.session
        if session is None or self.runner.busy or self.closing:
            return
        self.operation = action
        if action != "check":
            self.account_id = None
        session.cancelled.clear()
        self.timer.stop()
        self.message.setText(
            {
                "signin": (
                    "Sign in with your email and password in the browser. You can cancel here."
                ),
                "signup": (
                    "Create your account and enter the email verification code in the browser."
                ),
                "recovery": (
                    "Request an email code, then enter it with your new password in the browser. "
                    "Sign in there afterward to return to Buddy."
                ),
            }.get(action, "Checking account session…")
        )
        for button in self.buttons.values():
            button.setEnabled(False)
        self.cancel.setEnabled(action in {"signin", "signup", "recovery"})
        self.activity_changed.emit(True)

        def work() -> str:
            try:
                if action == "resume":
                    return session.resume()
                if action == "signout":
                    return session.sign_out()
                if action == "check":
                    return session.account()
                return session.sign_in(action)
            except IdentityError as exc:
                raise ServiceError(str(exc)) from None

        self.runner.submit(work)

    def completed(self, _identifier: int, result: object) -> None:
        if self.closing or (self.session and self.session.cancelled.is_set()):
            self.message.setText("Sign-in was cancelled.")
            return
        # Cognito's page keeps its spinner after handing off the callback URL.
        self.message.setText(
            "Signed out."
            if self.operation == "signout"
            else "Signed in to Buddy. You can close the browser tab."
            if self.operation in {"signin", "signup", "recovery"}
            else "Signed in to Buddy."
        )
        if self.session and self.session.tokens:
            self.account_id = str(result)
            self.timer.start()
        else:
            self.account_id = None

    def failed(self, _identifier: int, message: str) -> None:
        self.account_id = None
        self.message.setText(message)
        if self.session:
            self.session.tokens = None

    def idle(self) -> None:
        session = self.session
        # Cancel may arrive after worker completion but before its queued Qt result.
        # Finish cleanup on the worker before allowing another operation or closing.
        if session and session.cancelled.is_set() and session.tokens is not None:

            def cleanup() -> str:
                try:
                    return session.discard()
                except IdentityError as exc:
                    raise ServiceError(str(exc)) from None

            self.runner.submit(cleanup)
            return
        if self.closing:
            self.runner.stop()
        for action, button in self.buttons.items():
            signed_in = self.session is not None and self.session.tokens is not None
            button.setEnabled(
                self.session is not None
                and not self.closing
                and (action == "signout" or not signed_in)
            )
        self.cancel.setEnabled(False)
        self.activity_changed.emit(False)
        self.session_changed.emit()

    def expire_session(self, message: str) -> None:
        self.account_id = None
        if self.session:
            self.session.tokens = None
        self.timer.stop()
        self.message.setText(message)
        self.idle()

    def check_session(self) -> None:
        if self.session and self.session.tokens and not self.runner.busy:
            self.start("check")

    def cancel_operation(self) -> None:
        if self.session:
            self.session.cancelled.set()

    def shutdown(self) -> None:
        self.closing = True
        self.timer.stop()
        if self.runner.busy:
            self.cancel_operation()
        else:
            self.runner.stop()
