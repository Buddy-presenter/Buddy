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
        "cute cats", images=True, browser=True
    )
    assert parse_voice_command("Search for quarterly revenue forecast 2027") == WebSearch(
        "quarterly revenue forecast 2027"
    )
    assert parse_voice_command("Buddy, search for images of 2026 eclipse") == WebSearch(
        "2026 eclipse", images=True
    )


def test_search_self_corrections_replace_the_query_without_losing_search_mode():
    assert parse_voice_command("Search for images of dogs, I mean cute cats") == WebSearch(
        "cute cats", images=True
    )
    assert parse_voice_command(
        "Search for quarterly results—actually, revenue growth"
    ) == WebSearch("revenue growth")
    assert parse_voice_command("Search for what actually causes inflation") == WebSearch(
        "what actually causes inflation"
    )
    assert parse_voice_command("Search for images of day rather than night") == WebSearch(
        "day rather than night", images=True
    )
    assert parse_voice_command("Search for what I mean by opportunity cost") == WebSearch(
        "what I mean by opportunity cost"
    )
    assert parse_voice_command('Search for the phrase "I mean"') == WebSearch('the phrase "I mean"')
    assert parse_voice_command(
        "Search for images of dogs, I mean cats, actually kittens"
    ) == WebSearch("kittens", images=True)
    assert parse_voice_command("Search for images of dogs, I mean") is None
    assert parse_voice_command("Search for images of") is None
    assert parse_voice_command("Search for the") is None
    assert parse_voice_command("Search for something") is None


def test_parses_definition_questions_and_common_image_search_phrasings():
    for phrase, expected in (
        ("What's the definition of opportunity cost", "definition of opportunity cost"),
        ("What does amortization mean", "definition of amortization"),
        ("Lookup the definition of compound interest", "definition of compound interest"),
        ("What is meant by machine learning", "definition of machine learning"),
        ("Define gross domestic product", "definition of gross domestic product"),
    ):
        assert parse_voice_command(phrase) == WebSearch(expected)

    for phrase in (
        "Show me pictures of solar panels",
        "Could you find images of a wind turbine",
        "Look for photos of an electric car",
        "Search Google Images for cute cats",
        "Would you search the web for images of the moon",
    ):
        command = parse_voice_command(phrase)
        assert isinstance(command, WebSearch)
        assert command.images


def test_incomplete_definition_question_waits_for_its_subject_and_meaning(monkeypatch):
    from buddy_desktop import voice_commands

    now = [10.0]
    monkeypatch.setattr(voice_commands, "monotonic", lambda: now[0])
    router = VoiceCommandRouter()
    pending = router.handle(
        {"text": "What does inflation", "segment_id": "question", "final": True}
    )
    continuation = router.handle({"text": "mean", "segment_id": "ending", "final": True})

    assert pending.suppress_agent and pending.search_pending and pending.command is None
    assert continuation.command == WebSearch("definition of inflation")


def test_correction_after_a_short_pause_still_replaces_image_query(monkeypatch):
    from buddy_desktop import voice_commands

    now = [10.0]
    monkeypatch.setattr(voice_commands, "monotonic", lambda: now[0])
    router = VoiceCommandRouter()
    initial = router.handle(
        {"text": "Search for images of cats", "segment_id": "initial", "final": True}
    )
    now[0] += 2.5
    corrected = router.handle({"text": "I mean dogs", "segment_id": "correction", "final": True})

    assert initial.command == WebSearch("cats", images=True)
    assert corrected.command == WebSearch("dogs", images=True)


