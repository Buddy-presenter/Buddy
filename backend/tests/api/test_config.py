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


def test_agent_is_opt_in_and_credentials_are_backend_only():
    base = {"BUDDY_ENVIRONMENT": "development", "BUDDY_COHERE_API_KEY": "private-key"}
    assert Settings.from_environment(base).agent is None
    enabled = Settings.from_environment({**base, "BUDDY_AGENT_ENABLED": "true"})
    assert enabled.agent.monthly_limit == 100
    assert "private-key" not in repr(enabled)
    for values in (
        {"BUDDY_AGENT_ENABLED": "yes"},
        {"BUDDY_AGENT_ENABLED": "true", "BUDDY_COHERE_API_KEY": ""},
        {"BUDDY_AGENT_ENABLED": "true", "BUDDY_AGENT_MONTHLY_LIMIT": "101"},
    ):
        with pytest.raises(ConfigurationError):
            Settings.from_environment({**base, **values})
