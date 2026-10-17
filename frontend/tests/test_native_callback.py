"""Real Qt activation/IPC and the security boundary around pending OAuth attempts."""

import json
import plistlib
import subprocess
import sys
from threading import Event

import pytest
from PySide6.QtCore import QCoreApplication, QUrl
from PySide6.QtGui import QFileOpenEvent

from buddy_desktop.auth import browser_code
from buddy_desktop.native_callback import NativeCallbackServer, PendingCallback, pending_callback
from buddy_identity.config import CALLBACK, SIGNOUT_CALLBACK, IdentityError


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


def test_macos_url_event_reaches_only_the_pending_attempt(qapp, qtbot, tmp_path):
    server = NativeCallbackServer(directory=tmp_path)
    qapp.installEventFilter(server)
    try:
        with pending_callback.waiting("expected") as attempt:
            event = QFileOpenEvent(QUrl(CALLBACK + "?state=expected&code=from-macos"))
            QCoreApplication.sendEvent(qapp, event)
            assert attempt.code == "from-macos"
    finally:
        qapp.removeEventFilter(server)
        server.close()


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


def test_production_configuration_cannot_silently_package_staging_or_localhost(tmp_path):
    from frontend.scripts.release.build_desktop import deployment_config

    preview = deployment_config(None)
    with pytest.raises(ValueError, match="explicit"):
        deployment_config(None, production=True)
    path = tmp_path / "public.json"
    path.write_text(json.dumps(preview))
    with pytest.raises(ValueError, match="staging"):
        deployment_config(path, production=True)
    path.write_text(json.dumps({**preview, "BUDDY_API_URL": "http://127.0.0.1:8000"}))
    with pytest.raises(ValueError, match="HTTPS"):
        deployment_config(path)
    path.write_text(json.dumps({**preview, "password": "synthetic-never-package"}))
    with pytest.raises(ValueError, match="public"):
        deployment_config(path)
