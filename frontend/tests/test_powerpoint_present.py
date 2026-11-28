"""Native policy boundaries substitute Office only, never saved geometry or policy rules."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from frontend.tests.test_powerpoint_session_binding import example

from buddy_desktop.powerpoint_contract import PowerPointActionResult, PowerPointError
from buddy_desktop.powerpoint_present import PowerPointPresent
from buddy_desktop.powerpoint_session_binding import PowerPointSessionBinding
from buddy_desktop.transition_policy import Target


def prepared(tmp_path):
    analysis, content, info = example()
    path = tmp_path / "saved.pptx"
    path.write_bytes(content)
    info = replace(info, path=str(path))
    return PowerPointPresent(PowerPointSessionBinding.create(analysis, content, info)), info, path


def test_native_navigation_is_bound_to_exact_source_and_native_ids(tmp_path):
    native, info, path = prepared(tmp_path)
    command = native.command(Target("next_slide", "slide-2"), info)
    calls = []

    def execute(action, **kwargs):
        calls.append((action, kwargs))
        assert kwargs["commit_guard"]()
        return PowerPointActionResult(action, replace(info, slide_index=2, slide_id=102))

    native.execute(SimpleNamespace(execute=execute), command, guard=lambda: True)
    assert len(calls) == 1 and native.focus == Target("slide", "slide-2")
    path.write_bytes(b"changed")
    with pytest.raises(PowerPointError, match="source changed"):
        native.execute(SimpleNamespace(execute=execute), command)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "target",
    [
        Target("next_slide", "slide-1"),
        Target("go_to_slide", "invented"),
        Target("zoom_region", "slide-1", "invented", (0, 0, 20, 20)),
    ],
)
def test_native_rejects_stale_navigation_and_invented_regions(tmp_path, target):
    native, info, _ = prepared(tmp_path)
    with pytest.raises(PowerPointError):
        native.command(target, info)
