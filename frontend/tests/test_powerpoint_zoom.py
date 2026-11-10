"""Manual native zoom boundaries; no Office or global input is used by unit tests."""

from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from buddy_desktop.powerpoint_contract import PowerPointAction, PowerPointError, PresentationInfo
from buddy_desktop.powerpoint_zoom import WindowsPowerPointZoom, focus_point
from buddy_desktop.slide_contract import Region


def info():
    return PresentationInfo("deck.pptx", "deck.pptx", 1, 3, 42, 256, 960, 540)


def test_native_zoom_rejects_other_platforms_before_loading_windows_bindings(monkeypatch):
    import buddy_desktop.powerpoint_zoom as zoom

    monkeypatch.setattr(zoom.sys, "platform", "darwin")
    with pytest.raises(PowerPointError, match="Windows only"):
        WindowsPowerPointZoom()


@pytest.mark.parametrize(
    "client,expected",
    [
        ((0, 0, 1920, 1080), (480, 270)),
        ((0, 0, 2560, 1600), (640, 440)),
        ((-1920, -200, 0, 880), (-1440, 70)),
        ((100, 200, 900, 800), (300, 388)),
    ],
)
def test_physical_focus_mapping_accounts_for_letterbox_and_negative_origins(client, expected):
    assert focus_point(info(), Region(120, 60, 240, 150), client) == expected


@pytest.mark.parametrize("kind", ["zoom_region", "reset_zoom"])
def test_zoom_actions_are_typed_and_source_bound(kind):
    region = Region(100, 100, 200, 120) if kind == "zoom_region" else None
    action = PowerPointAction(kind, info(), region=region)
    action.validate()
    with pytest.raises(PowerPointError):
        replace(action, target=replace(info(), window_id=0)).validate()
    if kind == "zoom_region":
        for invalid in (None, Region(-1, 0, 100, 100), Region(0, 0, 5, 100)):
            with pytest.raises(PowerPointError):
                replace(action, region=invalid).validate()
    else:
        with pytest.raises(PowerPointError):
            replace(action, region=Region(0, 0, 100, 100)).validate()


def test_focus_guard_aborts_before_input_when_slide_or_foreground_changes():
    driver = WindowsPowerPointZoom.__new__(WindowsPowerPointZoom)
    driver.gui = SimpleNamespace(
        IsWindow=lambda hwnd: True, IsIconic=lambda hwnd: False, GetForegroundWindow=lambda: 99
    )
    with pytest.raises(PowerPointError, match="lost focus"):
        driver._guard(info(), lambda: None)
    driver.gui.GetForegroundWindow = lambda: 42

    def stale():
        raise PowerPointError("stale slide")

    with pytest.raises(PowerPointError, match="stale slide"):
        driver._guard(info(), stale)


@pytest.fixture
def native_driver():
    driver = WindowsPowerPointZoom.__new__(WindowsPowerPointZoom)
    state = {"foreground": 42, "covered": False, "monitors": 1, "plans": []}
    driver.gui = SimpleNamespace(
        IsWindow=lambda _: True,
        IsIconic=lambda _: False,
        GetForegroundWindow=lambda: state["foreground"],
        GetClientRect=lambda _: (0, 0, 1920, 1080),
        ClientToScreen=lambda _, point: point,
        WindowFromPoint=lambda _: 99 if state["covered"] else 42,
        IsChild=lambda *_: False,
    )
    driver.api = SimpleNamespace(
        GetAsyncKeyState=lambda _: 0,
        GetSystemMetrics=lambda n: {0: 1920, 1: 1080, 80: state["monitors"]}[n],
    )
    driver._physical_pixels = nullcontext
    driver.magnifier = SimpleNamespace(
        release=lambda: None, apply=lambda hwnd, plan: state["plans"].append((hwnd, plan))
    )
    return driver, state


def test_direct_zoom_applies_one_transform_without_keyboard_input(native_driver):
    driver, state = native_driver
    driver.zoom(info(), Region(120, 60, 240, 150), lambda: None)
    assert len(state["plans"]) == 1
    assert state["plans"][0][0] == 42
    assert state["plans"][0][1].scale > 1


def test_direct_zoom_refuses_other_monitors_or_obscured_slide(native_driver):
    driver, state = native_driver
    state["monitors"] = 2
    with pytest.raises(PowerPointError, match="single display"):
        driver.zoom(info(), Region(120, 60, 240, 150), lambda: None)
    state["monitors"] = 1
    state["covered"] = True
    with pytest.raises(PowerPointError, match="covered"):
        driver.zoom(info(), Region(120, 60, 240, 150), lambda: None)
    assert not state["plans"]
