"""Real Qt activation/IPC and the security boundary around pending OAuth attempts."""

import json
import plistlib
import subprocess
import sys
from threading import Event

import pytest
from PySide6.QtCore import QSettings

from buddy_desktop.auth import browser_code
from buddy_desktop.native_callback import NativeCallbackServer, PendingCallback, pending_callback
from buddy_desktop.tasks import TaskRunner
from buddy_identity.config import CALLBACK, SIGNOUT_CALLBACK, IdentityConfig, IdentityError


@pytest.mark.parametrize(
    "suffix",
    [
        "?state=wrong&code=valid",
        "?state=expected&code=a&code=b",
        "?state=expected&code=",
        "?state=expected&code=a&error=denied",
        "?state=expected&access_token=secret",
        "?state=expected&code=a#fragment",
        "?state=expected&code=a&state=expected",
        "?state=expected&code=a&extra=x",
    ],
)
def test_bad_or_ambiguous_callbacks_do_not_complete_attempt(suffix):
    callback = PendingCallback()
    with callback.waiting("expected") as attempt:
        assert not callback.deliver(CALLBACK + suffix)
        assert not attempt.received.is_set()
        assert not callback.deliver("http://localhost/callback?state=expected&code=a")
        assert not callback.deliver(SIGNOUT_CALLBACK)
        assert not callback.deliver(CALLBACK + "?code=" + "a" * 8192)
        assert callback.deliver(CALLBACK + "?state=expected&code=valid")
        assert attempt.code == "valid"
    assert attempt.code == ""  # Single-use code does not survive the attempt.


def test_timeout_cancel_browser_failure_and_concurrent_attempt_cleanup():
    for opener, timeout in [(lambda _: False, 1), (lambda _: True, 0)]:
        with pytest.raises(IdentityError):
            browser_code("https://example.test", "state", Event(), opener=opener, timeout=timeout)
        assert pending_callback.attempt is None
    with (
        pending_callback.waiting("first"),
        pytest.raises(IdentityError, match="Another"),
        pending_callback.waiting("second"),
    ):
        pass
    with pending_callback.waiting("", logout=True) as attempt:
        assert not pending_callback.deliver(CALLBACK + "?state=&code=bad")
        assert pending_callback.deliver(SIGNOUT_CALLBACK)
        assert attempt.code == "signedout"


def test_macos_url_event_reaches_only_the_pending_attempt(tmp_path):
    # QFileOpenEvent is an application-wide OS event. Give it a fresh Qt process
    # rather than sharing native application state with earlier window tests.
    script = """
import sys
from pathlib import Path
from PySide6.QtCore import QCoreApplication, QUrl
from PySide6.QtGui import QFileOpenEvent
from PySide6.QtWidgets import QApplication
from buddy_desktop.native_callback import NativeCallbackServer, pending_callback
from buddy_identity.config import CALLBACK

app = QApplication([])
server = NativeCallbackServer(app, directory=Path(sys.argv[1]))
app.installEventFilter(server)
try:
    with pending_callback.waiting("expected") as attempt:
        event = QFileOpenEvent(QUrl(CALLBACK + "?state=expected&code=from-macos"))
        assert QCoreApplication.sendEvent(app, event)
        app.processEvents()
        assert attempt.code == "from-macos"
finally:
    app.removeEventFilter(server)
    server.close()
app.processEvents()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_second_process_relays_callback_over_native_ipc(qapp, qtbot, tmp_path):
    server = NativeCallbackServer(directory=tmp_path)
    assert server.start()
    script = """
