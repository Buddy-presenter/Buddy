"""Profile privacy, serialized lifecycle, typed transport and accessible navigation."""

import time
from threading import Event, get_ident
from uuid import uuid4

import pytest
from frontend.tests.test_auth import CONFIG, Store
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QScrollArea

from buddy_desktop import auth
from buddy_desktop.visuals import apply_theme
from buddy_identity.config import IdentityError, SessionExpired


@pytest.fixture
def profile(desktop_window, monkeypatch, qapp):
    window = desktop_window()
    account = window.account
    session = auth.AccountSession(CONFIG, window.config.api_url, Store())
    session.tokens = auth.Tokens("synthetic-access", "synthetic-refresh", time.monotonic() + 300)
    account.session = session
    account.account_id = str(uuid4())
    response = {
        "data": {"id": account.account_id, "email": "person@example.test", "email_verified": True}
    }
    calls = []

    def respond(request):
        calls.append((get_ident(), request))
        return response

    monkeypatch.setattr(auth, "request_json", respond)
    return window, response, calls


def open_profile(window, qtbot):
    qtbot.mouseClick(window.account_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not window.account.runner.busy)


@pytest.mark.parametrize("size", [(800, 600), (1200, 800)])
def test_verified_profile_navigation_keyboard_and_layout(profile, qtbot, qapp, size):
    window, response, calls = profile
    old_style, old_palette = qapp.styleSheet(), qapp.palette()
    apply_theme(qapp)
    try:
        window.resize(*size)
        response["data"]["email"] = "long" * 64 + "@example.test"
        window.account_button.setFocus()
        qtbot.keyClick(window.account_button, Qt.Key.Key_Space)
        qtbot.waitUntil(lambda: not window.account.runner.busy)
        page = window.profile
        assert window.pages.currentIndex() == 4 and window.navigation.count() == 4
        assert page.email.text() == response["data"]["email"]
        assert page.verification.text() == "Email verified"
        assert page.email.textFormat() == Qt.TextFormat.PlainText
        assert len(calls) == 1 and calls[0][0] != get_ident()
        scroll = window.pages.currentWidget()
        assert isinstance(scroll, QScrollArea)
        qapp.processEvents()
        assert scroll.horizontalScrollBar().maximum() == 0
        assert page.refresh.accessibleName() and page.controls.accessibleName()
        window.navigation.setCurrentRow(3)
        assert page.email.text() == ""
        window._show_account()
        qtbot.waitUntil(lambda: not window.account.runner.busy)
        page.controls.click()
        assert window.pages.currentIndex() == 0 and page.email.text() == ""
    finally:
        qapp.setStyleSheet(old_style)
        qapp.setPalette(old_palette)


@pytest.mark.parametrize("failure", [IdentityError("offline"), SessionExpired("revoked")])
def test_profile_errors_clear_private_data_and_recover(profile, monkeypatch, qtbot, failure):
    window, response, _ = profile
    open_profile(window, qtbot)
    session = window.account.session
    assert window.profile.email.text()
    monkeypatch.setattr(session, "profile", lambda _: (_ for _ in ()).throw(failure))
    window.profile.refresh.click()
    qtbot.waitUntil(lambda: not window.account.runner.busy)
    assert window.profile.email.text() == ""
    if isinstance(failure, SessionExpired):
        assert session.tokens is None and window.account.account_id is None
        assert "expired" in window.profile.state.message.text()
    else:
        assert session.tokens is not None
        assert "unavailable" in window.profile.state.message.text().lower()
        monkeypatch.setattr(session, "profile", lambda _: auth.AccountProfile(**response["data"]))
        window.profile.state.retry.click()
        qtbot.waitUntil(lambda: not window.account.runner.busy)
        assert window.profile.email.text() == response["data"]["email"]


@pytest.mark.parametrize("boundary", ["navigate", "expire", "shutdown", "backend"])
def test_late_profile_results_cannot_restore_private_state(profile, monkeypatch, qtbot, boundary):
    window, response, _ = profile
    entered, release = Event(), Event()

    def blocked(request):
        entered.set()
        assert release.wait(5)
        return response

    monkeypatch.setattr(auth, "request_json", blocked)
    window._show_account()
    try:
        qtbot.waitUntil(entered.is_set)
        assert "Loading" in window.profile.state.title.text()
        if boundary == "navigate":
            window.navigation.setCurrentRow(0)
        elif boundary == "expire":
            window.account.expire_session("Your session expired. Please sign in again.")
        elif boundary == "shutdown":
            window.close()
        else:
            # Backend changes are serialized until the in-flight request ends.
            window.save_settings("http://127.0.0.1:9000")
            assert window.config.api_url != "http://127.0.0.1:9000"
            window.navigation.setCurrentRow(0)
        release.set()
        qtbot.waitUntil(lambda: not window.account.runner.busy)
        if boundary == "backend":
            window.save_settings("http://127.0.0.1:9000")
            assert window.account.account_id is None
        assert window.profile.email.text() == ""
    finally:
        release.set()
        window.account.runner.wait_for_shutdown()


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"email": "invalid", "email_verified": True},
        {"email": "person@example.test", "email_verified": "true"},
        {"email": "person@example.test", "email_verified": False},
    ],
)
def test_older_or_malformed_profile_is_unavailable_without_breaking_login(profile, data):
    window, response, _ = profile
    response["data"] = {"id": window.account.account_id, **data}
    session = window.account.session
    assert session.account() == window.account.account_id
    with pytest.raises(IdentityError):
        session.profile(window.account.account_id)


def test_profile_rejects_another_account_response(profile):
    window, response, _ = profile
    response["data"]["id"] = str(uuid4())
    with pytest.raises(SessionExpired):
        window.account.session.profile(window.account.account_id)


def test_signed_out_profile_has_account_guidance(desktop_window, qtbot):
    window = desktop_window()
    open_profile(window, qtbot)
    assert "Sign in" in window.profile.state.title.text()
    assert window.profile.email.text() == "" and not window.profile.refresh.isEnabled()
    window.profile.controls.click()
    assert window.pages.currentIndex() == 0
