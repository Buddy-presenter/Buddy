"""Replay versioned action instances on the production Qt surface and save viewport evidence."""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import QSize, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from buddy_desktop.slide_contract import read_json
from buddy_desktop.slide_executor import SlideExecutor
from buddy_desktop.slide_surface import SlideSurface


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--actions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=540)
    parser.add_argument("--dpr", type=float, default=1)
    args = parser.parse_args()
    if not (1 <= args.width <= 4096 and 1 <= args.height <= 4096 and 1 <= args.dpr <= 3):
        parser.error("Use viewport dimensions 1..4096 and DPR 1..3.")
    if args.width * args.height * args.dpr**2 > 16_777_216:
        parser.error("Capture exceeds the 16 megapixel limit.")
    actions = read_json(args.actions)
    if not isinstance(actions, list):
        parser.error("Actions input must be an array of versioned instances.")
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv)
    surface = SlideSurface()
    surface.resize(args.width, args.height)
    executor = SlideExecutor(surface)
    evidence = []
    remaining = iter(actions)
    end = object()
    current = None

    def settled(result):
        if result.status != "completed":
            print(f"{result.status}: {result.message}", file=sys.stderr)
            executor.close()
            app.exit(1)
            return
        QTimer.singleShot(0, capture)

    def capture():
        nonlocal current
        image = QImage(
            QSize(round(args.width * args.dpr), round(args.height * args.dpr)),
            QImage.Format.Format_RGB32,
        )
        image.setDevicePixelRatio(args.dpr)
        surface.render(image)
        filename = f"{len(evidence):02d}-viewport.png"
        if not image.save(str(args.output / filename)):
            executor.close()
            app.exit(1)
            return
        evidence.append(
            {
                "action": current,
                "slide_id": surface.slide.slide_id,
                "region": asdict(surface.region),
                "viewport": [surface.width(), surface.height()],
                "dpr": args.dpr,
                "capture": filename,
            }
        )
        current = next(remaining, end)
        if current is end:
            (args.output / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
            executor.close()
            app.exit(0)
        else:
            executor.dispatch(current)

    executor.completed.connect(settled)
    executor.load_manifest(args.manifest)
    result = app.exec()
    executor.wait_for_shutdown()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
