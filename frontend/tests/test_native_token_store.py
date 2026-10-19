"""Native session persistence boundaries, using synthetic credentials only."""

import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from buddy_desktop import auth
from buddy_identity.config import IdentityError


class Vault:
    def __init__(self):
        self.values = {}
        self.writes = 0
        self.fail_write = None
        self.fail_delete = set()
        self.delete_attempts = []

    def get_password(self, service, username):
        return self.values.get(service)

    def set_password(self, service, username, value):
        self.writes += 1
        if self.writes == self.fail_write:
            raise RuntimeError("synthetic-secret")
        assert len(value.encode("utf-16-le")) <= 2560
        self.values[service] = value

    def delete_password(self, service, username):
        self.delete_attempts.append(service)
        if service in self.fail_delete:
            raise RuntimeError("synthetic-secret")
        del self.values[service]


@pytest.fixture
def windows_store(tmp_path, monkeypatch):
    # Replace this module's sys reference, not the interpreter's platform.
    monkeypatch.setattr(auth, "sys", SimpleNamespace(platform="win32"))
    vault = Vault()
    monkeypatch.setattr(auth.NativeTokenStore, "backend", lambda self: vault)
    return auth.NativeTokenStore("synthetic-account", tmp_path), vault


@pytest.mark.parametrize(
    "character,length",
    [
        ("x", 1),
        ("x", 512),  # Exactly one piece.
        ("x", 513),  # First spill into a second piece.
        ("x", 16300),  # Largest accepted token.
        ("\U0001f512", 1300),  # Surrogate pairs double the UTF-16 bytes per piece.
    ],
)
def test_windows_byte_limits_and_restart(windows_store, length, character):
    store, vault = windows_store
    token = character * length
    store.save(token, fresh=True)
    restored = auth.NativeTokenStore("synthetic-account", store.marker.parent)
    assert restored.read() == token
    assert len(vault.values) == 1 + (length + 511) // 512
    assert not list(store.marker.parent.iterdir())  # No token files or metadata files.
    restored.block()
    restored.delete()
    assert restored.read() is None
    assert not vault.values
    assert store.marker.read_text() == "signed out\n"


def test_legacy_session_migrates_and_rotation_removes_old_pieces(windows_store):
    store, vault = windows_store
    vault.values[store.service] = "legacy-token"
    assert store.read() == "legacy-token"
    store.save("a" * 3000, fresh=True)
    assert store.read() == "a" * 3000
    store.save("rotated")
    assert store.read() == "rotated"
    assert set(vault.values) == {store.service, store._chunk_service(0)}
    assert "a" * 512 not in vault.values.values()


@pytest.mark.parametrize("fail_write", [1, 2, 3, 4, 5])
def test_partial_write_or_publish_failure_blocks_resume(windows_store, fail_write):
    store, vault = windows_store
    store.save("previous", fresh=True)
    vault.fail_write = vault.writes + fail_write
    # pending marker, three parts, then final manifest
    with pytest.raises(IdentityError, match="Cannot save") as error:
        store.save("x" * 1300)
    assert "synthetic-secret" not in str(error.value)
    assert store.read() is None
    assert store.marker.exists()
    assert not vault.values
    vault.fail_write = None
    store.save("fresh-session", fresh=True)
    assert store.read() == "fresh-session"


@pytest.mark.parametrize(
    "damage", ["missing", "changed", "pending", "count", "hash", "version", "lost"]
)
def test_interrupted_or_corrupt_credentials_never_resume(windows_store, damage):
    store, vault = windows_store
    store.save("x" * 1300, fresh=True)
    if damage == "missing":
        del vault.values[store._chunk_service(1)]
    elif damage == "changed":
        vault.values[store._chunk_service(1)] = "different"
    elif damage == "pending":
        vault.values[store.service] = store._prefix + "pending"
    elif damage == "count":
        vault.values[store.service] = store._prefix + "999999:" + "0" * 64
    elif damage == "hash":
        vault.values[store.service] = store._prefix + "3:" + "0" * 64
    elif damage == "version":
        vault.values[store.service] = "Buddy-refresh-v2:unknown"
    else:
        del vault.values[store.service]
    if damage == "lost":
        assert store.read() is None
    else:
        with pytest.raises(IdentityError, match="Cannot read"):
            store.read()
    store.block()
    store.delete()
    assert not vault.values  # Includes orphaned pieces without readable metadata.


