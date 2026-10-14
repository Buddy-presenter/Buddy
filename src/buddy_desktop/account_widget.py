"""Accessible account controls with cancellable browser work off the Qt GUI thread."""

from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from buddy_desktop.auth import AccountSession, NativeTokenStore
from buddy_desktop.services import ServiceError
from buddy_desktop.tasks import TaskRunner
from buddy_identity.config import IdentityConfig, IdentityError


class AccountWidget(QWidget):
    activity_changed = Signal(bool)

    def __init__(self, api_url: str) -> None:
        super().__init__()
        self.session: AccountSession | None = None
        self.runner = TaskRunner(self)
        self.runner.succeeded.connect(self.completed)
        self.runner.failed.connect(self.failed)
        self.runner.idle.connect(self.idle)
        self.operation = ""
        self.closing = False
        layout = QVBoxLayout(self)
        self.message = QLabel("Sign in to your Buddy account.")
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        buttons = QHBoxLayout()
        self.buttons: dict[str, QPushButton] = {}
        for action, title in [
            ("signin", "Sign in"),
            ("signup", "Create account"),
            ("recovery", "Recover account"),
            ("resume", "Resume session"),
            ("signout", "Sign out"),
        ]:
            button = QPushButton(title)
            button.clicked.connect(lambda checked=False, name=action: self.start(name))
            buttons.addWidget(button)
            self.buttons[action] = button
        layout.addLayout(buttons)
        self.cancel = QPushButton("Cancel sign-in")
        self.cancel.clicked.connect(self.cancel_operation)
        self.cancel.setEnabled(False)
        layout.addWidget(self.cancel)
        self.timer = QTimer(self)
        self.timer.setInterval(60000)
        self.timer.timeout.connect(self.check_session)
        self.configure(api_url)

    def configure(self, api_url: str) -> None:
        if self.runner.busy:
            return
        self.timer.stop()
        self.session = None
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
        session.cancelled.clear()
        self.timer.stop()
        self.message.setText(
            "Continue in your browser. You can cancel here."
            if action in {"signin", "signup", "recovery"}
            else "Checking account session…"
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
        self.message.setText(
            "Signed out." if self.operation == "signout" else "Signed in to Buddy."
        )
        if self.session and self.session.tokens:
            self.timer.start()

    def failed(self, _identifier: int, message: str) -> None:
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
