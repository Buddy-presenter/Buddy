"""Run the source desktop against a local API with temporary Windows callback registration.

Start the configured local API/database separately. No fake accounts, passwords,
verification codes or privileged AWS credentials are created by this launcher.
"""

import os
import sys
from pathlib import Path

from buddy_desktop.app import main
from buddy_desktop.source_callback import windows_source_callback


def run() -> int:
    if sys.platform != "win32":
        raise SystemExit("Use the installed macOS app for native browser return.")
    # Explicit local development overrides the preview's remote API default.
    os.environ.setdefault("BUDDY_API_URL", "http://127.0.0.1:8000")
    python = Path(sys.executable).with_name("pythonw.exe")
    if not python.is_file():
        python = Path(sys.executable)
    with windows_source_callback([str(python), "-m", "buddy_desktop"]):
        return main()


if __name__ == "__main__":
    raise SystemExit(run())
