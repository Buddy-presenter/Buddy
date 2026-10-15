"""Public synchronous stage with an isolated parser, deadline and OS memory ceiling."""

import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO

from buddy_api.extraction.contracts import Extraction, ExtractionError, Limits


@contextmanager
def rss_memory_limit(pid: int, memory_bytes: int) -> Iterator[None]:
    """Darwin fallback: sample RSS every 10ms and kill on excess or monitor failure."""
    import psutil  # type: ignore[import-untyped]

    stop = threading.Event()
    errors: list[str] = []
    try:
        child = psutil.Process(pid)
        child.memory_info()  # Verify access before allowing source parsing.
    except psutil.Error:
        raise ExtractionError("isolation_unavailable") from None

    def watch() -> None:
        while not stop.is_set():
            try:
                if child.memory_info().rss > memory_bytes:
                    errors.append("memory_limit")
                    child.kill()
                    return
            except psutil.NoSuchProcess:
                return
            except psutil.Error:
                errors.append("isolation_unavailable")
                with suppress(psutil.NoSuchProcess):
                    child.kill()
                return
            stop.wait(0.01)

    thread = threading.Thread(target=watch, name="buddy-extraction-memory", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
        if errors:
            raise ExtractionError(errors[0])


@contextmanager
def parser_memory_limit(pid: int, memory_bytes: int) -> Iterator[None]:
    if sys.platform == "darwin":
        with rss_memory_limit(pid, memory_bytes):
            yield
    else:
        with windows_memory_limit(pid, memory_bytes):
            yield


@contextmanager
def windows_memory_limit(pid: int, memory_bytes: int) -> Iterator[None]:
    """Attach before sending the start request; fail closed if containment fails."""
    if sys.platform != "win32":
        yield
        return
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_longlong),
            ("job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD),
            ("minimum", ctypes.c_size_t),
            ("maximum", ctypes.c_size_t),
            ("active", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class IO(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "read_ops",
                "write_ops",
                "other_ops",
                "read_bytes",
                "write_bytes",
                "other_bytes",
            )
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("basic", Basic),
            ("io", IO),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process", ctypes.c_size_t),
            ("peak_job", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    job = kernel.CreateJobObjectW(None, None)
    process = kernel.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
    try:
        info = Extended()
        info.basic.flags = 0x0100 | 0x2000  # PROCESS_MEMORY | KILL_ON_JOB_CLOSE
        info.process_memory = memory_bytes
        if not (
            job
            and process
            and kernel.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
            and kernel.AssignProcessToJobObject(job, process)
        ):
            raise ExtractionError("isolation_unavailable")
        yield
    finally:
        if process:
            kernel.CloseHandle(process)
        if job:
            kernel.CloseHandle(job)


def extract_pptx(
    source: Path | BinaryIO,
    *,
    source_revision: str,
    limits: Limits | None = None,
) -> Extraction:
    """Parse an approved/validated local source, never mutate it or return partial success.

    Offload this blocking call from event loops. Caller owns stream lifetime/position;
    streams must be local bounded reads, not arbitrary network callbacks.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", source_revision):
        raise ExtractionError("invalid_source_revision")
    limits = limits or Limits()
    start = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="buddy-extract-") as folder:
            path = Path(folder) / "source.pptx"
            stream = source.open("rb") if isinstance(source, Path) else source
            try:
                with path.open("wb") as output:
                    size = 0
                    while chunk := stream.read(64 * 1024):
                        size += len(chunk)
                        if size > limits.source_bytes:
                            raise ExtractionError("source_limit")
                        if time.monotonic() - start >= limits.seconds:
                            raise ExtractionError("time_limit")
                        output.write(chunk)
            finally:
                if isinstance(source, Path):
                    stream.close()
            if not size:
                raise ExtractionError("invalid_archive")
            request = json.dumps({"revision": source_revision, "limits": limits.model_dump()})
            process = subprocess.Popen(
                [sys.executable, "-m", "buddy_api.extraction.worker", str(path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={
                    **{
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
                            "VIRTUAL_ENV",
                        }
                    },
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            )
            try:
                with parser_memory_limit(process.pid, limits.memory_mb * 1024 * 1024):
                    remaining = limits.seconds - (time.monotonic() - start)
                    if remaining <= 0:
                        raise ExtractionError("time_limit")
                    try:
                        stdout, _ = process.communicate(request.encode(), timeout=remaining)
                    except subprocess.TimeoutExpired:
                        raise ExtractionError("time_limit") from None
                    if len(stdout) > limits.result_bytes:
                        raise ExtractionError("result_limit")
                    if process.returncode != 0:
                        raise ExtractionError("parser_resource_limit")
                    response = json.loads(stdout)
                    if "error" in response:
                        raise ExtractionError(response["error"])
                    result = Extraction.model_validate(response)
                    if time.monotonic() - start >= limits.seconds:
                        raise ExtractionError("time_limit")
                    return result
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate()
    except ExtractionError:
        raise
    except (OSError, ValueError, TypeError):
        raise ExtractionError("source_or_parser_unavailable") from None
