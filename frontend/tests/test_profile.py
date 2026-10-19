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
    window._show_account()
    qtbot.waitUntil(lambda: not window.account.runner.busy)


def test_verified_profile_navigation_keyboard_and_layout(profile, qtbot, qapp):
    window, response, calls = profile
    old_style, old_palette = qapp.styleSheet(), qapp.palette()
    apply_theme(qapp)
    try:
        window.resize(800, 600)  # The minimum window size is the binding layout constraint.
        response["data"]["email"] = "long" * 64 + "@example.test"
        window.account_button.setFocus()
        qtbot.keyClick(window.account_button, Qt.Key.Key_Space)
        qtbot.waitUntil(lambda: not window.account.runner.busy)
        qtbot.mouseClick(window.profile_action, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(lambda: not window.account.runner.busy)
        page = window.profile
        assert window.pages.currentIndex() == 4 and window.navigation.count() == 4
        assert page.email.text() == response["data"]["email"]
        assert page.verification.text() == "Email verified"
        assert page.email.textFormat() == Qt.TextFormat.PlainText
        assert calls and all(call[0] != get_ident() for call in calls)
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
        assert window.account_popover.isVisible()
        assert window.pages.currentIndex() == 4
        window.account_popover.reject()
        window.navigation.setCurrentRow(0)
        assert page.email.text() == ""
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
    assert window.account_popover.isVisible()
    assert window.pages.currentIndex() == 4


def test_browser_sign_in_remains_cancellable_after_popup_dismisses(
    profile, monkeypatch, qtbot
):
    window, _, _ = profile
    account = window.account
    assert account.session is not None
    account.session.tokens = None
    account.account_id = None
    account.idle()
    window._account_identity()
    entered_browser, cancelled_browser = Event(), Event()

    def wait_for_browser_cancel(_url, _state, cancelled, **_kwargs):
        entered_browser.set()
        assert cancelled.wait(5), "Browser authentication did not receive cancellation"
        cancelled_browser.set()
        raise IdentityError("Sign-in was cancelled.")

    monkeypatch.setattr(auth, "browser_code", wait_for_browser_cancel)
    window._show_account_controls()
    qtbot.mouseClick(account.buttons["signin"], Qt.MouseButton.LeftButton)
    try:
        qtbot.waitUntil(entered_browser.is_set)
        qtbot.waitUntil(lambda: window.cancel_auth_button.isVisible())

        # The system browser takes focus and Qt dismisses the account popup. The
        # main-window cancel action must remain available while OAuth is pending.
        window.account_popover.hide()
        assert not window.account_popover.isVisible()
        assert window.cancel_auth_button.isVisible()
        assert window.cancel_auth_button.isEnabled()
        assert window.cancel_auth_button.accessibleName() == "Cancel browser sign-in"

        qtbot.mouseClick(window.cancel_auth_button, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(cancelled_browser.is_set)
        qtbot.waitUntil(lambda: not account.runner.busy)
        assert not window.cancel_auth_button.isVisible()
    finally:
        account.cancel_operation()
        account.runner.wait_for_shutdown()


# Sign-up and recovery share sign-in's browser path; resume is the other token path.
@pytest.mark.parametrize("action", ["signin", "resume"])
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel", "navigate"])
def test_open_profile_during_authentication_tracks_completion(
    profile, monkeypatch, qtbot, action, outcome
):
    window, response, _ = profile
    account = window.account
    session = account.session
    session.tokens = None
    account.account_id = None
    session.store.save("saved-synthetic-refresh")
    entered, release = Event(), Event()
    loaded = []
    account.profile_loaded.connect(lambda *args: loaded.append(args))
    monkeypatch.setattr(auth, "browser_code", lambda *args, **kwargs: "synthetic-code")

    def respond(request, **kwargs):
        if request.full_url.endswith("/token"):
            entered.set()
            assert release.wait(5), "Authentication response was not released"
            if outcome == "failure":
                raise SessionExpired("Sign-in was rejected. Please sign in again.")
            return {
                "access_token": "synthetic-access",
                "refresh_token": "synthetic-refresh",
                "token_type": "Bearer",
                "expires_in": 300,
            }
        if request.full_url.endswith("/me"):
            return response
        return {}

    monkeypatch.setattr(auth, "request_json", respond)
    account.start(action)
    try:
        qtbot.waitUntil(entered.is_set)
        window._show_account()
        assert window.profile.email.text() == ""
        if outcome == "success":
            assert window.profile.state.title.text() == "Checking account"
        elif outcome == "cancel":
            account.cancel_operation()
        elif outcome == "navigate":
            window.navigation.setCurrentRow(0)
        release.set()
        qtbot.waitUntil(lambda: not account.runner.busy)
        if outcome == "success":
            assert window.profile.email.text() == response["data"]["email"]
            assert window.profile.verification.text() == "Email verified"
            assert len(loaded) == 1  # No manual refresh and no duplicate follow-up request.
        else:
            assert loaded == [] and window.profile.email.text() == ""
            if outcome in {"failure", "cancel"}:
                assert account.account_id is None
                assert window.profile.state.title.text() == "Sign in to view your profile"
            else:
                assert account.account_id == response["data"]["id"]
                assert not window.profile.active
    finally:
        release.set()
        account.runner.wait_for_shutdown()
