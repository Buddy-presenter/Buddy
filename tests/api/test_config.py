import pytest

from buddy_api.cli import main
from buddy_api.config import ConfigurationError, Environment, Settings


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"BUDDY_ENVIRONMENT": "invalid"},
        {"BUDDY_ENVIRONMENT": "production"},
        {"BUDDY_ENVIRONMENT": "development", "BUDDY_ALLOWED_HOSTS": "*"},
        {"BUDDY_ENVIRONMENT": "development", "BUDDY_ALLOWED_HOSTS": "user:secret@example.com"},
        {"BUDDY_ENVIRONMENT": "development", "BUDDY_ALLOWED_HOSTS": ""},
        {"BUDDY_ENVIRONMENT": "development", "BUDDY_PORT": "0"},
        {"BUDDY_ENVIRONMENT": "development", "BUDDY_PORT": "70000"},
        {"BUDDY_ENVIRONMENT": "development", "BUDDY_READINESS_TIMEOUT_SECONDS": "nan"},
        {"BUDDY_ENVIRONMENT": "development", "BUDDY_READINESS_TIMEOUT_SECONDS": "31"},
        {"BUDDY_ENVIRONMENT": "development", "BUDDY_PORT": "secret"},
    ],
)
def test_invalid_configuration_fails_safely(values):
    with pytest.raises(ConfigurationError) as error:
        Settings.from_environment(values)
    assert "secret" not in str(error.value)


def test_environment_configuration_and_loopback_defaults():
    config = Settings.from_environment({"BUDDY_ENVIRONMENT": "development"})
    assert config.bind_host == "127.0.0.1"
    assert config.port == 8000
    assert config.environment is Environment.DEVELOPMENT
    assert config.allowed_hosts == ("localhost", "127.0.0.1", "::1")


def test_startup_missing_configuration_exits_before_binding(monkeypatch, capsys):
    monkeypatch.delenv("BUDDY_ENVIRONMENT", raising=False)
    assert main() == 2
    assert "BUDDY_ENVIRONMENT" in capsys.readouterr().err
