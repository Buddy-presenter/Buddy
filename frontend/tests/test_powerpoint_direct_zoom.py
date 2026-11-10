import time
from types import SimpleNamespace

from buddy_desktop.powerpoint_direct_zoom import MagnifierSession, ZoomPlan


class FakeMagnifier:
    def __init__(self):
        self.current = ZoomPlan(1, 0, 0)
        self.calls = []

    def MagInitialize(self):
        return 1

    def MagUninitialize(self):
        return 1

    def MagGetFullscreenTransform(self, scale, x, y):
        scale._obj.value, x._obj.value, y._obj.value = (
            self.current.scale,
            self.current.x,
            self.current.y,
        )
        return 1

    def MagSetFullscreenTransform(self, scale, x, y):
        self.current = ZoomPlan(scale.value, x, y)
        self.calls.append(self.current)
        return 1


def test_one_direct_transform_and_reset():
    lib = FakeMagnifier()
    gui = SimpleNamespace(IsWindow=lambda _: True, GetForegroundWindow=lambda: 42)
    session = MagnifierSession(gui, lib)
    session.apply(42, ZoomPlan(2, 100, 200))
    assert lib.calls == [ZoomPlan(2, 100, 200)]
    session.release()
    assert lib.calls == [ZoomPlan(2, 100, 200), ZoomPlan(1, 0, 0)]


def test_focus_loss_restores_and_foreign_transform_is_not_overwritten():
    lib = FakeMagnifier()
    focus = {"hwnd": 42}
    gui = SimpleNamespace(IsWindow=lambda _: True, GetForegroundWindow=lambda: focus["hwnd"])
    session = MagnifierSession(gui, lib)
    session.apply(42, ZoomPlan(2, 100, 200))
    focus["hwnd"] = 43
    deadline = time.monotonic() + 2
    while session.owned and time.monotonic() < deadline:
        time.sleep(0.02)
    assert lib.current == ZoomPlan(1, 0, 0)
    focus["hwnd"] = 42
    session.apply(42, ZoomPlan(2, 100, 200))
    lib.current = ZoomPlan(3, 20, 30)
    session.release()
    assert lib.current == ZoomPlan(3, 20, 30)
