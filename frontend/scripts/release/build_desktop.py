"""Build native packages, run the actual frozen GUI, and record artifact integrity."""

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def deployment_config(source: Path | None, *, production: bool = False) -> dict[str, str]:
    from buddy_desktop.config import validate_api_url
    from buddy_identity.config import IdentityConfig, IdentityError

    if production and source is None:
        raise ValueError("Production requires an explicit public deployment configuration.")
    value = json.loads((source or ROOT / "infra/staging/public-config.json").read_text())
    if (
        not isinstance(value, dict)
        or set(value) - {"BUDDY_COGNITO_CALLBACK_URL"}
        != {
            "BUDDY_API_URL",
            "BUDDY_AWS_REGION",
            "BUDDY_COGNITO_POOL_ID",
            "BUDDY_COGNITO_CLIENT_ID",
            "BUDDY_COGNITO_DOMAIN",
        }
        or not all(isinstance(v, str) for v in value.values())
    ):
        raise ValueError("Deployment configuration must contain only public API/identity settings.")
    if not validate_api_url(value["BUDDY_API_URL"]).startswith("https://"):
        raise ValueError("Packaged deployments require HTTPS.")
    try:
        identity = IdentityConfig.from_environment(value)
    except IdentityError:
        raise ValueError(
            "Deployment configuration requires valid public identity settings."
        ) from None
    if identity is None:
        raise ValueError("Deployment configuration requires complete public identity settings.")
    if identity.callback_url.startswith("http:"):
        raise ValueError("Packaged browser return pages require HTTPS.")
    staging = json.loads((ROOT / "infra/staging/public-config.json").read_text())
    if production and any(
        value[key] == staging[key]
        for key in (
            "BUDDY_API_URL",
            "BUDDY_COGNITO_POOL_ID",
            "BUDDY_COGNITO_CLIENT_ID",
            "BUDDY_COGNITO_DOMAIN",
        )
    ):
        raise ValueError("Production must not target staging.")
    return value


def _png(size: int) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    from buddy_desktop.visuals import icon_inset, mascot_image

    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not mascot_image(size, icon_inset(size)).save(buffer, "PNG"):
        raise RuntimeError("Could not render the Buddy icon.")
    return bytes(data.data())


def write_app_icon(folder: Path) -> Path:
    """Package the antialiased Buddy mascot as a multi-resolution .ico or .icns."""
    if sys.platform == "win32":
        sizes = (16, 20, 24, 32, 40, 48, 64, 128, 256)
        images = [_png(size) for size in sizes]
        # ICONDIR, then one ICONDIRENTRY per PNG-compressed image (0 encodes 256).
        header = (0).to_bytes(2, "little") + (1).to_bytes(2, "little")
        header += len(sizes).to_bytes(2, "little")
        offset, entries = 6 + 16 * len(sizes), b""
        for size, image in zip(sizes, images, strict=True):
            entries += bytes([size % 256, size % 256, 0, 0]) + (1).to_bytes(2, "little")
            entries += (32).to_bytes(2, "little") + len(image).to_bytes(4, "little")
            entries += offset.to_bytes(4, "little")
            offset += len(image)
        path = folder / "Buddy.ico"
        path.write_bytes(header + entries + b"".join(images))
        return path
    chunks = b""
    for kind, size in (
        (b"icp4", 16),
        (b"icp5", 32),
        (b"ic11", 32),
        (b"icp6", 64),
        (b"ic12", 64),
        (b"ic07", 128),
        (b"ic08", 256),
        (b"ic13", 256),
        (b"ic09", 512),
        (b"ic14", 512),
        (b"ic10", 1024),
    ):
        image = _png(size)
        chunks += kind + (len(image) + 8).to_bytes(4, "big") + image
    path = folder / "Buddy.icns"
    path.write_bytes(b"icns" + (len(chunks) + 8).to_bytes(4, "big") + chunks)
    return path


def register_macos_callback(bundle: Path, signing_identity: str | None = None) -> None:
    """Register URL activation before the final bundle signature and notarization."""
    info = bundle / "Contents/Info.plist"
    data = plistlib.loads(info.read_bytes())
    data["CFBundleURLTypes"] = [
        {
            "CFBundleURLName": "org.buddy.desktop.oauth",
            "CFBundleURLSchemes": ["org.buddy.desktop"],
            "CFBundleTypeRole": "Viewer",
        }
    ]
    info.write_bytes(plistlib.dumps(data))
    command = ["codesign", "--force", "--sign", signing_identity or "-"]
    if signing_identity:
        command += ["--options", "runtime", "--timestamp"]
    subprocess.run([*command, str(bundle)], check=True)


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
    output: Path,
    revision: str,
    signing_identity: str | None = None,
    *,
    production: bool = False,
    public_config: Path | None = None,
) -> Path:
    if not re.fullmatch(r"[a-f0-9]{40}", revision) or sys.platform not in {"win32", "darwin"}:
        raise ValueError("Native Windows/macOS runner and reviewed revision are required.")
    output.mkdir(parents=True, exist_ok=True)
    public_settings = output / "buddy-deployment.json"
    public_settings.write_text(json.dumps(deployment_config(public_config, production=production)))
    bundle = output / "bundle"
    app_icon = write_app_icon(output)
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
        "--icon",
        str(app_icon),
        "--paths",
        str(ROOT),
        "--collect-data",
        "buddy_desktop",
        "--add-data",
        str(public_settings) + os.pathsep + ".",
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
    elif sys.platform == "win32":
        # COM automation modules are imported at runtime to keep the macOS build clean.
        command += [
            "--hidden-import",
            "pythoncom",
            "--hidden-import",
            "pywintypes",
            "--hidden-import",
            "win32com.client",
        ]
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
    if sys.platform == "darwin":
        register_macos_callback(bundle / "Buddy.app", signing_identity)
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
                f"/DAppIcon={app_icon}",
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
    parser.add_argument("--deployment-config", type=Path)
    parser.add_argument("--manifest-only", action="store_true")
    parser.add_argument("--channel", choices=["preview", "stable"], default="preview")
    args = parser.parse_args()
    if not args.manifest_only:
        build(
            args.output.resolve(),
            args.revision,
            args.signing_identity,
            production=args.channel == "stable",
            public_config=args.deployment_config,
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
