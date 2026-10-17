"""Immutable PDF inputs and bounded, isolated PDFium rendering for the slide executor."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import psutil  # type: ignore[import-untyped]
from PySide6.QtGui import QImage

from buddy_desktop.slide_contract import ContractError, Manifest, Slide


def render(content: bytes, index: int = 0) -> tuple[dict[str, Any], QImage]:
    if not content.startswith(b"%PDF-") or not 1 <= len(content) <= 64 * 1024 * 1024:
        raise ContractError("Choose a PDF presentation up to 64 MiB.")
    try:
        with tempfile.TemporaryDirectory(prefix="buddy-pdf-") as directory:
            folder = Path(directory)
            (folder / "source.pdf").write_bytes(content)
            (folder / "request.json").write_text(json.dumps({"index": index}))
            command = (
                [sys.executable, "--pdf-worker"]
                if getattr(sys, "frozen", False)
                else [sys.executable, "-m", "buddy_desktop.pdf_worker"]
            )
            environment = {
                key: value
                for key, value in os.environ.items()
                if key.upper()
                in {
                    "PATH",
                    "SYSTEMROOT",
                    "WINDIR",
                    "TEMP",
                    "TMP",
                    "HOME",
                    "USERPROFILE",
                    "LANG",
                    "LC_ALL",
                }
            }
            process = subprocess.Popen(
                command + [str(folder)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                monitor = psutil.Process(process.pid)
                deadline = time.monotonic() + 20
                while process.poll() is None:
                    try:
                        rss = monitor.memory_info().rss
                    except psutil.NoSuchProcess:
                        if process.poll() is not None:
                            break
                        raise
                    if time.monotonic() > deadline or rss > 512 * 1024 * 1024:
                        raise ContractError("PDF exceeds the rendering time or memory limit.")
                    time.sleep(0.01)
                if process.returncode:
                    raise ContractError(
                        "Cannot open this PDF. It may be encrypted, damaged, or exceed page limits."
                    )
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
            with (folder / "result.json").open("rb") as output:
                raw_metadata = output.read(32769)
            if len(raw_metadata) > 32768:
                raise ContractError("PDF renderer returned oversized metadata.")
            metadata = json.loads(raw_metadata)
            width, height, stride = (metadata[key] for key in ("width", "height", "stride"))
            sizes = metadata["sizes"]
            if (
                not isinstance(sizes, list)
                or not 1 <= len(sizes) <= 500
                or any(
                    not isinstance(size, list)
                    or len(size) != 2
                    or any(type(v) is not int or not 1 <= v <= 8192 for v in size)
                    or size[0] * size[1] > 16_777_216
                    for size in sizes
                )
                or not 0 <= index < len(sizes)
                or any(type(v) is not int for v in (width, height, stride))
                or [width, height] != sizes[index]
                or stride != width * 4
            ):
                raise ContractError("PDF renderer returned invalid page geometry.")
            with (folder / "pixels.bin").open("rb") as output:
                pixels = output.read(stride * height + 1)
            if len(pixels) != stride * height:
                raise ContractError("PDF renderer returned incomplete pixels.")
            image = QImage(
                pixels,
                metadata["width"],
                metadata["height"],
                metadata["stride"],
                QImage.Format.Format_ARGB32,
            ).copy()
            if image.isNull():
                raise ContractError("PDF page could not be rendered.")
            return metadata, image
    except ContractError:
        raise
    except (OSError, ValueError, KeyError, psutil.Error):
        raise ContractError(
            "PDF rendering is unavailable. Try another file or repair Buddy."
        ) from None


def open_pdf(
    path: Path | bytes, *, deck_id: str | None = None, revision: str | None = None
) -> tuple[Manifest, QImage]:
    try:
        if isinstance(path, Path):
            with path.open("rb") as stream:
                content = stream.read(64 * 1024 * 1024 + 1)
        else:
            content = path
    except OSError:
        raise ContractError("PDF is missing or unreadable.") from None
    metadata, image = render(content)
    digest = hashlib.sha256(content).hexdigest()
    revision = revision or digest
    slides = tuple(
        Slide(
            str(uuid5(NAMESPACE_URL, f"buddy:{digest}:{revision}:pdf:{index}")),
            width,
            height,
            "",
            width,
            height,
            digest,
        )
        for index, (width, height) in enumerate(metadata["sizes"])
    )
    return Manifest(
        deck_id or digest,
        "pdfium-5.14.0-144dpi:" + digest,
        slides,
        Path("."),
        pdf_content=content,
        account_bound=deck_id is not None,
    ), image
