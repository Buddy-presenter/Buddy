"""Verify integrity and launch the installed/extracted frozen application."""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def verify(folder: Path) -> list[Path]:
    manifest = json.loads((folder / "manifest.json").read_text())
    if manifest.get("schema") != 1 or manifest.get("platform") != sys.platform:
        raise ValueError("Wrong release schema or platform.")
    artifacts = []
    for item in manifest["artifacts"]:
        name = item["name"]
        if Path(name).name != name or not name.startswith("Buddy-"):
            raise ValueError("Invalid artifact path.")
        artifact = folder / name
        with artifact.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if artifact.stat().st_size != item["size"] or digest != item["sha256"]:
            raise ValueError("Release integrity verification failed.")
        artifacts.append(artifact)
    if not artifacts:
        raise ValueError("Empty release.")
    return artifacts


def smoke(folder: Path) -> None:
    for artifact in verify(folder):
        with TemporaryDirectory(prefix="buddy-installed-") as directory:
            destination = Path(directory)
            if sys.platform == "win32":
                subprocess.run(
                    [
                        str(artifact.resolve()),
                        "/VERYSILENT",
                        "/SUPPRESSMSGBOXES",
                        "/NORESTART",
                        "/NOICONS",
                        "/SP-",
                        f"/DIR={destination}",
                    ],
                    check=True,
                    timeout=90,
                )
                executable = destination / "Buddy.exe"
            elif sys.platform == "darwin":
                subprocess.run(
                    ["ditto", "-x", "-k", str(artifact.resolve()), str(destination)], check=True
                )
                executable = destination / "Buddy.app/Contents/MacOS/Buddy"
            else:
                raise ValueError("Native package testing requires Windows/macOS.")
            try:
                subprocess.run([str(executable), "--smoke-test"], check=True, timeout=30)
            finally:
                if sys.platform == "win32" and (destination / "unins000.exe").exists():
                    subprocess.run(
                        [
                            str(destination / "unins000.exe"),
                            "/VERYSILENT",
                            "/SUPPRESSMSGBOXES",
                            "/NORESTART",
                        ],
                        check=True,
                        timeout=90,
                    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    smoke(parser.parse_args().folder)
