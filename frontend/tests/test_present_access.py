"""#230: Present gets one refreshing token provider per sign-in, never a token copy."""

from types import SimpleNamespace

import pytest

from buddy_desktop.library_service import LibraryAccessExpired
from buddy_desktop.services import ServiceError
from buddy_desktop.window import MainWindow
from buddy_identity.config import IdentityError, SessionExpired


class Session:
    def __init__(self, *results):
        self.results = list(results)
        self.margins = []

    def fresh_access(self, margin: float = 60) -> str:
        self.margins.append(margin)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def provider(window, session):
    return MainWindow._access_provider(window, session)


def test_provider_refreshes_on_every_call_and_is_reused_for_the_same_sign_in():
    window = SimpleNamespace(_present_access=None)
    session = Session("first", "second")
    access = provider(window, session)
    assert provider(window, session) is access  # Same sign-in: the stream is not restarted.
    assert [access(), access()] == ["first", "second"]
    # Covers one 150-second event stream that the server re-checks on every poll.
    assert min(session.margins) >= 150
    assert provider(window, Session("other")) is not access


def test_provider_maps_expiry_and_transient_failures_to_service_errors():
    window = SimpleNamespace(_present_access=None)
    expired = provider(window, Session(SessionExpired("Please sign in again.")))
    with pytest.raises(LibraryAccessExpired):
        expired()
    offline = provider(window, Session(IdentityError("Network is unavailable.")))
    with pytest.raises(ServiceError) as raised:
        offline()
    assert not isinstance(raised.value, LibraryAccessExpired)
