"""Process exit races must not weaken resource isolation or manufacture failures."""

from types import SimpleNamespace

import psutil
import pytest

from buddy_desktop.preflight import _resident_bytes


def test_exited_child_does_not_fail_resident_memory_sampling():
    def exited():
        raise psutil.NoSuchProcess(123)

    assert (
        _resident_bytes(
            [
                SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=4096)),
                SimpleNamespace(memory_info=exited),
                SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=8192)),
            ]
        )
        == 12288
    )


def test_access_denial_still_fails_closed():
    def denied():
        raise psutil.AccessDenied(123)

    with pytest.raises(psutil.AccessDenied):
        _resident_bytes([SimpleNamespace(memory_info=denied)])


def test_scratch_sampling_tolerates_deleted_files_and_keeps_storage_limit(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from buddy_desktop.preflight import _scratch_bytes

    def disappeared():
        raise FileNotFoundError("renderer finished with the temporary file")

    retained = SimpleNamespace(is_file=lambda: True, stat=lambda: SimpleNamespace(st_size=77))
    removed = SimpleNamespace(is_file=lambda: True, stat=disappeared)
    monkeypatch.setattr(type(tmp_path), "rglob", lambda *_: iter((removed, retained)))
    assert _scratch_bytes(tmp_path) == 77


def test_scratch_sampling_does_not_hide_access_failure(monkeypatch, tmp_path):
    import pytest

    from buddy_desktop.preflight import _scratch_bytes

    def inaccessible(*_):
        raise PermissionError("cannot inspect scratch storage")

    monkeypatch.setattr(type(tmp_path), "rglob", inaccessible)
    with pytest.raises(PermissionError):
        _scratch_bytes(tmp_path)
