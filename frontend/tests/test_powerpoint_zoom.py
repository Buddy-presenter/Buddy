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
    state = {"selector": False, "cursor": (400, 300), "foreground": 42, "covered": False}
    events = []
    jumps = []
    driver.gui = SimpleNamespace(
        IsWindow=lambda hwnd: True,
        IsIconic=lambda hwnd: False,
        GetForegroundWindow=lambda: state["foreground"],
        SetForegroundWindow=lambda hwnd: state.update(foreground=hwnd),
        GetClientRect=lambda hwnd: (0, 0, 1920, 1080),
        ClientToScreen=lambda hwnd, point: point,
        WindowFromPoint=lambda point: 99 if state["covered"] else 42,
        IsChild=lambda parent, child: False,
    )

    def mouse(kind, *args):
        events.append(kind)
        if kind == 4:
            state["selector"] = False

    driver.api = SimpleNamespace(
        GetAsyncKeyState=lambda key: 0,
        GetCursorPos=lambda: state["cursor"],
        SetCursorPos=lambda point: state.update(cursor=point),
        mouse_event=mouse,
    )
    pattern = SimpleNamespace(Invoke=lambda: state.update(selector=True))
    button = SimpleNamespace(
        CurrentIsEnabled=True,
        GetCurrentPattern=lambda kind: SimpleNamespace(QueryInterface=lambda interface: pattern),
    )
    driver.types = SimpleNamespace(IUIAutomationInvokePattern=object())
    driver.uia = SimpleNamespace(
        ElementFromHandle=lambda hwnd: SimpleNamespace(FindFirst=lambda scope, condition: button),
        CreatePropertyCondition=lambda prop, value: (prop, value),
    )
    driver._physical_pixels = nullcontext
    driver._zoom_window = lambda hwnd: 77 if state["selector"] else None
    window = SimpleNamespace(View=SimpleNamespace(GotoSlide=lambda *args: jumps.append(args)))
    return driver, state, events, jumps, lambda: window


def test_native_zoom_waits_for_selector_and_confirms_completion(native_driver):
    driver, state, events, jumps, validate = native_driver
    driver.zoom(info(), Region(120, 60, 240, 150), validate)
    assert events == [2, 4]
    assert jumps == [(1, 0)]
    assert not state["selector"]
    assert state["cursor"] == (400, 300)


def test_occlusion_or_missing_native_control_never_clicks_the_slide(native_driver):
    driver, state, events, _, validate = native_driver
    state["covered"] = True
    with pytest.raises(PowerPointError, match="covered"):
        driver.zoom(info(), Region(120, 60, 240, 150), validate)
    assert events == []
    state["covered"] = False
    driver.uia.ElementFromHandle = lambda hwnd: SimpleNamespace(FindFirst=lambda *args: None)
    with pytest.raises(PowerPointError, match="does not expose"):
        driver.zoom(info(), Region(120, 60, 240, 150), validate)
    assert events == []


def test_reset_is_safe_when_already_full_size_and_sends_no_escape(native_driver):
    driver, _, events, jumps, validate = native_driver
    driver.reset(info(), validate)
    driver.reset(info(), validate)
    assert jumps == [(1, 0), (1, 0)]
    assert events == []


def test_held_modifiers_prevent_native_zoom_input(native_driver):
    driver, _, events, jumps, validate = native_driver
    driver.api.GetAsyncKeyState = lambda key: 0x8000 if key == 17 else 0
    with pytest.raises(PowerPointError, match="Release"):
        driver.zoom(info(), Region(120, 60, 240, 150), validate)
    assert events == []
    assert jumps == []


def test_focus_uses_actual_foreground_and_never_forces_denied_activation():
    driver = WindowsPowerPointZoom.__new__(WindowsPowerPointZoom)
    foreground = [42]
    calls = []

    def activate(hwnd):
        calls.append(hwnd)
        raise RuntimeError("Activation denied")

    driver.gui = SimpleNamespace(
        IsWindow=lambda hwnd: True,
        IsIconic=lambda hwnd: False,
        GetForegroundWindow=lambda: foreground[0],
        SetForegroundWindow=activate,
    )
    driver.focus(info())
    assert calls == []
    foreground[0] = 99
    with pytest.raises(PowerPointError, match="lost focus"):
        driver.focus(info())
    assert calls == [42]
