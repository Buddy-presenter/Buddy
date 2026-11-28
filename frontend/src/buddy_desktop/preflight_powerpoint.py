"""Dedicated Office test-session adapter; never attaches an existing user slideshow."""

import importlib
import io
import json
import sys
import time
import zipfile
from contextlib import suppress
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from xml.etree import ElementTree

import psutil  # type: ignore[import-untyped]
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from buddy_desktop.powerpoint import PowerPointAutomation
from buddy_desktop.powerpoint_contract import PowerPointAction, PresentationInfo
from buddy_desktop.powerpoint_generated import generated_action
from buddy_desktop.powerpoint_zoom_evidence import capture, distance
from buddy_desktop.slide_contract import ContractError
from buddy_desktop.validation_worker import PROFILES


def safe_source(content: bytes) -> None:
    """Only bounded passive PPTX packages, even when a catalog came from analysis."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        if (
            len(entries) > 4096
            or len({i.filename for i in entries}) != len(entries)
            or sum(i.file_size for i in entries) > 128 * 1024 * 1024
        ):
            raise ContractError("unsafe_office_content")
        for entry in entries:
            name = entry.filename.lower()
            if ".." in name.split("/") or name.startswith("/") or "\\" in name:
                raise ContractError("unsafe_office_content")
            if any(part in name for part in ("vbaproject", "activex/", "embeddings/")):
                raise ContractError("unsafe_office_content")
            if name.endswith(".rels"):
                raw = archive.read(entry)
                if b"\x00" in raw or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                    raise ContractError("unsafe_office_content")
                root = ElementTree.fromstring(raw)
                if any(r.attrib.get("TargetMode", "").lower() == "external" for r in root):
                    raise ContractError("unsafe_office_content")


def private_com_caches(folder: Path) -> None:
    """Generate COM wrappers per job instead of sharing installed/user caches."""
    win32_cache = folder / "com-cache" / "win32com"
    comtypes_cache = folder / "com-cache" / "comtypes"
    for path in (win32_cache, comtypes_cache):
        path.mkdir(parents=True)
    win32com: Any = importlib.import_module("win32com")
    win32com.__gen_path__ = str(win32_cache)
    win32com.gen_py.__path__ = [str(win32_cache)]
    comtypes: Any = importlib.import_module("comtypes")
    generated: Any = ModuleType("comtypes.gen")
    generated.__path__ = [str(comtypes_cache)]
    sys.modules["comtypes.gen"] = generated
    comtypes.gen = generated
    client: Any = importlib.import_module("comtypes.client")
    client.gen_dir = str(comtypes_cache)


class OfficePlayer:
    office: Any
    presentation: Any
    adapter: Any
    request: dict[str, Any]
    app: QApplication
    pythoncom: Any
    lease: Any
    pid: int
    info: PresentationInfo
    current_id: str
    runtime: dict[str, Any]

    def __init__(self, app: QApplication, folder: Path, request: dict[str, Any]) -> None:
        if sys.platform != "win32":
            raise ContractError("powerpoint_requires_windows_office")
        safe_source((folder / "source.pptx").read_bytes())
        if PowerPointAutomation._powerpoint_process_running():
            raise ContractError("powerpoint_worker_busy_use_dedicated_session")
        events = importlib.import_module("win32event")
        self.lease = events.CreateMutex(None, False, "Local\\BuddyPreflightOffice")
        if events.WaitForSingleObject(self.lease, 0) not in {0, 0x80}:
            self.lease.Close()
            raise ContractError("powerpoint_worker_busy_use_dedicated_session")
        # Check again inside the process-wide test-session lease.
        if PowerPointAutomation._powerpoint_process_running():
            events.ReleaseMutex(self.lease)
            self.lease.Close()
            raise ContractError("powerpoint_worker_busy_use_dedicated_session")
        self.office: Any = None
        self.presentation: Any = None
        self.adapter: Any = None
        self.request = request
        self.app = app
        self.pythoncom = importlib.import_module("pythoncom")
        self.pythoncom.CoInitialize()
        stage = "startup"
        try:
            private_com_caches(folder)
            started = time.time()
            self.office = importlib.import_module("win32com.client").DispatchEx(
                "PowerPoint.Application"
            )
            processes = [
                p
                for p in psutil.process_iter(["name", "create_time"])
                if (p.info["name"] or "").lower() == "powerpnt.exe"
                and p.info["create_time"] >= started - 1
            ]
            if len(processes) != 1 or self.office.Presentations.Count:
                # If ownership is ambiguous, don't issue Quit to an unknown process.
                self.office = None
                raise ContractError("powerpoint_worker_busy_use_dedicated_session")
            process = processes[0]
            self.pid = process.pid
            (folder / "office-process.json").write_text(
                json.dumps(dict(pid=self.pid, created=process.create_time()))
            )
            self.office.AutomationSecurity = 3
            self.office.DisplayAlerts = 1
            stage = "launch"
            self.adapter = PowerPointAutomation(
                com_loader=lambda: (
                    SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=lambda: None),
                    SimpleNamespace(Dispatch=lambda _name: self.office),
                ),
                progid_probe=lambda: None,
                process_probe=lambda: True,
            )
            self.info = self.adapter.launch(folder / "source.pptx")
            self.presentation = self.office.Presentations.Item(1)
            # Production zoom requires a fullscreen audience window on the primary
            # display. Office always plays at that display's physical size, so the
            # actual client viewport is recorded rather than a host-chosen size.
            # The display profile must match the display's real scale (1x or 2x),
            # the same rule the PDF player and Present use.
            width, height = request["viewport"]
            dpr = PROFILES[request["profile"]][2]
            screen = app.primaryScreen()
            if screen is None or abs(screen.devicePixelRatio() - dpr) > 0.001:
                raise ContractError("unsupported_display_profile")
            self.runtime = dict(
                renderer_version="desktop-powerpoint-2",
                runtime_os="Windows",
                office_version=f"{self.office.Version}.{self.office.Build}",
                office_identity="interactive_windows_user",
                com_cache_private=True,
                requested_viewport=[width, height],
                display_scale=dpr,
            )
            self.current_id = next(iter(request["slides"]))
            stage = "baseline"
            self.load(self.current_id)
            driver = self.adapter._zoom_driver()
            with driver._physical_pixels():
                left, top, right, bottom = driver.gui.GetClientRect(self.info.window_id)
            self.runtime["viewport"] = [right - left, bottom - top]
            geometry = screen.geometry()
            physical = [round(geometry.width() * dpr), round(geometry.height() * dpr)]
            # The slideshow must fill the primary display; anything else (windowed,
            # another monitor, a resolution change) fails closed.
            if self.runtime["viewport"] != physical:
                raise ContractError("unsupported_display_profile")
        except Exception as exc:
            self.close()
            if isinstance(exc, ContractError):
                raise
            raise ContractError("powerpoint_" + stage + "_failed") from exc

    def _guard(self) -> None:
        gui = importlib.import_module("win32process")
        if gui.GetWindowThreadProcessId(self.info.window_id)[1] != self.pid:
            raise ContractError("Office audience window changed ownership.")
        if self.office.Presentations.Count != 1:
            raise ContractError("Dedicated Office session is no longer isolated.")

    def load(self, slide_id: str) -> None:
        self._guard()
        self.reset()
        slide = self.request["slides"][slide_id]
        self.info = self.adapter.execute(
            PowerPointAction("go_to_slide", self.info, slide_number=slide["number"])
        ).info
        if self.info.slide_id != slide["native_id"]:
            raise ContractError("Native PowerPoint slide differs from analysis.")
        self.current_id = slide_id
        self.reset()

    def reset(self) -> None:
        self._guard()
        for kind in ("reset_zoom", "erase_drawing"):
            self.info = self.adapter.execute(PowerPointAction(kind, self.info)).info

    def apply(self, raw: dict[str, Any]) -> None:
        self._guard()
        slide = self.request["slides"][self.current_id]
        action = generated_action(
            raw,
            self.info,
            deck_id=self.request["deck_id"],
            render_id="pptx-" + self.request["source_sha256"],
            slide_id=self.current_id,
            native_slide_id=slide["native_id"],
            width=slide["width"],
            height=slide["height"],
        )
        self.info = self.adapter.execute(action).info

    def state(self) -> dict[str, Any]:
        self._guard()
        plan = self.adapter._zoom_driver().magnifier.owned
        return dict(
            slide_id=self.current_id,
            native_id=self.info.slide_id,
            slide_number=self.info.slide_index,
            width=self.info.slide_width,
            height=self.info.slide_height,
            managed_shapes=self.presentation.Slides.Item(self.info.slide_index).Shapes.Count,
            zoom=None if plan is None else dict(scale=plan.scale, x=plan.x, y=plan.y),
        )

    def _require_clear_viewport(self, driver: Any) -> None:
        with driver._physical_pixels():
            rect = driver.gui.GetClientRect(self.info.window_id)
            origin = driver.gui.ClientToScreen(self.info.window_id, (0, 0))
            for x in (0.1, 0.3, 0.5, 0.7, 0.9):
                for y in (0.1, 0.3, 0.5, 0.7, 0.9):
                    hit = driver.gui.WindowFromPoint(
                        (origin[0] + round(rect[2] * x), origin[1] + round(rect[3] * y))
                    )
                    if driver.gui.GetAncestor(hit, 2) != self.info.window_id:
                        raise ContractError("audience_viewport_occluded")

    def capture(self) -> Any:
        self._guard()
        self.app.processEvents()
        driver = self.adapter._zoom_driver()
        driver.focus(self.info)
        try:
            # Keep the isolated slideshow's navigation chrome out of the evidence.
            window = self.adapter._target_window(self.office, self.info)
            window.View.PointerType = 3  # ppSlideShowPointerAlwaysHidden
        except Exception as exc:
            raise ContractError("desktop_capture_unavailable") from exc

        # A window can briefly cover the slideshow while Office is activating.
        # Require a clear viewport on both sides of the accepted capture.
        for attempt in range(2):
            try:
                self._require_clear_viewport(driver)
                image = capture(self.info, driver)
                self._require_clear_viewport(driver)
                break
            except ContractError:
                if attempt:
                    raise
                time.sleep(0.4)
                self.app.processEvents()
                driver.focus(self.info)
            except Exception as exc:
                raise ContractError("desktop_capture_unavailable") from exc

        source = QImage.fromData(self.adapter.preview(self.info).png)
        if source.isNull():
            raise ContractError("desktop_capture_unavailable")
        reference = office_reference(
            source,
            image.size().width(),
            image.size().height(),
            self.runtime["viewport"],
            driver.magnifier.owned,
        )
        if distance(image, reference) > 0.015:
            raise ContractError("audience_viewport_occluded")
        return image

    def close(self) -> None:
        if self.adapter is not None:
            with suppress(Exception):
                self.adapter.release_zoom()
        if self.presentation is not None:
            with suppress(Exception):
                self.presentation.Saved = True
                self.presentation.Close()
        if self.office is not None:
            with suppress(Exception):
                self.office.Quit()
        self.pythoncom.CoUninitialize()
        importlib.import_module("win32event").ReleaseMutex(self.lease)
        self.lease.Close()


def office_reference(
    source: QImage, width: int, height: int, viewport: list[int], plan: Any
) -> QImage:
    """Occlusion guard only. Evidence always comes from the desktop, never this export."""
    reference = QImage(width, height, QImage.Format.Format_RGB32)
    reference.fill(Qt.GlobalColor.black)
    painter = QPainter(reference)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.scale(width / viewport[0], height / viewport[1])
    if plan is not None:
        painter.scale(plan.scale, plan.scale)
        painter.translate(-plan.x, -plan.y)
    scale = min(viewport[0] / source.width(), viewport[1] / source.height())
    w, h = source.width() * scale, source.height() * scale
    painter.drawImage(QRectF((viewport[0] - w) / 2, (viewport[1] - h) / 2, w, h), source)
    painter.end()
    return reference
