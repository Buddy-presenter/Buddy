"""Accessible account controls with cancellable browser work off the Qt GUI thread."""

from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from buddy_desktop.auth import AccountProfile, AccountSession, NativeTokenStore, ProfileFailure
from buddy_desktop.services import ServiceError
from buddy_desktop.tasks import TaskRunner
from buddy_desktop.views import StatusLabel
from buddy_desktop.visuals import command
from buddy_identity.config import IdentityConfig, IdentityError, SessionExpired


class AccountWidget(QWidget):
    activity_changed = Signal(bool)
    session_changed = Signal()
    profile_loaded = Signal(int, object)
    profile_failed = Signal(int, str)

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
        layout.setSpacing(10)
        self.message = StatusLabel()
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        self.message.setObjectName("muted")
        layout.addWidget(self.message)
        self.buttons: dict[str, QPushButton] = {}
        for action, title, tip in [
            ("signin", "Sign in", "Sign in with your email in the browser"),
            ("signup", "Create account", "Create a Buddy account in the browser"),
            ("recovery", "Forgot password?", "Reset your password in the browser"),
            ("resume", "Use saved sign-in", "Sign back in with the session saved on this device"),
            ("signout", "Sign out", "Sign out of Buddy on this device"),
        ]:
            button = QPushButton(title)
            button.setAccessibleName(title.rstrip("?"))
            button.setToolTip(tip)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda checked=False, name=action: self.start(name))
            self.buttons[action] = button
        command(self.buttons["signin"], "log-in", primary=True)
        self.buttons["signin"].setIcon(QIcon())
        self.buttons["signin"].setToolTip("Sign in with your email in the browser")
        for action in ("signin", "signup"):
            layout.addWidget(self.buttons[action])
        links = QHBoxLayout()
        links.setContentsMargins(0, 2, 0, 0)
        # Alignment rather than a spacer, so the row collapses when both links are hidden.
        for action, side in (
            ("recovery", Qt.AlignmentFlag.AlignLeft),
            ("resume", Qt.AlignmentFlag.AlignRight),
        ):
            self.buttons[action].setObjectName("linkButton")
            links.addWidget(self.buttons[action], alignment=side)
        layout.addLayout(links)
        command(self.buttons["signout"], "log-out")
        self.buttons["signout"].setObjectName("menuItem")
        self.buttons["signout"].setToolTip("Sign out of Buddy on this device")
        layout.addWidget(self.buttons["signout"])
        self.cancel = QPushButton("Cancel sign-in")
        command(self.cancel, "x")
        self.cancel.clicked.connect(self.cancel_operation)
        self.cancel.setEnabled(False)
        layout.addWidget(self.cancel)
        self.cancel.hide()
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
                self.message.setText("")
            else:
                self.message.setText("Account access is not configured for this installation.")
        except IdentityError as exc:
            self.message.setText(str(exc))
        self.idle()

    def start(self, action: str) -> int | None:
        session = self.session
        if session is None or self.runner.busy or self.closing:
            return None
        self.operation = action
        if action == "profile" and (not self.account_id or not session.tokens):
            return None
        expected_id = self.account_id
        if action not in {"check", "profile"}:
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
        self.cancel.setVisible(action in {"signin", "signup", "recovery"})
        self.activity_changed.emit(True)

        def work() -> str | AccountProfile | ProfileFailure:
            try:
                if action == "profile" and expected_id:
                    try:
                        return session.profile(expected_id)
                    except SessionExpired:
                        return ProfileFailure("Your session expired. Please sign in again.", True)
                    except IdentityError:
                        return ProfileFailure(
                            "Profile unavailable. Check your connection and retry."
                        )
                if action == "resume":
                    return session.resume()
                if action == "signout":
                    return session.sign_out()
                if action == "check":
                    return session.account()
                return session.sign_in(action)
            except IdentityError as exc:
                raise ServiceError(str(exc)) from None

        return self.runner.submit(work)

    def completed(self, _identifier: int, result: object) -> None:
        if self.closing or (self.session and self.session.cancelled.is_set()):
            self.message.setText("Sign-in was cancelled.")
            return
        if self.operation == "profile":
            if isinstance(result, AccountProfile) and result.id == self.account_id:
                self.profile_loaded.emit(_identifier, result)
                # The verified email in the account menu already says who is signed in.
                self.message.setText("")
                self.timer.start()
            else:
                if isinstance(result, ProfileFailure) and result.expired:
                    self.account_id = None
                    if self.session:
                        self.session.tokens = None
                message = (
                    result.message if isinstance(result, ProfileFailure) else "Profile unavailable."
                )
                self.message.setText(message)
                self.profile_failed.emit(_identifier, message)
                if self.session and self.session.tokens:
                    self.timer.start()
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
        if self.operation == "profile":
            self.profile_failed.emit(_identifier, "Profile unavailable. Please try again.")
            if self.session and self.session.tokens:
                self.timer.start()
            return
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
        for action, button in self.buttons.items():
            button.setVisible((action == "signout") == signed_in)
        self.cancel.setEnabled(False)
        self.cancel.hide()
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
