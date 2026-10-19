"""Opt-in Windows/Office smoke using generated decks and the real Buddy task lifecycle.

No existing presentation is changed. Temporary fixtures are removed on exit.
--hold pauses at drawn/erased stages for visual inspection; press Enter to continue.
"""

import argparse
import hashlib
import importlib
import json
import sys
import tempfile
import time
from pathlib import Path
from threading import Event, Thread

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from buddy_desktop.config import AppConfig
from buddy_desktop.powerpoint import PowerPointAutomation, canonical_path
from buddy_desktop.visuals import apply_theme
from buddy_desktop.window import MainWindow


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hold", action="store_true")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("This opt-in smoke requires Windows and installed desktop PowerPoint.")
    pythoncom = importlib.import_module("pythoncom")
    client = importlib.import_module("win32com.client")
    was_running = PowerPointAutomation._powerpoint_process_running()
    pythoncom.CoInitialize()
    office = client.Dispatch("PowerPoint.Application")
    office.Visible = True
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    apply_theme(app)
    generated: set[str] = set()
    reports = []
    window = None
    temporary = tempfile.TemporaryDirectory(prefix="buddy-powerpoint-smoke-")
    folder = temporary.name

    def wait(predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while not predicate():
            app.processEvents()
            pythoncom.PumpWaitingMessages()
            if time.monotonic() > deadline:
                if window is not None:
                    feedback = window.present.powerpoint_controls.feedback.text()
                    print(
                        json.dumps(
                            {
                                "status": window.present.powerpoint_status.text(),
                                "action_feedback": feedback,
                            }
                        ),
                        flush=True,
                    )
                raise AssertionError("PowerPoint/Buddy smoke condition timed out")
            time.sleep(0.01)
        app.processEvents()

    def hold(stage):
        print(json.dumps({"visual_stage": stage}), flush=True)
        if args.hold:
            released = Event()
            Thread(target=lambda: (sys.stdin.readline(), released.set()), daemon=True).start()
            wait(released.is_set, timeout=180)

    try:
        root = Path(folder)
        window = MainWindow(
            AppConfig("http://127.0.0.1:8000", 0.5),
            QSettings(str(root / "smoke.ini"), QSettings.Format.IniFormat),
        )
        window.setWindowTitle("Buddy — PowerPoint smoke test")
        window.resize(1280, 900)
        window.show()
        window.navigation.setCurrentRow(2)
        page = window.present

        def idle():
            return not page.powerpoint_runner.busy and page._queued_powerpoint_preview is None

        def click(button):
            wait(idle)
            if not button.isEnabled():
                raise AssertionError("Expected PowerPoint control was disabled")
            button.click()
            wait(idle)

        def apply_tool(tool, bounds=None, *, text="", path=None):
            controls.mode.setCurrentIndex(controls.mode.findData(tool))
            if path is not None:
                controls.preview.path = tuple(path)
                controls.preview.selection = (path[0], path[-1])
                controls.preview.update()
            elif bounds is not None:
                x, y, width_value, height_value = bounds
                for key, value in {
                    "x1": x,
                    "y1": y,
                    "x2": x + width_value,
                    "y2": y + height_value,
                }.items():
                    controls.fields[key].setValue(value)
            controls.text_input.setText(text)
            controls.refresh()
            if not controls.apply.isEnabled():
                raise AssertionError(f"Tool {tool} disabled: {controls.feedback.text()}")
            click(controls.apply)

        for name, width, height, show_type in (
            ("BuddySmokeWide", 960, 540, 1),
            ("BuddySmokeStandard", 720, 540, 2),
        ):
            path = root / f"{name}.pptx"
            generated.add(canonical_path(path))
            presentation = office.Presentations.Add(-1)
            presentation.PageSetup.SlideWidth = width
            presentation.PageSetup.SlideHeight = height
            for index in range(1, 4):
                slide = presentation.Slides.Add(index, 12)
                slide.FollowMasterBackground = 0
                slide.Background.Fill.ForeColor.RGB = 0xFFFFFF
                title = slide.Shapes.AddTextbox(1, 30, 20, width - 60, 50)
                title.TextFrame.TextRange.Text = f"Buddy smoke: {name} — slide {index}"
                title.TextFrame.TextRange.Font.Size = 24
                box = slide.Shapes.AddShape(
                    1, width * 0.2, height * 0.25, width * 0.6, height * 0.5
                )
                box.Fill.ForeColor.RGB = 0xEEEEEE
                box.Line.ForeColor.RGB = 0x808080
                box.TextFrame.TextRange.Text = "Native slideshow ink test area"
                box.TextFrame.TextRange.Font.Color.RGB = 0x202020
            presentation.SlideShowSettings.ShowType = show_type
            presentation.SaveAs(str(path), 24)
            presentation.Close()
            original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            page._submit_powerpoint("launch", str(path))
            wait(lambda: page._powerpoint_info is not None)
            wait(idle)
            controls = page.powerpoint_controls
            assert page._powerpoint_info.slide_index == 1
            assert not controls.previous.isEnabled()
            assert not controls.preview.image.isNull(), controls.feedback.text()
            # A second show with a similar filename must never receive these actions.
            other_path = root / f"{name}Other.pptx"
            generated.add(canonical_path(other_path))
            other = office.Presentations.Add(-1)
            other.Slides.Add(1, 12)
            other.SlideShowSettings.ShowType = 2
            other.SaveAs(str(other_path), 24)
            other_window = other.SlideShowSettings.Run()
            click(controls.next)
            assert page._powerpoint_info.slide_index == 2
            assert other_window.View.Slide.SlideIndex == 1
            controls.target.setValue(3)
            click(controls.go)
            assert page._powerpoint_info.slide_index == 3
            assert not controls.next.isEnabled()
            click(controls.previous)
            assert page._powerpoint_info.slide_index == 2
            active = next(
                office.SlideShowWindows.Item(i)
                for i in range(1, office.SlideShowWindows.Count + 1)
                if canonical_path(office.SlideShowWindows.Item(i).Presentation.FullName)
                == canonical_path(path)
            )
            if show_type == 2:
                active.Left, active.Top, active.Width, active.Height = 90, 70, 800, 600
            active.View.GotoSlide(1, 0)
            wait(lambda: page._powerpoint_info.slide_index == 1)
            wait(idle)
            shapes = active.Presentation.Slides.Item(1).Shapes
            prefix = PowerPointAutomation._MANAGED_ANNOTATION_PREFIX
            apply_tool("draw_line", (width * 0.08, height * 0.08, width * 0.23, 12))
            assert (
                sum(str(shapes.Item(i).Name).startswith(prefix) for i in range(1, shapes.Count + 1))
                == 1
            )
            click(controls.erase)
            assert not any(
                str(shapes.Item(i).Name).startswith(prefix) for i in range(1, shapes.Count + 1)
            )
            # This specifically guards the reported regression: the new annotation
            # must be the only visible Buddy object after EraseDrawing and reapply.
            apply_tool("draw_arrow", (width * 0.1, height * 0.1, width * 0.2, height * 0.12))
            assert (
                sum(str(shapes.Item(i).Name).startswith(prefix) for i in range(1, shapes.Count + 1))
                == 1
            )
            apply_tool("draw_rectangle", (width * 0.38, height * 0.35, width * 0.12, height * 0.1))
            before_area_erase = sum(
                str(shapes.Item(i).Name).startswith(prefix) for i in range(1, shapes.Count + 1)
            )
            apply_tool("erase_region", (width * 0.37, height * 0.34, width * 0.14, height * 0.12))
            after_area_erase = sum(
                str(shapes.Item(i).Name).startswith(prefix) for i in range(1, shapes.Count + 1)
            )
            assert after_area_erase == before_area_erase - 1

            apply_tool("draw_line", (width * 0.08, height * 0.08, width * 0.23, 12))
            apply_tool("draw_rectangle", (width * 0.08, height * 0.25, width * 0.2, height * 0.13))
            apply_tool("draw_oval", (width * 0.32, height * 0.25, width * 0.2, height * 0.13))
            apply_tool("draw_underline", (width * 0.56, height * 0.25, width * 0.3, height * 0.08))
            apply_tool(
                "draw_freehand",
                path=(
                    (width * 0.08, height * 0.48),
                    (width * 0.14, height * 0.58),
                    (width * 0.2, height * 0.5),
                    (width * 0.27, height * 0.58),
                ),
            )
            apply_tool(
                "highlight_region", (width * 0.32, height * 0.45, width * 0.18, height * 0.16)
            )
            apply_tool(
                "filled_rectangle", (width * 0.54, height * 0.45, width * 0.17, height * 0.16)
            )
            apply_tool("filled_oval", (width * 0.76, height * 0.45, width * 0.17, height * 0.16))
            apply_tool(
                "text_box",
                (width * 0.08, height * 0.72, width * 0.25, height * 0.15),
                text="Buddy text",
            )
            apply_tool(
                "callout",
                (width * 0.48, height * 0.72, width * 0.35, height * 0.15),
                text="Buddy callout",
            )
            managed = [
                shapes.Item(i)
                for i in range(1, shapes.Count + 1)
                if str(shapes.Item(i).Name).startswith(prefix)
            ]
            assert len(managed) >= 12, len(managed)
            controls.mode.setCurrentIndex(controls.mode.findData("pointer_pen"))
            click(controls.apply)
            assert active.View.PointerType == 2
            window.pages.widget(2).ensureWidgetVisible(controls)
            app.processEvents()
            hold(name + " all tools drawn")
            click(controls.erase)
            assert page._powerpoint_info.slide_index == 1
            assert not any(
                str(shapes.Item(i).Name).startswith(prefix) for i in range(1, shapes.Count + 1)
            )
            hold(name + " erased")
            click(controls.reset)
            assert page._powerpoint_info.slide_index == 1
            tracked_path = page._powerpoint_info.path
            page._disconnect_powerpoint()
            assert page._powerpoint_info is None
            assert not page._powerpoint_poll.isActive()
            assert active.View.Slide.SlideIndex == 1
            page._submit_powerpoint("connect", tracked_path)
            wait(lambda: page._powerpoint_info is not None)
            wait(idle)
            active.View.Exit()
            wait(lambda: page._powerpoint_info is None)
            assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash
            assert other_window.View.Slide.SlideIndex == 1
            other_window.View.Exit()
            other.Saved = True
            other.Close()
            reports.append(
                {
                    "deck": name,
                    "slide_points": [width, height],
                    "show_type": show_type,
                    "annotation_navigation_and_reconnect_smoke": "passed",
                    "source_unchanged": True,
                }
            )
            presentation = office.Presentations.Item(name + ".pptx")
            presentation.Saved = True  # Discard only this generated fixture's session state.
            presentation.Close()
        print(
            json.dumps(
                {
                    "office_version": office.Version,
                    "qt_device_ratios": [s.devicePixelRatio() for s in app.screens()],
                    "results": reports,
                },
                indent=2,
            ),
            flush=True,
        )
        window.close()
        wait(lambda: not window.isVisible())
    finally:
        if window is not None:
            window.present.shutdown()
            window.present.powerpoint_runner.wait_for_shutdown()
            window.close()
        for index in range(office.Presentations.Count, 0, -1):
            presentation = office.Presentations.Item(index)
            if canonical_path(presentation.FullName) in generated:
                presentation.Saved = True
                presentation.Close()
        if not was_running and office.Presentations.Count == 0:
            office.Quit()
        pythoncom.CoUninitialize()
        temporary.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
