import pytest

from buddy_desktop.navigation import navigation_intent


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("Go to slide 8", ("absolute", 8)),
        ("Let's go back to slide one", ("absolute", 1)),
        ("Let's go back to the first slide", ("absolute", 1)),
        ("Please move to the twenty first slide", ("absolute", 21)),
        ("Go to slide 7 where we explain the results", ("absolute", 7)),
        ("Let's go to the next slide", ("relative", 1)),
        ("Please switch to the previous page", ("relative", -1)),
        ("Go back one slide", ("relative", -1)),
        ("Do not go to slide 8", None),
        ("If we go to slide 8, we lose context", None),
        ("Go to slide 8 if there is time", None),
        ("Let's go to slide 8, actually stay here", None),
        ("Should we go to slide 8?", None),
        ('She said "go to slide 8"', None),
        ("We previously saw slide 8", None),
    ],
)
def test_local_navigation_is_conservative(phrase, expected):
    assert navigation_intent(phrase) == expected


def test_spoken_figures_and_shown_figures():
    from buddy_desktop.navigation import shows_figure, spoken_figures

    assert spoken_figures("Revenue came in at 48 million") == (("48", "m"),)
    assert spoken_figures("we grew to 48.5 million") == (("48.5", "m"),)
    assert spoken_figures("1,200 users joined") == (("1200", None),)
    assert spoken_figures("we have 3 offices") == ()
    assert spoken_figures("was it 48 million?") == ()
    assert spoken_figures("not 48 million") == ()
    assert shows_figure("Revenue $48M", ("48", "m"))
    assert shows_figure("48 million", ("48", "m"))
    assert not shows_figure("Revenue $48.5M", ("48", "m"))
    assert shows_figure("1,200 users", ("1200", None))
