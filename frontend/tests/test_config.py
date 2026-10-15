import pytest

from buddy_desktop.config import AppConfig, ConfigurationError, validate_api_url


@pytest.mark.parametrize(
    "value",
    [
        "file:///tmp/backend",
        "https:///missing-host",
        "http://remote.example",
        "https://user:secret@example.com",
        "https://example.com?token=secret",
        "https://example.com#fragment",
        "https://example.com:broken",
        "https://example.com:0",
        "https://example.com/path\nattack",
    ],
)
def test_unsafe_backend_url_is_rejected(value):
    with pytest.raises(ConfigurationError):
        validate_api_url(value)


@pytest.mark.parametrize("value", ["nan", "inf", "0", "31", "bad"])
def test_invalid_timeout_is_rejected(value):
    with pytest.raises(ConfigurationError):
        AppConfig.from_environment({"BUDDY_REQUEST_TIMEOUT_SECONDS": value})


def test_default_config_and_path_normalization():
    assert AppConfig.from_environment({}) == AppConfig("http://127.0.0.1:8000", 5)
    assert validate_api_url("https://example.com/buddy/") == "https://example.com/buddy"
    assert validate_api_url("http://[::1]:8000") == "http://[::1]:8000"


def test_invalid_startup_exits_before_creating_application(monkeypatch, capsys):
    from buddy_desktop.app import main

    monkeypatch.setenv("BUDDY_API_URL", "https://user:secret@example.com")
    assert main() == 2
    error = capsys.readouterr().err
    assert "configuration error" in error
    assert "secret" not in error
