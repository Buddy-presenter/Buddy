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