def test_delete_failure_still_removes_other_pieces_and_blocks_resume(windows_store):
    store, vault = windows_store
    store.save("x" * 1300, fresh=True)
    failed = store._chunk_service(0)
    vault.fail_delete.add(failed)
    store.block()
    with pytest.raises(IdentityError, match="Cannot remove") as error:
        store.delete()
    assert "synthetic-secret" not in str(error.value)
    assert set(vault.values) == {failed}
    assert store._chunk_service(2) in vault.delete_attempts
    assert store.read() is None
    vault.fail_delete.clear()
    store.delete()
    assert not vault.values


def test_rotation_cleanup_failure_cannot_publish_new_session(windows_store):
    store, vault = windows_store
    store.save("x" * 3000, fresh=True)
    failed = store._chunk_service(5)
    vault.fail_delete.add(failed)
    with pytest.raises(IdentityError, match="Cannot save"):
        store.save("rotated")
    assert store.read() is None
    assert set(vault.values) == {failed}


def test_only_fresh_signin_clears_signout_marker(windows_store):
    store, _ = windows_store
    store.block()
    store.save("rotated")
    assert store.read() is None
    store.save("new-login", fresh=True)
    assert store.read() == "new-login"


def test_other_accounts_are_isolated(windows_store):
    store, _ = windows_store
    other = auth.NativeTokenStore("other-account", store.marker.parent)
    store.save("x" * 3000, fresh=True)
    other.save("other-session", fresh=True)
    store.delete()
    assert other.read() == "other-session"


@pytest.mark.parametrize("token", ["", "x" * 16301])
def test_unsupported_length_fails_safely(windows_store, token):
    store, vault = windows_store
    with pytest.raises(IdentityError, match="Cannot save"):
        store.save(token, fresh=True)
    assert store.read() is None
    assert not vault.values


def test_macos_keeps_single_keychain_entry(windows_store, monkeypatch):
    store, vault = windows_store
    monkeypatch.setattr(auth, "sys", SimpleNamespace(platform="darwin"))
    store.save("macos-session", fresh=True)
    assert store.read() == "macos-session"
    assert vault.values == {store.service: "macos-session"}
    store.delete()
    assert not vault.values


def test_native_storage_signin_restart_rotation_and_signout(windows_store, monkeypatch):
    from buddy_identity.config import IdentityConfig

    store, vault = windows_store
    config = IdentityConfig(
        "eu-central-1",
        "eu-central-1_Example",
        "client123",
        "https://example.auth.eu-central-1.amazoncognito.com",
    )
    identifier = str(uuid4())
    refreshes = iter(["first-" * 500, "rotated-" * 600])

    def respond(request, **kwargs):
        if request.full_url.endswith("/token"):
            return {
                "access_token": "synthetic-access",
                "refresh_token": next(refreshes),
                "token_type": "Bearer",
                "expires_in": 300,
            }
        if request.full_url.endswith("/me"):
            return {"data": {"id": identifier}}
        return {}

    monkeypatch.setattr(auth, "request_json", respond)
    monkeypatch.setattr(auth, "browser_code", lambda *args, **kwargs: "synthetic-code")
    session = auth.AccountSession(config, "https://api.example.com", store)
    assert session.sign_in() == identifier
    restored_store = auth.NativeTokenStore("synthetic-account", store.marker.parent)
    restored = auth.AccountSession(config, session.api_url, restored_store)
    assert restored.resume() == identifier
    assert restored_store.read() == "rotated-" * 600
    restored.sign_out()
    assert restored_store.read() is None
    assert not vault.values


@pytest.mark.skipif(sys.platform != "win32", reason="Requires actual Windows Credential Manager")
def test_real_windows_vault_long_token_roundtrip_and_cleanup(tmp_path):
    # Random isolated service names; never touch an account or existing credentials.
    identity = "Buddy-vault-regression-" + uuid4().hex
    store = auth.NativeTokenStore(identity, tmp_path)
    try:
        store.save("synthetic-" * 1500, fresh=True)
        assert auth.NativeTokenStore(identity, tmp_path).read() == "synthetic-" * 1500
        store.save("rotated-synthetic")
        assert store.read() == "rotated-synthetic"
    finally:
        store.block()
        store.delete()
    assert store.read() is None
    backend = store.backend()
    assert backend.get_password(store.service, "refresh") is None
    assert all(
        backend.get_password(store._chunk_service(index), "refresh") is None
        for index in range(store._max_chunks)
    )
