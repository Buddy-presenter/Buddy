"""Exercise the packaged Qt validator with a synthetic PDF and no container network."""

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2] / "tests/fixtures/slides/Buddy_PDF_Geometry.pdf"
    content = source.read_bytes()
    digest, deck = hashlib.sha256(content).hexdigest(), str(uuid4())
    slide = str(uuid5(NAMESPACE_URL, f"buddy:{digest}:smoke:pdf:0"))
    with tempfile.TemporaryDirectory(prefix="buddy-validator-container-") as directory:
        folder = Path(directory)
        # Only a public synthetic fixture enters this CI-only shared bind mount.
        os.chmod(folder, 0o777)
        (folder / "source.pdf").write_bytes(content)
        (folder / "request.json").write_text(
            json.dumps(
                {
                    "schema_version": "buddy.action-validation-request.v1",
                    "deck_id": deck,
                    "source_revision": "smoke",
                    "source_sha256": digest,
                    "profile": "wide-1x",
                    "action": {
                        "version": "1.1",
                        "instance_id": "container-smoke",
                        "purpose": "Highlight the fixture.",
                        "deck_id": deck,
                        "render_id": "pdfium-5.14.0-144dpi:" + digest,
                        "slide_id": slide,
                        "type": "highlight_region",
                        "params": {"region": {"x": 96, "y": 410, "width": 480, "height": 240}},
                    },
                }
            )
        )
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit=64",
                "--memory=1g",
                "--cpus=1",
                "--mount",
                f"type=bind,source={directory},target=/job",
                "--entrypoint",
                "/opt/buddy-validator/bin/python",
                args.image,
                "-m",
                "buddy_desktop.validation_worker",
                "/job",
            ],
            input=b"1",
            timeout=90,
            check=False,
        )
        if result.returncode:
            return 1
        report = json.loads((folder / "result.json").read_text())
        if report["state"] != "validated" or report["semantic_verified"]:
            print("Container validator failed deterministic execution checks.")
            return 1
        if not all((folder / name).is_file() for name in ("before.png", "after.png")):
            return 1
        print("Packaged validator: real PDF action and before/after evidence passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
