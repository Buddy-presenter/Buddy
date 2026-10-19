"""Companion motion, real interaction, safe layout and timer lifecycle."""

from importlib.resources import files
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QImage, QMouseEvent
from PySide6.QtWidgets import QApplication

from buddy_desktop.companion import CompanionHabitat, spring
from buddy_desktop.visuals import apply_theme


def test_animation_body_is_transparent_and_has_no_baked_in_eyes(qapp):
    body = QImage()
    assert body.loadFromData(
        files("buddy_desktop").joinpath("assets", "buddy-body.png").read_bytes()
    )
    assert body.hasAlphaChannel() and body.width() >= 512
    assert body.pixelColor(0, 0).alpha() == 0
    for fraction in (0.367, 0.612):
        eye_area = body.pixelColor(round(body.width() * fraction), round(body.height() * 0.519))
        assert eye_area.red() > 150 and eye_area.alpha() > 240


def test_spring_is_continuous_monotone_and_frame_rate_independent():
    def settle(dt):
        value = velocity = 0.0
        values = []
        for _ in range(round(1 / dt)):
            value, velocity = spring(value, velocity, 100, dt)
            values.append(value)
        assert values == sorted(values) and max(values) < 100
        return value, velocity

    assert settle(1 / 60) == pytest.approx(settle(1 / 30), abs=1e-8)
    value, velocity = spring(42, 120, 10, 0)
    assert value == 42 and velocity == 120


def test_wander_travels_gently_and_stays_in_its_lane(qtbot):
    habitat = CompanionHabitat()
    qtbot.addWidget(habitat)
    habitat.resize(700, 116)
    habitat._rng.seed(4)
    initial = habitat.avatar.x()
    positions = []
    velocities = []
    for _ in range(360):
        habitat.advance(1 / 60)
        positions.append(habitat.avatar.x())
        velocities.append(abs(habitat._velocity))
        assert habitat.rect().contains(habitat.avatar.geometry())
    assert max(positions) - initial > 250
    assert max(velocities) <= 140
    habitat.resize(260, 116)
    habitat.show()
    assert habitat.rect().contains(habitat.avatar.geometry())


def test_pointer_tracking_blink_and_clicked_expression(qtbot):
    habitat = CompanionHabitat()
    qtbot.addWidget(habitat)
    habitat.resize(700, 116)
    habitat.set_wandering(False)
    for _ in range(60):
        habitat.advance(1 / 60, QPointF(650, -100))
    assert habitat.avatar.gaze.x() > 2 and habitat.avatar.gaze.y() < -1
    habitat._next_blink = habitat._age
    for _ in range(5):
        habitat.advance(1 / 60)
    assert habitat.avatar.openness < 0.4
    habitat.avatar.click()
    assert habitat.avatar.mood == "greeting"
    assert "hello" in habitat.caption.text()
    for _ in range(100):
        habitat.advance(1 / 60)
    assert habitat.avatar.mood == "idle"


def test_hello_never_hides_active_operation_or_claims_success(qtbot):
    habitat = CompanionHabitat()
    qtbot.addWidget(habitat)
    habitat.set_state("working")
    habitat.avatar.click()
    assert habitat.avatar.mood == "working" and "saved" not in habitat.caption.text()
    habitat.set_state("success")
    assert habitat.avatar.mood == "success"
    for _ in range(140):
        habitat.advance(1 / 60)
    assert habitat.avatar.mood == "idle"


@pytest.mark.parametrize("mode", ["rest", "reduced", "presenting", "shutdown"])
def test_paused_modes_stop_autonomous_motion(qtbot, mode):
    habitat = CompanionHabitat()
    qtbot.addWidget(habitat)
    habitat.resize(700, 116)
    for _ in range(120):
        habitat.advance(1 / 60)
    if mode == "rest":
        habitat.set_resting(True)
    elif mode == "reduced":
        habitat.set_reduced_motion(True)
    elif mode == "presenting":
        habitat.set_state("presenting")
    else:
        habitat.shutdown()
    position, gaze = habitat.avatar.pos(), QPointF(habitat.avatar.gaze)
    for _ in range(120):
        habitat.advance(1 / 60, QPointF(650, 70))
    assert habitat.avatar.pos() == position and habitat.avatar.gaze == gaze
    assert not habitat.timer.isActive()


