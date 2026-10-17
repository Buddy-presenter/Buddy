"""Build native packages, run the actual frozen GUI, and record artifact integrity."""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def manifest(folder: Path, revision: str, channel: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{40}", revision) or channel not in {"preview", "stable"}:
        raise ValueError("Invalid release identity.")
    artifacts = [path for path in folder.iterdir() if path.suffix in {".exe", ".zip"}]
    if not artifacts:
        raise ValueError("No packaged artifacts to publish.")
    return {
        "schema": 1,
        "revision": revision,
        "channel": channel,
        "version": tomllib.loads((ROOT / "frontend/pyproject.toml").read_text())["project"][
            "version"
        ],
        "platform": sys.platform,
        "architecture": platform.machine(),
        "backend_api": "v1",
        "automatic_updates": False,
        "artifacts": [
            {
                "name": path.name,
                "size": path.stat().st_size,
                "sha256": hashlib.file_digest(path.open("rb"), "sha256").hexdigest(),
            }
            for path in artifacts
        ],
    }


def build(
    output: Path, revision: str, signing_identity: str | None = None, *, production: bool = False
) -> Path:
    if not re.fullmatch(r"[a-f0-9]{40}", revision) or sys.platform not in {"win32", "darwin"}:
        raise ValueError("Native Windows/macOS runner and reviewed revision are required.")
    output.mkdir(parents=True, exist_ok=True)
    bundle = output / "bundle"
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--noupx",
        "--windowed",
        "--name",
        "Buddy",
        "--paths",
        str(ROOT),
        "--collect-data",
        "buddy_desktop",
        "--collect-all",
        "pypdfium2",
        "--collect-all",
        "pypdfium2_raw",
        "--copy-metadata",
        "pypdfium2",
        "--collect-submodules",
        "keyring.backends",
        "--distpath",
        str(bundle),
        "--workpath",
        str(output / "work"),
        "--specpath",
        str(output),
    ]
    if sys.platform == "darwin":
        if production and not signing_identity:
            raise ValueError("Production macOS build requires a Developer ID identity.")
        command += ["--osx-bundle-identifier", "org.buddy.desktop"]
        if signing_identity:
            command += ["--codesign-identity", signing_identity]
    command += [str(ROOT / "frontend/scripts/release/desktop_entry.py")]
    build_environment = dict(os.environ)
    if sys.platform == "win32":
        # Do not let unrelated tools (e.g. Poppler's ICU/OpenSSL) shadow Qt/OS DLLs.
        windows = Path(os.environ["SYSTEMROOT"])
        build_environment["PATH"] = os.pathsep.join(
            [
                str(windows / "System32"),
                str(windows),
                str(Path(sys.executable).parent),
                sys.base_prefix,
            ]
        )
    subprocess.run(command, cwd=ROOT, check=True, env=build_environment)
    executable = bundle / (
        "Buddy/Buddy.exe" if sys.platform == "win32" else "Buddy.app/Contents/MacOS/Buddy"
    )
    subprocess.run([str(executable), "--smoke-test"], check=True, timeout=30)
    version = tomllib.loads((ROOT / "frontend/pyproject.toml").read_text())["project"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Installer version must be a three-part numeric version.")
    if sys.platform == "win32":
        if production:
            subprocess.run(
                [
                    "pwsh",
                    "-File",
                    str(ROOT / "frontend/scripts/release/sign_windows.ps1"),
                    "-OutputDirectory",
                    str(output),
                    "-BundleOnly",
                ],
                check=True,
            )
        compiler = (
            os.environ.get("INNO_SETUP_COMPILER")
            or shutil.which("ISCC")
            or r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
        )
        subprocess.run(
            [
                compiler,
                f"/DAppVersion={version}",
                f"/DAppSource={bundle / 'Buddy'}",
                f"/DOutputDirectory={output}",
                f"/DOutputName=Buddy-{version}-{revision[:12]}-windows-x64",
                str(ROOT / "frontend/scripts/release/windows.iss"),
            ],
            check=True,
        )
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--signing-identity")
    parser.add_argument("--manifest-only", action="store_true")
    parser.add_argument("--channel", choices=["preview", "stable"], default="preview")
    args = parser.parse_args()
    if not args.manifest_only:
        build(
            args.output.resolve(),
            args.revision,
            args.signing_identity,
            production=args.channel == "stable",
        )
    if sys.platform == "darwin" and not args.manifest_only:
        subprocess.run(
            [
                "ditto",
                "-c",
                "-k",
                "--sequesterRsrc",
                "--keepParent",
                str(args.output / "bundle/Buddy.app"),
                str(args.output / f"Buddy-{args.revision[:12]}-macos-{platform.machine()}.zip"),
            ],
            check=True,
        )
    (args.output / "manifest.json").write_text(
        json.dumps(manifest(args.output, args.revision, args.channel), indent=2) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
