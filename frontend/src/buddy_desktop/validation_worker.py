"""Headless validation entry point: fixed job files, real executor, surface-only evidence."""

import hashlib
import json
import math
import os
import sys
import time
from dataclasses import asdict
from importlib.resources import files
from pathlib import Path
from typing import Any

PROFILES = {
    "wide-1x": (1280, 720, 1),
    "standard-1x": (960, 720, 1),
    "portrait-1x": (720, 960, 1),
    "wide-2x": (1280, 720, 2),
}
MAX_LINE_HIGHLIGHT_HEIGHT_RATIO = 0.06


def validate(folder: Path) -> dict[str, Any]:
    from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
    from PySide6.QtCore import QRectF, qVersion
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QApplication

    from buddy_desktop.slide_contract import ContractError, Region, read_json
    from buddy_desktop.slide_executor import SlideExecutor, load_image
    from buddy_desktop.slide_surface import SlideSurface

    request = read_json(folder / "request.json", 16384)
    definition = json.loads(
        files("buddy_desktop")
        .joinpath("validation_contracts", "request-v1.schema.json")
        .read_text()
    )
    Draft202012Validator(definition).validate(request)
    assert isinstance(request, dict)
    result: dict[str, Any] = {
        "schema_version": "buddy.action-validation-result.v1",
        "state": "needs-review",
        "reasons": ["execution_failed"],
        "semantic_verified": False,
        "validator_version": "qt-executor-1",
        "renderer_version": "pdfium-5.14.0-144dpi",
        "profile": request["profile"],
        "runtime_platform": sys.platform,
        "qt_version": qVersion(),
        "source_revision": request["source_revision"],
        "source_sha256": request["source_sha256"],
        "action_sha256": hashlib.sha256(
            json.dumps(
                request["action"], sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest(),
        "before": None,
        "after": None,
    }
    app = QApplication(["buddy-action-validator"])
    surface = SlideSurface()
    width, height, dpr = PROFILES[request["profile"]]
    surface.resize(width, height)
    surface.show()
    executor = SlideExecutor(surface)
    completions: list[Any] = []
    executor.completed.connect(completions.append)
    deadline = time.monotonic() + 45

    def finish() -> Any:
        while executor.busy:
            if time.monotonic() > deadline:
                raise TimeoutError
            app.processEvents()
            time.sleep(0.001)
        app.processEvents()
        if not completions:
            raise RuntimeError
        return completions[-1]

    def snapshot() -> dict[str, Any]:
        mapping = surface.mapping()
        assert mapping is not None and surface.slide is not None and surface.region is not None
        return {
            "slide_id": surface.slide.slide_id,
            "region": asdict(surface.region),
            "highlight": asdict(surface.highlight) if surface.highlight else None,
            "scale": mapping.scale,
            "offset_x": mapping.offset_x,
            "offset_y": mapping.offset_y,
            "width": surface.width(),
            "height": surface.height(),
            "dpr": surface.devicePixelRatioF(),
        }

    def capture(name: str) -> QImage:
        app.processEvents()
        image = surface.grab().toImage().convertToFormat(QImage.Format.Format_ARGB32)
        if not image.save(str(folder / name)):
            raise RuntimeError
        return image

    try:
        if qVersion() != "6.12.0":
            result.update(reasons=["unsupported_qt_version"])
            return result
        content = (folder / "source.pdf").read_bytes()
        if hashlib.sha256(content).hexdigest() != request["source_sha256"]:
            result.update(state="rejected", reasons=["source_mismatch"])
            return result
        executor.load_pdf(
            content, deck_id=str(request["deck_id"]), revision=request["source_revision"]
        )
        if finish().status != "completed" or executor.manifest is None:
            return result
        manifest = executor.manifest
        action = request["action"]
        # Only catalog JSON is accepted; no file paths, executable names or URLs are inputs.
        schema_name = (
            "action-v1.1.schema.json" if action.get("version") == "1.1" else "action-v1.schema.json"
        )
        action_schema = json.loads(
            files("buddy_desktop").joinpath("slide_contracts", schema_name).read_text()
        )
        if not Draft202012Validator(action_schema).is_valid(action):
            result.update(state="rejected", reasons=["invalid_action"])
            return result
        if (action["deck_id"], action["render_id"]) != (manifest.deck_id, manifest.render_id):
            result.update(state="rejected", reasons=["stale_reference"])
            return result
        try:
            current = manifest.index(action["slide_id"])
        except ContractError:
            result.update(state="rejected", reasons=["unknown_slide"])
            return result
        if request.get("duplicate_candidate", False):
            result.update(state="rejected", reasons=["duplicate_action"])
            return result
        if (
            request.get("precision", "standard") == "line"
            and action["type"] == "highlight_region"
            and action["params"]["region"]["height"]
            > manifest.slides[current].height * MAX_LINE_HIGHLIGHT_HEIGHT_RATIO
        ):
            # Model-generated highlights are line-level cues. Reject broad candidates
            # deterministically while leaving the general-purpose catalog unchanged.
            result.update(state="rejected", reasons=["highlight_region_too_broad"])
            return result
        if current:
            executor.dispatch(
                {
                    **action,
                    "slide_id": manifest.slides[0].slide_id,
                    "type": "go_to_slide",
                    "params": {"target_slide_id": action["slide_id"]},
                }
            )
            if finish().status != "completed":
                return result
        executor.dispatch({**action, "type": "reset_view", "params": {}})
        finish()
        if action["type"] == "reset_view":
            # Prove reset actually clears a changed view, rather than only testing
            # a no-op on an already-reset page. Setup is deterministic and internal.
            slide = manifest.slides[current]
            seed = {
                "region": {
                    "x": slide.width / 4,
                    "y": slide.height / 4,
                    "width": slide.width / 2,
                    "height": slide.height / 2,
                }
            }
            for kind in ("zoom_region", "highlight_region"):
                executor.dispatch({**action, "version": "1.1", "type": kind, "params": seed})
                if finish().status != "completed":
                    return result
        result["before"] = snapshot()
        before = capture("before.png")
        if not math.isclose(surface.devicePixelRatioF(), dpr, abs_tol=0.001):
            result.update(reasons=["unsupported_display_profile"])
            return result
        # Derive the expected outcome from the published catalog independently of
        # executor target/transform helpers, then compare both state and painted pixels.
        target = current
        kind = action["type"]
        if kind == "next_slide":
            target = min(current + 1, len(manifest.slides) - 1)
        elif kind == "previous_slide":
            target = max(current - 1, 0)
        elif kind == "go_to_slide":
            try:
                target = manifest.index(action["params"]["target_slide_id"])
            except ContractError:
                target = -1
        expected_region = manifest.slides[target].full if target >= 0 else None
        expected_highlight = None
        if kind == "zoom_region":
            expected_region = Region(**action["params"]["region"])
        elif kind == "highlight_region":
            expected_highlight = Region(**action["params"]["region"])
        executor.dispatch(action)
        completion = finish()
        result["after"] = snapshot()
        actual = capture("after.png")
        if completion.status == "rejected":
            result.update(state="rejected", reasons=["invalid_action_parameters"])
            if result["before"] != result["after"] or before != actual:
                result.update(state="needs-review", reasons=["rejection_changed_surface"])
            return result
        if completion.status != "completed" or target < 0 or expected_region is None:
            return result
        expected_slide = manifest.slides[target]
        region = expected_region
        scale = min(width / region.width, height / region.height)
        ox = (width - region.width * scale) / 2 - region.x * scale
        oy = (height - region.height * scale) / 2 - region.y * scale
        correct = (
            surface.slide == expected_slide
            and surface.region == region
            and surface.highlight == expected_highlight
            and all(
                math.isclose(result["after"][key], value, rel_tol=1e-9, abs_tol=1e-7)
                for key, value in (("scale", scale), ("offset_x", ox), ("offset_y", oy))
            )
        )
        reference = QImage(width * dpr, height * dpr, QImage.Format.Format_ARGB32)
        reference.setDevicePixelRatio(dpr)
        reference.fill(QColor("#06090D"))
        painter = QPainter(reference)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.translate(ox, oy)
        painter.scale(scale, scale)
        painter.drawImage(
            QRectF(0, 0, expected_slide.width, expected_slide.height),
            load_image(manifest, expected_slide),
        )
        if expected_highlight:
            color = QColor("#FFEB3B")
            color.setAlpha(90)
            painter.fillRect(
                QRectF(
                    expected_highlight.x,
                    expected_highlight.y,
                    expected_highlight.width,
                    expected_highlight.height,
                ),
                color,
            )
        painter.end()
        if (
            correct
            and reference.size() == actual.size()
            and all(
                abs(expected - observed) <= 4
                for expected, observed in zip(
                    bytes(reference.constBits()), bytes(actual.constBits()), strict=True
                )
            )
        ):
            result.update(state="validated", reasons=[])
        else:
            result.update(state="rejected", reasons=["unexpected_execution_or_pixels"])
        return result
    finally:
        executor.close()
        executor.wait_for_shutdown()
        surface.close()
        app.processEvents()


def main() -> int:
    folder = Path(sys.argv[1]).resolve()
    if sys.stdin.buffer.read(1) != b"1":
        return 1
    # Qt and PDF files are the only inputs. No personal settings, vault or account UI.
    os.environ["HOME"] = str(folder)
    os.environ["USERPROFILE"] = str(folder)
    if sys.platform != "win32":
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (50, 50))
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
    try:
        result = validate(folder)
        (folder / "result.json").write_text(json.dumps(result, allow_nan=False), encoding="utf-8")
        return 0
    except Exception as exc:
        # Never emit parser/Qt exceptions containing private content or file paths.
        (folder / "result.json").write_text(
            json.dumps({"error": "validation_runtime_failed", "error_type": type(exc).__name__})
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
