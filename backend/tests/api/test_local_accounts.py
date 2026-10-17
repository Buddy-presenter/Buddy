"""Local account setup must never silently point at a remote database."""

import os

import pytest
from backend.scripts.run_local_accounts import configure

from buddy_api.config import Settings


@pytest.fixture
def clean_local(monkeypatch):
    monkeypatch.setattr(
        os,
        "environ",
        {key: value for key, value in os.environ.items() if not key.startswith("BUDDY_")},
    )


def test_local_launcher_has_matching_real_identity_and_local_database(clean_local, monkeypatch):
    monkeypatch.setenv(
        "BUDDY_DATABASE_URL", "postgresql://buddy@127.0.0.1:5432/buddy?sslmode=disable"
    )
    configure()
    settings = Settings.from_environment()
    assert settings.identity is not None
    assert settings.identity.client_id == "s2k550ofnk3hipm63nteqte0"
    assert settings.bind_host == "127.0.0.1"
    assert settings.database is not None


@pytest.mark.parametrize("url", ["", "postgresql://buddy@remote.example/buddy?sslmode=verify-full"])
def test_local_launcher_rejects_missing_or_remote_database(clean_local, monkeypatch, url):
    monkeypatch.setenv("BUDDY_DATABASE_URL", url)
    with pytest.raises(ValueError, match="local PostgreSQL"):
        configure()


def test_local_launcher_refuses_public_binding(clean_local, monkeypatch):
    monkeypatch.setenv("BUDDY_DATABASE_URL", "postgresql://buddy@127.0.0.1:5432/buddy")
    monkeypatch.setenv("BUDDY_BIND_HOST", "0.0.0.0")
    with pytest.raises(ValueError, match="loopback"):
        configure()