def test_drag_keeps_grab_position_and_projects_a_bounded_release(qtbot):
    habitat = CompanionHabitat()
    qtbot.addWidget(habitat)
    habitat.resize(700, 116)
    habitat.show()
    habitat._drag(QPointF(habitat.mapToGlobal(habitat.rect().topLeft())) + QPointF(400, 27))
    assert habitat.avatar.x() == 400
    habitat._release()
    assert habitat.bounds()[0] <= habitat._target <= habitat.bounds()[1]
    for _ in range(180):
        habitat.advance(1 / 60)
    assert habitat.rect().contains(habitat.avatar.geometry())


def test_mouse_drag_preserves_the_original_grab_offset_and_does_not_greet(qtbot):
    habitat = CompanionHabitat()
    qtbot.addWidget(habitat)
    habitat.resize(700, 116)
    habitat.show()
    avatar = habitat.avatar
    grab = QPointF(20, 30)
    press_global = QPointF(avatar.mapToGlobal(grab.toPoint()))
    QApplication.sendEvent(
        avatar,
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            grab,
            press_global,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        ),
    )
    target_global = QPointF(habitat.mapToGlobal(QPoint(420, 57)))
    QApplication.sendEvent(
        avatar,
        QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(avatar.mapFromGlobal(target_global.toPoint())),
            target_global,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        ),
    )
    assert avatar.pos() == QPoint(400, 27)
    QApplication.sendEvent(
        avatar,
        QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            grab,
            target_global,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        ),
    )
    assert not habitat._dragging and habitat._greet_until == 0
    qtbot.keyClick(avatar, Qt.Key.Key_Space)
    assert avatar.mood == "greeting"


def test_preferences_persist_and_companion_does_not_cover_content(desktop_window, qtbot):
    window = desktop_window()
    window.resize(800, 600)
    qtbot.wait(30)
    assert window.companion.geometry().top() >= window.pages.geometry().bottom()
    window.preferences.wander.setChecked(False)
    window.companion.rest_button.click()
    window.settings.sync()
    assert not window.settings.value("appearance/companion_wander", type=bool)
    assert window.settings.value("appearance/companion_resting", type=bool)
    window.companion.avatar.click()
    assert not window.companion.resting
    window.preferences.show_companion.setChecked(False)
    assert not window.companion.isVisible() and not window.companion.timer.isActive()


def test_companion_timer_only_runs_in_visible_active_window(desktop_window, qtbot):
    window = desktop_window()
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow)
    qtbot.waitUntil(window.companion.timer.isActive)
    window.companion.hide()
    assert not window.companion.timer.isActive()
    window.companion.show()
    qtbot.waitUntil(window.companion.timer.isActive)
    window.preferences.reduce_motion.setChecked(True)
    assert not window.companion.timer.isActive()
    window.preferences.reduce_motion.setChecked(False)
    other = desktop_window()
    other.activateWindow()
    qtbot.waitUntil(other.isActiveWindow)
    qtbot.waitUntil(lambda: not window.companion.timer.isActive())


def test_character_rendered_expressions_and_window_layout(desktop_window, qapp):
    palette, stylesheet = qapp.palette(), qapp.styleSheet()
    apply_theme(qapp)
    try:
        window = desktop_window()
        window.resize(1120, 740)
        qapp.processEvents()
        destination = Path("build/issue112/companion")
        destination.mkdir(parents=True, exist_ok=True)
        for state in ("idle", "attention", "working", "success", "error", "presenting"):
            window.companion.set_state(state)
            window.companion.advance(0.016, QPointF(500, 0))
            qapp.processEvents()
            assert window.companion.avatar.grab().save(str(destination / f"{state}.png"))
        window.companion.set_state("idle")
        for _ in range(180):
            window.companion.advance(1 / 60)
        assert window.grab().save(str(destination / "workspace.png"))
    finally:
        qapp.setStyleSheet(stylesheet)
        qapp.setPalette(palette)
