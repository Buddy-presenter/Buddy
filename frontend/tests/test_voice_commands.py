from buddy_desktop.browser_search import google_search_url, launch_chrome_search
from buddy_desktop.voice_commands import (
    ReturnToPresentation,
    VoiceCommandRouter,
    WebSearch,
    parse_voice_command,
)


def test_parses_explicit_general_and_image_search_commands():
    assert parse_voice_command("Search for cute cat pictures") == WebSearch("cute cat pictures")
    assert parse_voice_command("Open Chrome and search for images of cute cats") == WebSearch(
        "cute cats", images=True
    )
    assert parse_voice_command("Search for quarterly revenue forecast 2027") == WebSearch(
        "quarterly revenue forecast 2027"
    )
    assert parse_voice_command("Buddy, search for images of 2026 eclipse") == WebSearch(
        "2026 eclipse", images=True
    )


def test_parses_clear_return_phrases_only():
    for phrase in (
        "Return to the presentation",
        "go back to the presentation",
        "Let's go back to the presentation",
        "let us go back to presentation please",
        "Buddy, return to presentation",
    ):
        assert parse_voice_command(phrase) == ReturnToPresentation()
    assert parse_voice_command("We previously saw in marketing strategies") is None
    assert parse_voice_command("Let's talk about going back to the presentation") is None


def test_search_command_transcripts_are_suppressed_until_final_and_routed_once():
    router = VoiceCommandRouter()
    partial = router.handle(
        {
            "text": "Open Chrome and search for images of cute",
            "segment_id": "segment",
            "final": False,
        }
    )
    assert partial.suppress_agent and partial.command is None
    final = router.handle(
        {
            "text": "Open Chrome and search for images of cute cats.",
            "segment_id": "segment",
            "final": True,
        }
    )
    assert final.suppress_agent
    assert final.command == WebSearch("cute cats", images=True)
    ordinary = router.handle(
        {
            "text": "Marketing strategies were mentioned earlier.",
            "segment_id": "other",
            "final": True,
        }
    )
    assert not ordinary.suppress_agent and ordinary.command is None


def test_return_router_does_not_swallow_slide_navigation_commands():
    router = VoiceCommandRouter()
    partial = router.handle({"text": "Let's go back to", "segment_id": "slide-nav", "final": False})
    assert not partial.suppress_agent
    final = router.handle(
        {"text": "Let's go back to slide one", "segment_id": "slide-nav", "final": True}
    )
    assert not final.suppress_agent and final.command is None

    previous = router.handle(
        {"text": "Return to the previous slide", "segment_id": "prev", "final": True}
    )
    assert not previous.suppress_agent and previous.command is None


def test_partial_router_does_not_swallow_normal_sentences_with_similar_openings():
    router = VoiceCommandRouter()
    for text in (
        "Let's introduce the objectives",
        "Please let me show the chart",
        "Open Chrome is already installed",
        "Return on investment increased",
    ):
        result = router.handle({"text": text, "segment_id": text, "final": False})
        assert not result.suppress_agent


def test_incomplete_or_overlong_search_is_not_opened():
    assert parse_voice_command("Search for") is None
    assert parse_voice_command("Search for " + ("cat " * 80)) is None
    assert parse_voice_command("The search for cute cats begins now") is None


def test_google_search_url_encodes_untrusted_spoken_query():
    url = google_search_url("cats & dogs? site:example.com")
    assert url == "https://www.google.com/search?q=cats+%26+dogs%3F+site%3Aexample.com"
    image_url = google_search_url("cute cats", images=True)
    assert image_url == "https://www.google.com/search?q=cute+cats&tbm=isch"


def test_chrome_launch_uses_argument_list_and_reports_missing_browser(tmp_path, monkeypatch):
    from buddy_desktop import browser_search

    app = tmp_path / "Google" / "Chrome" / "Application"
    app.mkdir(parents=True)
    executable = app / "chrome.exe"
    executable.write_bytes(b"test executable placeholder")
    monkeypatch.setattr(browser_search, "_registered_windows_chrome", lambda: None)
    launched = []
    monkeypatch.setattr(
        browser_search.QProcess,
        "startDetached",
        lambda program, arguments: launched.append((program, arguments)) or (True, 123),
    )
    result = launch_chrome_search(
        "cats & dogs", images=True, system="win32", environ={"LOCALAPPDATA": str(tmp_path)}
    )
    assert result.opened
    assert launched == [
        (
            str(executable),
            ["--new-tab", "https://www.google.com/search?q=cats+%26+dogs&tbm=isch"],
        )
    ]

    monkeypatch.setattr(browser_search, "_registered_windows_chrome", lambda: None)
    missing = launch_chrome_search("cute cats", system="win32", environ={})
    assert not missing.opened
    assert "could not be found" in missing.message