import sys
from pathlib import Path
from PySide6.QtCore import QCoreApplication
from buddy_desktop.native_callback import NativeCallbackServer
app = QCoreApplication([])
server = NativeCallbackServer(directory=Path(sys.argv[1]))
assert not server.start(sys.argv[2])
"""
    try:
        with pending_callback.waiting("expected") as attempt:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(tmp_path),
                    CALLBACK + "?state=expected&code=from-windows",
                ]
            )
            try:
                qtbot.waitUntil(attempt.received.is_set, timeout=8000)
                assert attempt.code == "from-windows"
                assert process.wait(timeout=5) == 0
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
    finally:
        server.close()


def test_standalone_login_worker_receives_fresh_native_callback(qapp, qtbot, tmp_path):
    """The manual helper must pump Qt while its background OAuth wait is active."""
    server = NativeCallbackServer(directory=tmp_path)
    qapp.installEventFilter(server)
    assert server.start()
    runner = TaskRunner(qapp)
    outcomes = []
    runner.succeeded.connect(lambda _identifier, value: outcomes.append(("success", value)))
    runner.failed.connect(lambda _identifier, value: outcomes.append(("failure", value)))
    runner.submit(
        lambda: browser_code(
            "https://cognito.example.test/authorize",
            "standalone-state",
            Event(),
            timeout=8,
            opener=lambda _url: True,
        )
    )
    process = None
    try:
        qtbot.waitUntil(lambda: pending_callback.attempt is not None, timeout=3000)
        script = """
