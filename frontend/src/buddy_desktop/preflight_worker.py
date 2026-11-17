"""Fixed-file preflight child; real production renderers and no network/account client."""

import hashlib
import json
import sys
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings, Qt, qVersion
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtWidgets import QApplication

from buddy_desktop.preflight import check_tools
from buddy_desktop.slide_contract import ContractError
from buddy_desktop.slide_executor import SlideExecutor
from buddy_desktop.slide_surface import SlideSurface
from buddy_desktop.validation_worker import PROFILES


class PDFPlayer:
    def __init__(self, app: QApplication, folder: Path, request: dict[str, Any]) -> None:
        self.app = app
        self.surface = SlideSurface()
        self.surface.setWindowTitle("Buddy — isolated preflight")
        width, height = request["viewport"]
        self.dpr = PROFILES[request["profile"]][2]
        self.surface.resize(width, height)
        self.surface.show()
        self.surface.raise_()
        self.surface.activateWindow()
        self.executor = SlideExecutor(self.surface)
        self.completions: list[Any] = []
        self.executor.completed.connect(self.completions.append)
        self.executor.load_pdf(
            (folder / "source.pdf").read_bytes(),
            deck_id=request["deck_id"],
            revision=request["revision"],
        )
        self.finish()
        manifest = self.executor.manifest
        assert manifest is not None
        if set(request["slides"]) != {s.slide_id for s in manifest.slides}:
            raise ContractError("Snapshot slide IDs differ from source.")
        self.runtime = dict(
            renderer_version="pdfium-5.14.0-144dpi",
            qt_version=qVersion(),
            viewport=[width, height],
            display_scale=self.dpr,
        )
        self.load(manifest.slides[0].slide_id)

    def finish(self) -> None:
        deadline = time.monotonic() + 25
        while self.executor.busy:
            if time.monotonic() > deadline:
                raise TimeoutError
            self.app.processEvents()
            time.sleep(0.005)
        if not self.completions or self.completions[-1].status != "completed":
            raise ContractError("Preflight executor did not complete.")
        self.settle()

    def settle(self) -> None:
        self.surface.repaint()
        for _ in range(5):
            self.app.processEvents()
            time.sleep(0.02)

    def action(self, kind: str, params: dict[str, Any]) -> None:
        assert self.executor.manifest is not None and self.surface.slide is not None
        self.apply(
            dict(
                version="1.1",
                instance_id="preflight-control",
                purpose="Preflight control",
                deck_id=self.executor.manifest.deck_id,
                render_id=self.executor.manifest.render_id,
                slide_id=self.surface.slide.slide_id,
                type=kind,
                params=params,
            )
        )

    def load(self, slide_id: str) -> None:
        self.action("go_to_slide", {"target_slide_id": slide_id})
        self.reset()

    def reset(self) -> None:
        self.action("reset_view", {})

    def apply(self, action: dict[str, Any]) -> None:
        self.executor.dispatch(action)
        self.finish()

    def state(self) -> dict[str, Any]:
        mapping = self.surface.mapping()
        assert mapping is not None and self.surface.slide and self.surface.region
        return dict(
            slide_id=self.surface.slide.slide_id,
            region=asdict(self.surface.region),
            highlight=asdict(self.surface.highlight) if self.surface.highlight else None,
            scale=mapping.scale,
            offset_x=mapping.offset_x,
            offset_y=mapping.offset_y,
        )

    def capture(self) -> QImage:
        self.settle()
        screen = self.surface.screen()
        self.runtime["actual_display_scale"] = self.surface.devicePixelRatioF()
        if screen is None or abs(self.surface.devicePixelRatioF() - self.dpr) > 0.001:
            raise ContractError("unsupported_display_profile")
        image = screen.grabWindow(int(self.surface.winId())).toImage()
        reference = self.surface.grab().toImage()
        if image.isNull() or image.size() != reference.size():
            raise ContractError("desktop_capture_unavailable")

        # Desktop capture must actually show our audience surface, not an occluding app.
        def pixels(value: QImage) -> bytes:
            return bytes(
                value.scaled(128, 72).convertToFormat(QImage.Format.Format_RGB32).constBits()
            )

        a, b = pixels(image), pixels(reference)
        if sum(abs(x - y) for x, y in zip(a, b, strict=True)) / len(a) > 4:
            raise ContractError("audience_viewport_occluded")
        return image

    def close(self) -> None:
        self.executor.close()
        self.surface.close()


