"""Exclusive per-container scratch ownership; never delete a live worker's files."""

import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def root() -> Path:
    return Path(tempfile.gettempdir()) / "buddy-processing"


@contextmanager
def workspace() -> Iterator[Path]:
    directory = root()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("Worker scratch must not be a symlink")
    # Container/task scratch volumes must not be shared between replicas.
    with (directory / "worker.lock").open("a+b") as lock:
        if sys.platform == "win32":
            import msvcrt

            lock.seek(0)
            lock.write(b"0")
            lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        for abandoned in directory.glob("work-*"):
            if abandoned.is_dir() and not abandoned.is_symlink():
                shutil.rmtree(abandoned)
        with tempfile.TemporaryDirectory(prefix="work-", dir=directory) as current:
            previous = tempfile.tempdir
            tempfile.tempdir = current
            try:
                yield Path(current)
            finally:
                tempfile.tempdir = previous