import sys
from pathlib import Path
from PySide6.QtCore import QCoreApplication
from buddy_desktop.native_callback import NativeCallbackServer
app = QCoreApplication([])
server = NativeCallbackServer(directory=Path(sys.argv[1]))
assert not server.start(sys.argv[2])
"""
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(tmp_path),
                CALLBACK + "?state=standalone-state&code=fresh-login-code",
            ]
        )
        qtbot.waitUntil(lambda: bool(outcomes), timeout=8000)
        assert outcomes == [("success", "fresh-login-code")]
        assert process.wait(timeout=5) == 0
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        runner.wait_for_shutdown()
        qapp.removeEventFilter(server)
        server.close()


def test_preview_public_config_matches_staging_and_macos_registration(tmp_path, monkeypatch):
    from importlib.resources import files
    from pathlib import Path

    from frontend.scripts.release.build_desktop import register_macos_callback

    config = json.loads(files("buddy_desktop").joinpath("deployment.json").read_text())
    root = Path(__file__).resolve().parents[2]
    assert config == json.loads((root / "infra/staging/public-config.json").read_text())
    assert config["BUDDY_API_URL"].startswith("https://")
    bundle = tmp_path / "Buddy.app"
    (bundle / "Contents").mkdir(parents=True)
    info = bundle / "Contents/Info.plist"
    info.write_bytes(plistlib.dumps({"CFBundleIdentifier": "org.buddy.desktop"}))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args[0]))
    register_macos_callback(bundle, "Developer ID test")
    metadata = plistlib.loads(info.read_bytes())
    assert metadata["CFBundleURLTypes"][0]["CFBundleURLSchemes"] == ["org.buddy.desktop"]
    assert "--timestamp" in calls[0] and calls[0][-1] == str(bundle)


def test_entrypoint_uses_bundled_api_as_default_without_disabling_saved_url(tmp_path, monkeypatch):
    import buddy_desktop.app as desktop_app

    deployment = {
        "BUDDY_API_URL": "https://staging.example.test",
        "BUDDY_AWS_REGION": "eu-central-1",
        "BUDDY_COGNITO_POOL_ID": "eu-central-1_TestPool",
        "BUDDY_COGNITO_CLIENT_ID": "client123",
        "BUDDY_COGNITO_DOMAIN": "https://buddy.auth.eu-central-1.amazoncognito.com",
    }
    (tmp_path / "deployment.json").write_text(json.dumps(deployment))
    monkeypatch.setattr(desktop_app, "files", lambda _package: tmp_path)
    for name in deployment:
        monkeypatch.delenv(name, raising=False)

    launches = []
    monkeypatch.setattr(
        desktop_app,
        "run_application",
        lambda config, *, environment_override: (
            launches.append((config, environment_override)) or 0
        ),
    )
    assert desktop_app.main() == 0
    config, environment_override = launches[-1]
    assert config.api_url == deployment["BUDDY_API_URL"]
    assert not environment_override

    settings = QSettings(str(tmp_path / "buddy.ini"), QSettings.Format.IniFormat)
    settings.setValue("connection/api_url", "https://saved.example.test")
    assert (
        desktop_app.resolve_config(
            config, settings, environment_override=environment_override
        ).api_url
        == "https://saved.example.test"
    )

    monkeypatch.setenv("BUDDY_API_URL", "https://operator.example.test")
    assert desktop_app.main() == 0
    config, environment_override = launches[-1]
    assert config.api_url == "https://operator.example.test"
    assert environment_override


@pytest.mark.parametrize(
    "frozen,override",
    [
        (False, "none"),
        (False, "api"),
        (False, "identity"),
        (False, "identity_callback"),
        (False, "callback"),
        (False, "single_identity"),
        # Frozen builds only change where the deployment file is read from.
        (True, "none"),
    ],
)
def test_entrypoint_keeps_callback_default_with_its_identity(
    tmp_path, monkeypatch, frozen, override
):
    import os

    import buddy_desktop.app as desktop_app

    deployment = {
        "BUDDY_API_URL": "https://staging.example.test",
        "BUDDY_AWS_REGION": "eu-central-1",
        "BUDDY_COGNITO_POOL_ID": "eu-central-1_Staging",
        "BUDDY_COGNITO_CLIENT_ID": "staging123",
        "BUDDY_COGNITO_DOMAIN": "https://staging.auth.eu-central-1.amazoncognito.com",
        "BUDDY_COGNITO_CALLBACK_URL": "https://staging.example.test/auth/desktop/callback",
    }
    for name in deployment:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    if frozen:
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        (tmp_path / "buddy-deployment.json").write_text(json.dumps(deployment))
    else:
        monkeypatch.setattr(desktop_app, "files", lambda _package: tmp_path)
        (tmp_path / "deployment.json").write_text(json.dumps(deployment))
    identity = {
        "BUDDY_AWS_REGION": "us-east-1",
        "BUDDY_COGNITO_POOL_ID": "us-east-1_Custom",
        "BUDDY_COGNITO_CLIENT_ID": "custom123",
        "BUDDY_COGNITO_DOMAIN": "https://custom.auth.us-east-1.amazoncognito.com",
    }
    if override.startswith("identity"):
        for name, value in identity.items():
            monkeypatch.setenv(name, value)
    elif override == "single_identity":
        monkeypatch.setenv("BUDDY_COGNITO_CLIENT_ID", "custom123")
    elif override == "api":
        monkeypatch.setenv("BUDDY_API_URL", "https://custom-api.example.test")
    callback = "https://custom.example.test/auth/desktop/callback"
    if override in {"identity_callback", "callback"}:
        monkeypatch.setenv("BUDDY_COGNITO_CALLBACK_URL", callback)
    before = {name: os.environ.get(name) for name in deployment}
    observed = []

    def launch(config, *, environment_override):
        observed.append(IdentityConfig.from_environment())
        return 0

    monkeypatch.setattr(desktop_app, "run_application", launch)
    assert desktop_app.main() == 0
    config = observed[0]
    assert config is not None
    expected = (
        callback
        if override in {"identity_callback", "callback"}
        else CALLBACK
        if override in {"identity", "single_identity"}
        else deployment["BUDDY_COGNITO_CALLBACK_URL"]
    )
    assert config.callback_url == expected
    assert config.logout_url == (
        SIGNOUT_CALLBACK
        if expected == CALLBACK
        else expected.removesuffix("callback") + "signed-out"
    )
    if override.startswith("identity"):
        assert config.region == identity["BUDDY_AWS_REGION"]
        assert config.pool_id == identity["BUDDY_COGNITO_POOL_ID"]
        assert config.client_id == identity["BUDDY_COGNITO_CLIENT_ID"]
        assert config.domain == identity["BUDDY_COGNITO_DOMAIN"]
    assert {name: os.environ.get(name) for name in deployment} == before


def test_production_configuration_cannot_silently_package_staging_or_localhost(tmp_path):
    from frontend.scripts.release.build_desktop import deployment_config

    preview = deployment_config(None)
    assert preview["BUDDY_COGNITO_CALLBACK_URL"].startswith("https://")
    with pytest.raises(ValueError, match="explicit"):
        deployment_config(None, production=True)
    path = tmp_path / "public.json"
    path.write_text(json.dumps(preview))
    with pytest.raises(ValueError, match="staging"):
        deployment_config(path, production=True)
    path.write_text(
        json.dumps(
            {
                **preview,
                "BUDDY_AWS_REGION": "",
                "BUDDY_COGNITO_POOL_ID": "",
                "BUDDY_COGNITO_CLIENT_ID": "",
                "BUDDY_COGNITO_DOMAIN": "",
            }
        )
    )
    with pytest.raises(ValueError, match="identity"):
        deployment_config(path, production=True)
    path.write_text(json.dumps({**preview, "BUDDY_API_URL": "http://127.0.0.1:8000"}))
    with pytest.raises(ValueError, match="HTTPS"):
        deployment_config(path)
    path.write_text(
        json.dumps(
            {**preview, "BUDDY_COGNITO_CALLBACK_URL": "http://localhost:8000/auth/desktop/callback"}
        )
    )
    with pytest.raises(ValueError, match="HTTPS"):
        deployment_config(path)
    path.write_text(json.dumps({**preview, "password": "synthetic-never-package"}))
    with pytest.raises(ValueError, match="public"):
        deployment_config(path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows protocol registration")
def test_source_callback_registration_restores_previous_handler(tmp_path):
    import winreg
    from uuid import uuid4

    from buddy_desktop.source_callback import windows_source_callback

    scheme = "buddy-test-" + uuid4().hex
    base = "Software\\Classes\\" + scheme
    path = base + "\\shell\\open\\command"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "previous-handler")
    try:
        with (
            pytest.raises(RuntimeError, match="application failed"),
            windows_source_callback([sys.executable, "-m", "buddy_desktop"], scheme=scheme),
        ):
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                command = winreg.QueryValueEx(key, "")[0]
                assert command.endswith('--auth-callback "%1"')
                assert sys.executable in command
            raise RuntimeError("application failed")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            assert winreg.QueryValueEx(key, "")[0] == "previous-handler"
        with (
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as key,
            pytest.raises(FileNotFoundError),
        ):
            winreg.QueryValueEx(key, "URL Protocol")
    finally:
        for suffix in ("\\shell\\open\\command", "\\shell\\open", "\\shell", ""):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, base + suffix)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows protocol registration")
def test_source_callback_registration_preserves_new_installer():
    import winreg
    from uuid import uuid4

    from buddy_desktop.source_callback import windows_source_callback

    scheme = "buddy-test-" + uuid4().hex
    base = "Software\\Classes\\" + scheme
    path = base + "\\shell\\open\\command"
    try:
        with (
            windows_source_callback([sys.executable], scheme=scheme),
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key,
        ):
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "new-installer")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            assert winreg.QueryValueEx(key, "")[0] == "new-installer"
    finally:
        for suffix in ("\\shell\\open\\command", "\\shell\\open", "\\shell", ""):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, base + suffix)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows protocol registration")
def test_source_callback_older_launcher_exit_keeps_newer_registration():
    import winreg
    from contextlib import ExitStack, suppress
    from uuid import uuid4

    from buddy_desktop.source_callback import windows_source_callback

    scheme = "buddy-test-" + uuid4().hex
    base = "Software\\Classes\\" + scheme
    path = base + "\\shell\\open\\command"
    try:
        older = ExitStack()
        older.enter_context(windows_source_callback([sys.executable], scheme=scheme))
        with windows_source_callback([sys.executable], scheme=scheme):
            older.close()
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                assert winreg.QueryValueEx(key, "")[0].endswith('--auth-callback "%1"')
    finally:
        for suffix in ("\\shell\\open\\command", "\\shell\\open", "\\shell", ""):
            with suppress(OSError):
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, base + suffix)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shell URL activation")
def test_windows_shell_launches_source_callback_and_relays_to_pending_login(qapp, qtbot, tmp_path):
    import os
    from pathlib import Path

    from buddy_desktop.source_callback import windows_source_callback

    python = Path(sys.executable).with_name("pythonw.exe")
    server = NativeCallbackServer(directory=tmp_path)
    assert server.start()
    script = (
        "import sys; from pathlib import Path; from PySide6.QtCore import QCoreApplication; "
        "from buddy_desktop.native_callback import NativeCallbackServer; "
        "app=QCoreApplication([]); "
        f"server=NativeCallbackServer(directory=Path({str(tmp_path)!r})); "
        "assert not server.start(sys.argv[-1])"
    )
    try:
        with (
            windows_source_callback([str(python), "-c", script]),
            pending_callback.waiting("shell-source") as attempt,
        ):
            os.startfile(CALLBACK + "?state=shell-source&code=synthetic-shell-code")
            qtbot.waitUntil(attempt.received.is_set, timeout=10000)
            assert attempt.code == "synthetic-shell-code"
    finally:
        server.close()


@pytest.mark.parametrize("finish", ["payload", "peer-close", "server-close", "timeout"])
def test_native_connection_resources_are_released(qapp, qtbot, tmp_path, finish):
    from PySide6.QtCore import QCoreApplication, QEvent, QTimer
    from PySide6.QtNetwork import QLocalSocket
    from shiboken6 import isValid

    server = NativeCallbackServer(directory=tmp_path)
    assert server.start()
    client = QLocalSocket()
    activations = []
    server.activated.connect(lambda: activations.append(True))
    try:
        client.connectToServer(server.name)
        assert client.waitForConnected(1500)
        qtbot.waitUntil(lambda: len(server.buffers) == 1)
        accepted = next(iter(server.buffers))
        deadline = server.findChild(QTimer)
        assert deadline is not None and deadline.isActive()
        if finish == "payload":
            client.write(b"activate\n")
            assert client.waitForBytesWritten(1500)
        elif finish == "peer-close":
            client.abort()
        elif finish == "server-close":
            server.close()
        # An idle peer must be cut off by the real two-second timer.
        qtbot.waitUntil(lambda: not server.buffers, timeout=4000)
        assert not isValid(deadline) or not deadline.isActive()
        qtbot.waitUntil(lambda: client.state() == QLocalSocket.LocalSocketState.UnconnectedState)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not isValid(accepted) and not isValid(deadline)
        assert server.findChildren(QTimer) == []
        assert activations == ([True] if finish == "payload" else [])
        server.close()  # Cleanup remains idempotent after a timeout/disconnect.
    finally:
        client.abort()
        server.close()


def test_repeated_native_socket_teardown_survives_deferred_deletion(tmp_path):
    # Reproduce the failing lifecycle in a fresh process: a native crash fails this
    # test without killing pytest or being mistaken for a timeout/assertion retry.
    script = r"""
import gc
import sys
import time
from pathlib import Path
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtNetwork import QLocalSocket
from buddy_desktop.native_callback import NativeCallbackServer

app = QCoreApplication([])
for cycle in range(40):
    server = NativeCallbackServer(directory=Path(sys.argv[1]))
    assert server.start()
    clients = []
    for _ in range(4):
        client = QLocalSocket()
        client.connectToServer(server.name)
        assert client.waitForConnected(1500)
        clients.append(client)
        app.processEvents()
    deadline = time.monotonic() + 5
    while len(server.buffers) < 4 and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.001)
    assert len(server.buffers) == 4
    for client in clients:
        client.write(b'activate\n' if cycle % 2 else b'partial')
        assert client.waitForBytesWritten(1500)
    app.processEvents()
    server.close()
    assert not server.buffers and not server.timers
    server.deleteLater()
    for client in clients:
        client.abort()
        client.deleteLater()
    clients.clear()
    del client, server
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()
    app.processEvents()
"""
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