def run(folder: Path) -> dict[str, Any]:
    # The slide-action reader deliberately caps every object at 16 fields. A
    # processed snapshot instead contains up to 500 slides/1,000 catalog entries.
    # This fixed private file was serialized/validated by the trusted host.
    with (folder / "request.json").open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ContractError("Preflight snapshot exceeds bounds.")
    request = json.loads(raw)
    if not isinstance(request, dict):
        raise ContractError("Invalid preflight snapshot.")
    check_tools(request["tools"], request["catalog"], request["slides"])
    result: dict[str, Any] = dict(
        source_sha256=request["source_sha256"],
        state="needs-review",
        reason="runtime_unavailable",
        runner_version="desktop-preflight-1",
        runtime_platform=sys.platform,
        semantic_verified=False,
        trace=[],
    )
    if sys.platform not in {"win32", "darwin"}:
        result["reason"] = "unsupported_desktop_runtime"
        return result
    source = folder / ("source.pdf" if request["source_format"] == "pdf" else "source.pptx")
    if hashlib.sha256(source.read_bytes()).hexdigest() != request["source_sha256"]:
        result["reason"] = "source_mismatch"
        return result
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.Round)
    app = QApplication(["buddy-isolated-preflight"])
    app.setApplicationName("BuddyPreflight")
    app.setOrganizationName("BuddyTestOnly")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(folder / "qt-settings")
    )
    result["qt_settings_private"] = Path(QSettings().fileName()).resolve().is_relative_to(folder)
    if not result["qt_settings_private"]:
        raise ContractError("Preflight Qt settings are not isolated.")
    player: Any = None
    try:
        if request["source_format"] == "pdf":
            player = PDFPlayer(app, folder, request)
        else:
            from buddy_desktop.preflight_powerpoint import OfficePlayer

            player = OfficePlayer(app, folder, request)
        result.update(player.runtime)
        capture_index = 0
        for tool in request["tools"]:
            before = player.state()
            op = tool["op"]
            baseline = None
            artifact = None
            if op == "load_slide":
                player.load(tool["slide_id"])
            elif op == "reset":
                player.reset()
            elif op == "apply_action":
                action = request["catalog"][tool["action_id"]]
                # Every action runs from its source slide's clean, full-slide baseline.
                player.load(action["slide_id"])
                baseline = player.state()
                player.apply(action)
            elif op == "capture":
                artifact = f"{capture_index}.png"
                image = player.capture()
                if not image.save(str(folder / artifact)):
                    raise ContractError("desktop_capture_unavailable")
                capture_index += 1
            result["trace"].append(
                dict(
                    tool=tool,
                    before=before,
                    baseline=baseline,
                    after=player.state(),
                    artifact=artifact,
                )
            )
        result.update(state="completed", reason=None)
    except Exception as exc:
        # Persist bounded diagnostics without leaking COM paths, source text or credentials.
        known = {
            "powerpoint_startup_failed",
            "powerpoint_launch_failed",
            "powerpoint_baseline_failed",
            "unsupported_display_profile",
            "desktop_capture_unavailable",
            "audience_viewport_occluded",
            "powerpoint_requires_windows_office",
            "powerpoint_worker_busy_use_dedicated_session",
            "unsafe_office_content",
        }
        result["reason"] = str(exc) if str(exc) in known else "execution_failed"
        result["failed_tool_index"] = len(result["trace"])
        errors = []
        cause: BaseException | None = exc
        for _ in range(4):
            if cause is None:
                break
            errors.append(
                dict(
                    kind=type(cause).__name__,
                    code=getattr(cause, "hresult", getattr(cause, "winerror", None)),
                    attribute=getattr(cause, "name", None),
                    operation=getattr(cause, "funcname", None),
                    frames=[
                        dict(
                            module=Path(frame.filename).stem, function=frame.name, line=frame.lineno
                        )
                        for frame in traceback.extract_tb(cause.__traceback__)[-3:]
                    ],
                )
            )
            cause = cause.__cause__
        result["failure_codes"] = errors
    finally:
        if player is not None:
            result.update(player.runtime)
            player.close()
        result["source_unchanged"] = (
            hashlib.sha256(source.read_bytes()).hexdigest() == request["source_sha256"]
        )
        if not result["source_unchanged"]:
            result.update(state="needs-review", reason="source_modified")
        app.quit()
    return result


def main() -> None:
    if sys.stdin.buffer.read(1) != b"1":
        return
    folder = Path(sys.argv[1]).resolve()
    result = run(folder)
    (folder / "result.json").write_text(json.dumps(result, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
