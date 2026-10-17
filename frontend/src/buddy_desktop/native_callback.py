"""OS URL activation with one in-memory PKCE attempt; never a TCP callback listener."""

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import gettempdir
from threading import Event, Lock
from urllib.parse import parse_qs, urlsplit

from PySide6.QtCore import QEvent, QLockFile, QObject, QStandardPaths, QTimer, Signal
from PySide6.QtGui import QFileOpenEvent
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from buddy_identity.config import CALLBACK, SIGNOUT_CALLBACK, IdentityError

MAX_CALLBACK_BYTES = 8192


def callback_shape(uri: str) -> bool:
    """Only the two fixed app routes, without fragments, credentials or control bytes."""
    try:
        return (
            len(uri.encode()) <= MAX_CALLBACK_BYTES
            and not any(ord(c) < 33 or ord(c) == 127 for c in uri)
            and not urlsplit(uri).fragment
            and uri.split("?", 1)[0] in {CALLBACK, SIGNOUT_CALLBACK}
        )
    except (ValueError, UnicodeError):
        return False


@dataclass(repr=False)
class Attempt:
    state: str
    logout: bool
    received: Event = field(default_factory=Event)
    code: str = ""


class PendingCallback:
    def __init__(self) -> None:
        self.lock = Lock()
        self.attempt: Attempt | None = None

    @contextmanager
    def waiting(self, state: str, *, logout: bool = False) -> Iterator[Attempt]:
        attempt = Attempt(state, logout)
        with self.lock:
            if self.attempt is not None:
                raise IdentityError("Another sign-in is in progress. Cancel it before retrying.")
            self.attempt = attempt
        try:
            yield attempt
        finally:
            with self.lock:
                self.attempt = None
                attempt.code = ""

    def deliver(self, uri: str) -> bool:
        if not callback_shape(uri):
            return False
        try:
            parts = urlsplit(uri)
            values = parse_qs(
                parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=4
            )
        except ValueError:
            return False
        with self.lock:
            attempt = self.attempt
            if attempt is None or attempt.received.is_set():
                return False
            if attempt.logout:
                if uri != SIGNOUT_CALLBACK:
                    return False
                attempt.code = "signedout"
            else:
                if (
                    uri.split("?", 1)[0] != CALLBACK
                    or values.get("state") != [attempt.state]
                    or not set(values) <= {"state", "code", "error", "error_description"}
                    or any(len(v) != 1 for v in values.values())
                    or ("code" in values) == ("error" in values)
                    or ("code" in values and not values["code"][0])
                ):
                    return False
                attempt.code = values.get("code", [""])[0]
            attempt.received.set()
            return True


pending_callback = PendingCallback()


class NativeCallbackServer(QObject):
    """One app per OS user. Windows activations relay through a user-only named pipe.

    macOS delivers URLs directly as QFileOpenEvent. A private directory also protects
    Unix-domain sockets: macOS does not enforce QLocalServer's permission option.
    """

    activated = Signal()

    def __init__(self, parent: QObject | None = None, *, directory: Path | None = None) -> None:
        super().__init__(parent)
        directory = (
            directory
            or Path(
                QStandardPaths.writableLocation(
                    QStandardPaths.StandardLocation.AppLocalDataLocation
                )
            )
            / "activation"
        )
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            directory.chmod(0o700)
        digest = hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:24]
        if os.name == "nt":
            self.name = "Buddy-" + digest
        else:
            # Unix socket names are short (104 bytes on macOS); pytest/profile paths
            # can exceed that. Protect a short directory rather than trusting socket mode.
            sockets = Path(gettempdir()) / ("buddy-" + digest[:12])
            sockets.mkdir(mode=0o700, exist_ok=True)
            if sockets.is_symlink() or sockets.stat().st_uid != directory.stat().st_uid:
                raise IdentityError("Buddy activation directory is unavailable.")
            sockets.chmod(0o700)
            self.name = str(sockets / "ipc")
        self.guard = QLockFile(str(directory / "instance.lock"))
        self.server = QLocalServer(self)
        self.server.setMaxPendingConnections(4)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self.connected)
        self.buffers: dict[QLocalSocket, bytearray] = {}

    def start(self, uri: str | None = None) -> bool:
        if uri is not None and not callback_shape(uri):
            raise IdentityError("Invalid Buddy callback. Start sign-in from the application.")
        if not self.guard.tryLock(0):
            socket = QLocalSocket()
            socket.connectToServer(self.name)
            if not socket.waitForConnected(1500):
                raise IdentityError("Buddy is already starting. Return to it and retry sign-in.")
            payload = (uri or "activate").encode() + b"\n"
            socket.write(payload)
            if not socket.waitForBytesWritten(1500):
                raise IdentityError("Cannot return to Buddy. Start sign-in again.")
            socket.disconnectFromServer()
            return False
        # Only the lock owner may remove a stale socket after a terminated process.
        QLocalServer.removeServer(self.name)
        if not self.server.listen(self.name):
            self.guard.unlock()
            raise IdentityError("Cannot receive browser sign-in. Restart Buddy and retry.")
        # No pending attempt exists on cold start: never resume an unsolicited URL.
        return True

    def connected(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            self.buffers[socket] = bytearray()
            socket.readyRead.connect(lambda socket=socket: self.read(socket))
            socket.disconnected.connect(lambda socket=socket: self.disconnected(socket))
            QTimer.singleShot(2000, socket, socket.abort)
            self.read(socket)

    def read(self, socket: QLocalSocket) -> None:
        data = self.buffers.get(socket)
        if data is None:
            return
        data.extend(socket.read(MAX_CALLBACK_BYTES + 2).data())
        if len(data) > MAX_CALLBACK_BYTES + 1:
            socket.abort()
            return
        if b"\n" not in data:
            return
        try:
            payload = bytes(data).removesuffix(b"\n").decode("utf-8")
        except UnicodeError:
            socket.abort()
            return
        if payload == "activate" or pending_callback.deliver(payload):
            self.activated.emit()
        socket.disconnectFromServer()

    def disconnected(self, socket: QLocalSocket) -> None:
        self.buffers.pop(socket, None)
        socket.deleteLater()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.FileOpen and isinstance(event, QFileOpenEvent):
            if pending_callback.deliver(event.url().toString()):
                self.activated.emit()
            return True
        return super().eventFilter(watched, event)

    def close(self) -> None:
        for socket in list(self.buffers):
            socket.abort()
        self.server.close()
        self.guard.unlock()
