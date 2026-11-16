"""Trusted preflight host. Agents receive only a bounded plan, never host configuration."""

import hashlib
import importlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from threading import Event
from typing import Any, Literal, TypedDict
from uuid import uuid4

import psutil  # type: ignore[import-untyped]

from buddy_desktop.slide_contract import ContractError, validate
from buddy_desktop.validation_worker import PROFILES


class Tool(TypedDict, total=False):
    op: Literal["load_slide", "reset", "apply_action", "read_state", "capture"]
    slide_id: str
    action_id: str


def check_tools(tools: object, catalog: dict[str, Any], slides: dict[str, Any]) -> None:
    if not isinstance(tools, list) or not 1 <= len(tools) <= 16:
        raise ContractError("A preflight plan needs 1–16 tools.")
    if sum(isinstance(t, dict) and t.get("op") == "capture" for t in tools) > 4:
        raise ContractError("At most four captures are allowed.")
    for tool in tools:
        if not isinstance(tool, dict):
            raise ContractError("Invalid preflight tool.")
        op = tool.get("op")
        if not isinstance(op, str):
            raise ContractError("Invalid preflight operation.")
        keys = {"op"}
        if op == "load_slide":
            keys.add("slide_id")
            if not isinstance(tool.get("slide_id"), str) or tool["slide_id"] not in slides:
                raise ContractError("Unknown preflight slide.")
        elif op == "apply_action":
            keys.add("action_id")
            if not isinstance(tool.get("action_id"), str) or tool["action_id"] not in catalog:
                raise ContractError("Unknown preflight action.")
        elif op not in {"reset", "read_state", "capture"}:
            raise ContractError("Tool is not allowlisted.")
        if set(tool) != keys:
            raise ContractError("Unexpected preflight tool fields.")


def _private(folder: Path) -> None:
    """Restrict evidence to the OS account running this trusted host."""
    if sys.platform == "win32":
        win32api = importlib.import_module("win32api")
        win32security = importlib.import_module("win32security")

        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), 8)
        try:
            sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
            acl = win32security.ACL()
            acl.AddAccessAllowedAceEx(2, 3, 0x1F01FF, sid)
            win32security.SetNamedSecurityInfo(str(folder), 1, -2147483644, None, None, acl, None)
        finally:
            token.Close()
    else:
        folder.chmod(0o700)