def test_parses_clear_return_phrases_only():
    for phrase in (
        "Return to the presentation",
        "go back to the presentation",
        "Let's go back to the presentation",
        "let us go back to presentation please",
        "Buddy, return to presentation",
        "go back to the PDF",
        "return to my PDF",
        "switch back to my slides",
        "take me back to the deck",
        "go back to Buddy",
        "let's return to the presentation",
        "back to the PDF",
    ):
        assert parse_voice_command(phrase) == ReturnToPresentation()
    assert parse_voice_command("We previously saw in marketing strategies") is None
    assert parse_voice_command("Let's talk about going back to the presentation") is None
    assert parse_voice_command("go back to slide") is None


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
    assert final.command == WebSearch("cute cats", images=True, browser=True)
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

    unfinished = router.handle(
        {"text": "go back to slide", "segment_id": "unfinished-slide-nav", "final": True}
    )
    assert not unfinished.suppress_agent and unfinished.command is None


def test_router_accepts_a_quick_correction_as_a_replacement_search():
    router = VoiceCommandRouter()
    initial = router.handle(
        {
            "text": "Search for images of dogs",
            "segment_id": "initial-search",
            "final": True,
        }
    )
    corrected = router.handle(
        {"text": "I mean cute cats", "segment_id": "correction", "final": True}
    )
    assert initial.command == WebSearch("dogs", images=True)
    assert corrected.suppress_agent
    assert corrected.command == WebSearch("cute cats", images=True)


def test_router_keeps_a_correction_segment_suppressed_until_its_final(monkeypatch):
    from buddy_desktop import voice_commands

    now = [10.0]
    monkeypatch.setattr(voice_commands, "monotonic", lambda: now[0])
    router = VoiceCommandRouter()
    router.handle(
        {
            "text": "Search for images of dogs",
            "segment_id": "initial-search",
            "final": True,
        }
    )
    now[0] += 0.4
    partial = router.handle({"text": "I mean cute", "segment_id": "correction", "final": False})
    now[0] += 0.8
    final = router.handle({"text": "I mean cute cats", "segment_id": "correction", "final": True})
    assert partial.suppress_agent and partial.correction_pending
    assert final.suppress_agent
    assert final.command == WebSearch("cute cats", images=True)


def test_incomplete_image_search_waits_for_query_words_across_segments(monkeypatch):
    from buddy_desktop import voice_commands

    now = [10.0]
    monkeypatch.setattr(voice_commands, "monotonic", lambda: now[0])
    router = VoiceCommandRouter()
    pending = router.handle(
        {"text": "Search for images of", "segment_id": "incomplete", "final": True}
    )
    now[0] += 1.0
    partial = router.handle({"text": "cute cats", "segment_id": "query", "final": False})
    completed = router.handle({"text": "cute cats", "segment_id": "query", "final": True})

    assert pending.search_pending and pending.command is None and pending.suppress_agent
    assert partial.search_pending and partial.suppress_agent
    assert completed.command == WebSearch("cute cats", images=True)


def test_incomplete_search_does_not_capture_slide_navigation(monkeypatch):
    from buddy_desktop import voice_commands

    now = [10.0]
    monkeypatch.setattr(voice_commands, "monotonic", lambda: now[0])
    router = VoiceCommandRouter()
    pending = router.handle(
        {"text": "Search for images of", "segment_id": "incomplete", "final": True}
    )
    assert pending.search_pending

    for text in ("Let's go back to the first slide", "I want to go back to slide one"):
        now[0] += 0.2
        result = router.handle({"text": text, "segment_id": text, "final": True})
        assert not result.suppress_agent
        assert not result.search_pending
        assert result.command is None


def test_incomplete_image_search_ignores_standalone_correction_marker(monkeypatch):
    from buddy_desktop import voice_commands

    now = [10.0]
    monkeypatch.setattr(voice_commands, "monotonic", lambda: now[0])
    router = VoiceCommandRouter()
    router.handle({"text": "Search for images of", "segment_id": "search", "final": True})
    now[0] += 0.2
    correction = router.handle({"text": "I mean", "segment_id": "correction", "final": True})
    assert correction.search_pending and correction.suppress_agent
    assert correction.command is None
    now[0] += 0.2
    completed = router.handle({"text": "cute cats", "segment_id": "query", "final": True})
    assert completed.command == WebSearch("cute cats", images=True)