class PreflightRunner:
    """One owner, private temporary evidence, at most eight retained runs.

    Instantiate inside the trusted deck-owner boundary. Source bytes and catalog are
    immutable snapshots fetched by that host, not agent-controlled paths or URLs.
    Closing the host deletes every artifact. It is not a multi-tenant HTTP endpoint.
    """

    def __init__(self, owner: str) -> None:
        if not owner:
            raise ValueError("An authenticated owner is required.")
        self._owner = owner
        self._temporary = tempfile.TemporaryDirectory(prefix="buddy-preflight-")
        self._root = Path(self._temporary.name)
        _private(self._root)
        self._runs: dict[str, float] = {}
        self._closed = False

    def _authorize(self, owner: str) -> None:
        if self._closed or owner != self._owner:
            raise PermissionError("Preflight session is unavailable.")

    def close(self) -> None:
        self._closed = True
        self._temporary.cleanup()

    def __enter__(self) -> "PreflightRunner":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _expire(self) -> None:
        for run, created in list(self._runs.items()):
            if time.monotonic() - created > 3600:
                shutil.rmtree(self._root / run)
                del self._runs[run]

    def artifact(self, owner: str, run: str, name: str) -> bytes:
        self._authorize(owner)
        self._expire()
        if run not in self._runs or name not in {"report.json", *[f"{i}.png" for i in range(4)]}:
            raise FileNotFoundError("Unknown preflight artifact.")
        return (self._root / run / name).read_bytes()

    def run(
        self,
        owner: str,
        *,
        source: bytes,
        source_format: Literal["pdf", "pptx"],
        deck_id: str,
        revision: str,
        slides: dict[str, dict[str, Any]],
        catalog: dict[str, dict[str, Any]],
        tools: list[Tool],
        profile: str = "wide-1x",
        viewport: tuple[int, int] | None = None,
        cancelled: Event | None = None,
    ) -> dict[str, Any]:
        self._authorize(owner)
        self._expire()
        if source_format not in {"pdf", "pptx"} or profile not in PROFILES:
            raise ContractError("Unsupported preflight source/profile.")
        viewport = viewport or PROFILES[profile][:2]
        if len(viewport) != 2 or any(type(v) is not int or not 320 <= v <= 4096 for v in viewport):
            raise ContractError("Invalid explicit preflight viewport.")
        if not 1 <= len(source) <= 64 * 1024 * 1024 or not 1 <= len(slides) <= 500:
            raise ContractError("Preflight source exceeds bounds.")
        if len(catalog) > 1000 or not deck_id or not revision:
            raise ContractError("Invalid preflight snapshot.")
        check_tools(tools, catalog, slides)
        # Validate catalog JSON before launching any native process.
        for action in catalog.values():
            schema = "powerpoint_contracts" if source_format == "pptx" else "slide_contracts"
            if source_format == "pdf":
                validate(
                    action,
                    "action-v1.1.schema.json"
                    if action.get("version") == "1.1"
                    else "action-v1.schema.json",
                )
            else:
                from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

                definition = json.loads(
                    (Path(__file__).parent / schema / "action-v2.schema.json").read_text()
                )
                if not Draft202012Validator(definition).is_valid(action):
                    raise ContractError("Invalid PowerPoint catalog action.")
            if action["deck_id"] != deck_id or action["slide_id"] not in slides:
                raise ContractError("Catalog does not belong to this snapshot.")
        run = uuid4().hex
        if len(self._runs) >= 8:
            oldest = next(iter(self._runs))
            shutil.rmtree(self._root / oldest)
            del self._runs[oldest]
        folder = self._root / run
        request = dict(
            source_format=source_format,
            deck_id=deck_id,
            revision=revision,
            source_sha256=hashlib.sha256(source).hexdigest(),
            slides=slides,
            catalog=catalog,
            tools=tools,
            profile=profile,
            viewport=viewport,
        )
        raw = json.dumps(request, allow_nan=False).encode()
        if len(raw) > 1024 * 1024:
            raise ContractError("Preflight snapshot exceeds 1 MiB.")
        folder.mkdir(mode=0o700)
        (folder / f"source.{source_format}").write_bytes(source)
        (folder / "request.json").write_bytes(raw)
        report: dict[str, Any] = dict(
            schema_version="buddy.preflight.v1",
            run_id=run,
            state="needs-review",
            reason="worker_failed",
            source_sha256=request["source_sha256"],
            source_format=source_format,
            source_revision=revision,
            semantic_verified=False,
            artifacts=[],
        )
        try:
            reason = self._launch(folder, profile, cancelled, source_format)
            if reason:
                report["reason"] = reason
            else:
                with (folder / "result.json").open("rb") as result_file:
                    raw_result = result_file.read(256 * 1024 + 1)
                if len(raw_result) > 256 * 1024:
                    raise ContractError("Preflight trace exceeds bounds.")
                payload = json.loads(raw_result)
                if (
                    not isinstance(payload, dict)
                    or payload.get("source_sha256") != request["source_sha256"]
                ):
                    raise ContractError("Invalid preflight result.")
                report.update(payload)
            total = 0
            artifacts = []
            for image in sorted(folder.glob("*.png")):
                with image.open("rb") as image_file:
                    content = image_file.read(16 * 1024 * 1024 + 1)
                total += len(content)
                if (
                    image.name not in {f"{i}.png" for i in range(4)}
                    or len(content) > 16 * 1024 * 1024
                    or not content.startswith(b"\x89PNG\r\n\x1a\n")
                ):
                    raise ContractError("Preflight artifact exceeds bounds.")
                artifacts.append(dict(name=image.name, sha256=hashlib.sha256(content).hexdigest()))
            if total > 64 * 1024 * 1024:
                raise ContractError("Preflight evidence exceeds bounds.")
            report["artifacts"] = artifacts
        except (OSError, ValueError):
            report.update(state="needs-review", reason="invalid_worker_result")
        finally:
            # Retain only versioned traces and captures; never retain source/private profile.
            for path in list(folder.iterdir()):
                if path.suffix != ".png":
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
        (folder / "report.json").write_text(json.dumps(report, allow_nan=False), encoding="utf-8")
        self._runs[run] = time.monotonic()
        return report

    @staticmethod
    def _launch(
        folder: Path, profile: str, cancelled: Event | None, source_format: str = "pdf"
    ) -> str | None:
        if cancelled is not None and cancelled.is_set():
            return "cancelled"
        for name in ("profile", "roaming", "local", "temp"):
            (folder / name).mkdir()
        for name in ("Desktop", "Documents", "AppData"):
            (folder / "profile" / name).mkdir()
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL"}
        }
        environment.update(
            HOME=str(folder / "profile"),
            USERPROFILE=str(folder / "profile"),
            TEMP=str(folder / "temp"),
            TMP=str(folder / "temp"),
            TMPDIR=str(folder / "temp"),
            APPDATA=str(folder / "roaming"),
            LOCALAPPDATA=str(folder / "local"),
            XDG_CONFIG_HOME=str(folder),
            XDG_CACHE_HOME=str(folder),
            QT_QPA_PLATFORM="windows"
            if sys.platform == "win32"
            else "cocoa"
            if sys.platform == "darwin"
            else "offscreen",
            QT_SCALE_FACTOR="1",
            QT_SCREEN_SCALE_FACTORS=str(PROFILES[profile][2]),
            PYTHONDONTWRITEBYTECODE="1",
            QML_DISK_CACHE_PATH=str(folder / "local" / "qml"),
            QT_SHADER_CACHE_DIR=str(folder / "local" / "shaders"),
        )
        if source_format == "pptx" and sys.platform == "win32":
            # Office's COM server uses the actual interactive Windows identity.
            # Fake profile paths cannot isolate that identity and can break its
            # activation/cache lookup. The operator supplies a dedicated Office
            # test account; Qt settings and all job data remain private below.
            for name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
                if name in os.environ:
                    environment[name] = os.environ[name]
        process = subprocess.Popen(
            [sys.executable, "-m", "buddy_desktop.preflight_worker", str(folder)],
            cwd=folder,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        monitor: Any = None
        deadline = time.monotonic() + 90
        job: Any = None
        jobs: Any = None
        api: Any = None
        assigned_office: int | None = None
        try:
            monitor = psutil.Process(process.pid)
            if sys.platform == "win32":
                jobs = importlib.import_module("win32job")
                api = importlib.import_module("win32api")
                job = jobs.CreateJobObject(None, "BuddyPreflight-" + uuid4().hex)
                limits = jobs.QueryInformationJobObject(job, jobs.JobObjectExtendedLimitInformation)
                # Office reserves far more committed address space than resident
                # memory. Bound aggregate RSS below, retain kill-on-close here.
                limits["BasicLimitInformation"]["LimitFlags"] = 0x2000
                jobs.SetInformationJobObject(job, jobs.JobObjectExtendedLimitInformation, limits)
                handle = api.OpenProcess(0x0101, False, process.pid)
                try:
                    jobs.AssignProcessToJobObject(job, handle)
                finally:
                    handle.Close()
            assert process.stdin is not None
            process.stdin.write(b"1")
            process.stdin.close()
            while process.poll() is None:
                if cancelled is not None and cancelled.is_set():
                    return "cancelled"
                if time.monotonic() > deadline:
                    return "time_limit"
                with suppress(psutil.NoSuchProcess):
                    office_path = folder / "office-process.json"
                    if assigned_office is None and job is not None and office_path.exists():
                        owned = json.loads(office_path.read_text())
                        office = psutil.Process(owned["pid"])
                        if (
                            office.name().lower() != "powerpnt.exe"
                            or office.create_time() != owned["created"]
                        ):
                            return "isolation_unavailable"
                        handle = api.OpenProcess(0x0101, False, office.pid)
                        try:
                            jobs.AssignProcessToJobObject(job, handle)
                        finally:
                            handle.Close()
                        assigned_office = office.pid
                    processes = {p.pid: p for p in [monitor, *monitor.children(recursive=True)]}
                    if assigned_office is not None:
                        processes[assigned_office] = psutil.Process(assigned_office)
                    if sum(p.memory_info().rss for p in processes.values()) > 1536 * 1024 * 1024:
                        return "memory_limit"
                if (
                    sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())
                    > 256 * 1024 * 1024
                ):
                    return "storage_limit"
                time.sleep(0.02)
            return None if process.returncode == 0 else "worker_failed"
        except Exception:
            return "isolation_unavailable"
        finally:
            if job is not None:
                job.Close()
            # Kill only the owned process tree, never discover or attach user apps.
            with suppress(psutil.NoSuchProcess, AttributeError):
                for child in monitor.children(recursive=True):
                    with suppress(psutil.NoSuchProcess):
                        child.kill()
            if sys.platform != "win32":
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            if process.poll() is None:
                process.kill()
            process.wait()
            with suppress(OSError, ValueError, KeyError, psutil.Error):
                owned = json.loads((folder / "office-process.json").read_text())
                office = psutil.Process(owned["pid"])
                if (
                    office.name().lower() == "powerpnt.exe"
                    and office.create_time() == owned["created"]
                ):
                    office.kill()
                    office.wait(5)