def test_incomplete_self_correction_waits_for_query_words_across_final_segments(monkeypatch):
    from buddy_desktop import voice_commands

    now = [10.0]
    monkeypatch.setattr(voice_commands, "monotonic", lambda: now[0])
    router = VoiceCommandRouter()
    router.handle(
        {
            "text": "Search for images of dogs",
            "segment_id": "initial-search",
            "final": True,
        }
    )
    now[0] += 0.2
    incomplete = router.handle({"text": "I mean", "segment_id": "correction-marker", "final": True})
    now[0] += 0.8
    completed = router.handle(
        {"text": "cute cats", "segment_id": "correction-query", "final": True}
    )

    assert incomplete.correction_pending and incomplete.correction_waiting_for_query
    assert incomplete.command is None
    assert completed.command == WebSearch("cute cats", images=True)


def test_correction_phrase_is_ordinary_speech_without_a_recent_search():
    router = VoiceCommandRouter()
    result = router.handle({"text": "I mean cute cats", "segment_id": "ordinary", "final": True})
    assert not result.suppress_agent
    assert result.command is None


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


def test_everyday_presenting_phrases_never_start_a_web_search():
    # #250: these belong to Buddy's slide commands or ordinary narration.
    for phrase in (
        "Show me the revenue chart",
        "Show me the whole slide",
        "Show the whole slide",
        "Find the margin on this slide",
        "Pull up the cost table",
        "Can you show me the next slide",
        "Find out more on our website",
        "Look for value in every customer",
        "Get me the next slide",
        "Zoom into the table",
        "Highlight the total",
        "Next slide",
    ):
        assert parse_voice_command(phrase) is None, phrase


def test_explicit_online_phrasings_still_search():
    assert parse_voice_command("Search online for electric cars") == WebSearch("electric cars")
    assert parse_voice_command("Look up EBITDA") == WebSearch("EBITDA")
    assert parse_voice_command("Google the Eiffel Tower") == WebSearch("the Eiffel Tower")
    assert parse_voice_command("Can you find electric car prices online") == WebSearch(
        "electric car prices"
    )
    assert parse_voice_command("Show me Tesla news on Google") == WebSearch("Tesla news")
    assert parse_voice_command("Find some pictures of red pandas") == WebSearch(
        "red pandas", images=True
    )


def test_partial_everyday_phrases_are_not_held_back_from_slide_decisions():
    router = VoiceCommandRouter()
    for text in ("Show me the", "Find the", "Pull up the"):
        result = router.handle({"text": text, "segment_id": text, "final": False})
        assert not result.suppress_agent, text


def test_searches_use_the_panel_unless_chrome_is_named():
    # #250: answers and images show in Buddy's panel; Chrome only when asked for.
    assert parse_voice_command("Show me images of cats") == WebSearch("cats", images=True)
    assert parse_voice_command("Search for cats") == WebSearch("cats")
    assert parse_voice_command("Open Chrome and search for cats") == WebSearch("cats", browser=True)


def test_close_the_panel_by_voice():
    for phrase in ("Close the panel", "close the results", "Hide the images", "dismiss the search"):
        assert parse_voice_command(phrase) == ReturnToPresentation(), phrase


def test_scroll_the_panel_by_voice():
    from buddy_desktop.voice_commands import ScrollPanel

    for phrase in ("Scroll down", "scroll down a bit", "Read more", "show more"):
        assert parse_voice_command(phrase) == ScrollPanel(1), phrase
    for phrase in ("Scroll up", "go up", "Go back up", "back to the top"):
        assert parse_voice_command(phrase) == ScrollPanel(-1), phrase
    assert parse_voice_command("Go back") is None  # slide navigation stays with Buddy


def test_web_search_phrasings():
    assert parse_voice_command("Web search images of dogs") == WebSearch("dogs", images=True)
    assert parse_voice_command("web search dogs") == WebSearch("dogs")
    assert parse_voice_command("Do a web search for electric cars") == WebSearch("electric cars")
    assert parse_voice_command("Can you do a web search on EBITDA") == WebSearch("EBITDA")
