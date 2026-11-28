"""Saved native actions and reversible policy transitions on the actual Office surface."""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from buddy_desktop.powerpoint import PowerPointAutomation
from buddy_desktop.powerpoint_contract import (
    PowerPointAction,
    PowerPointActionResult,
    PowerPointError,
    PresentationInfo,
)
from buddy_desktop.powerpoint_generated import generated_action
from buddy_desktop.powerpoint_session_binding import PowerPointSessionBinding
from buddy_desktop.presentation_session_service import SessionState
from buddy_desktop.review_service import ReviewAction
from buddy_desktop.transition_policy import Target


class PowerPointPresent:
    def __init__(self, binding: PowerPointSessionBinding):
        self.binding = binding
        self.focus: Target | None = None
        self.last_annotation = False

    def verify_source(self) -> None:
        with Path(self.binding.path).open("rb") as handle:
            content = handle.read(64 * 1024 * 1024 + 1)
        if (
            len(content) > 64 * 1024 * 1024
            or hashlib.sha256(content).hexdigest() != self.binding.source_sha256
        ):
            raise PowerPointError("The saved PowerPoint source changed. Reopen it from Library.")

    def saved(self, identifier: str, info: PresentationInfo) -> ReviewAction:
        self.binding.slide_id(info)
        slide = self.binding.slides[info.slide_index - 1]
        action = next((a for a in slide.actions if a.instance_id == identifier), None)
        if (
            action is None
            or not action.usable
            or action.state != "validated"
            or not action.semantic_verified
            or action.validation_kind != "visual"
            or action.raw is None
            or not action.runtime_version
        ):
            raise PowerPointError("This action is unavailable for the current native slide.")
        digest = hashlib.sha256(
            json.dumps(action.raw, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        if digest != action.action_sha256:
            raise PowerPointError("The saved action changed after validation.")
        return action

    def command(self, target: Target, info: PresentationInfo) -> PowerPointAction:
        current = self.binding.slide_id(info)
        ids = [s.slide_id for s in self.binding.slides]
        if target.slide_id not in ids:
            raise PowerPointError("The target is not part of this saved presentation.")
        index = ids.index(target.slide_id)
        if target.tool in {"next_slide", "previous_slide", "go_to_slide"}:
            offset = index - (info.slide_index - 1)
            if (
                target.action_id is not None
                or target.region is not None
                or (
                    target.tool != "go_to_slide"
                    and offset != (1 if target.tool == "next_slide" else -1)
                )
            ):
                raise PowerPointError("The navigation target is stale.")
            action = PowerPointAction(
                target.tool, info, slide_number=index + 1 if target.tool == "go_to_slide" else None
            )
        elif target.tool == "reset_view" and target.slide_id == current:
            action = PowerPointAction("reset_zoom", info)
        else:
            if target.slide_id != current or target.action_id is None:
                raise PowerPointError("Regional actions must target the current slide.")
            saved = self.saved(target.action_id, info)
            if (
                not saved.semantic_verified
                or saved.action_type != target.tool
                or saved.region != target.region
            ):
                raise PowerPointError(
                    "Automatic movement requires independently verified geometry."
                )
            action = self.saved_command(saved.instance_id, info)
        action.validate()
        return action

    def saved_command(self, identifier: str, info: PresentationInfo) -> PowerPointAction:
        saved = self.saved(identifier, info)
        slide = self.binding.slides[info.slide_index - 1]
        assert saved.raw is not None
        return generated_action(
            saved.raw,
            info,
            deck_id=self.binding.deck_id,
            render_id=self.binding.render_id,
            slide_id=slide.slide_id,
            native_slide_id=slide.native_slide_id,
            width=slide.width,
            height=slide.height,
        )

    def execute(
        self,
        automation: PowerPointAutomation,
        command: PowerPointAction,
        *,
        identifier: str | None = None,
        guard: Callable[[], bool] | None = None,
    ) -> PowerPointActionResult:
        self.verify_source()
        runtime = self.saved(identifier, command.target).runtime_version if identifier else None
        if identifier and self.saved_command(identifier, command.target) != command:
            raise PowerPointError("The native command differs from its validated action.")
        result = automation.execute(command, expected_runtime=runtime, commit_guard=guard)
        slide_id = self.binding.slide_id(result.info)
        region = command.region
        self.focus = (
            Target(
                command.kind,
                slide_id,
                identifier,
                (region.x, region.y, region.width, region.height),
            )
            if region and command.kind in {"zoom_region", "highlight_region"}
            else Target("slide", slide_id)
        )
        self.last_annotation = identifier is not None and command.kind != "zoom_region"
        return result

    def restore(
        self, automation: PowerPointAutomation, info: PresentationInfo, state: SessionState
    ) -> PowerPointActionResult:
        self.verify_source()
        self.binding.slide_id(info)
        if self.last_annotation:
            info = automation.execute(PowerPointAction("undo_annotation", info)).info
        index = next(
            (i for i, s in enumerate(self.binding.slides) if s.slide_id == state.active_slide_id),
            None,
        )
        if index is None:
            raise PowerPointError("The previous view belongs to another source.")
        info = automation.execute(
            PowerPointAction("go_to_slide", info, slide_number=index + 1)
        ).info
        result = automation.execute(PowerPointAction("reset_zoom", info))
        self.focus = Target("slide", state.active_slide_id)
        self.last_annotation = False
        if state.focus_view != "slide" and state.last_accepted_action_id is not None:
            command = self.saved_command(state.last_accepted_action_id, result.info)
            result = self.execute(automation, command, identifier=state.last_accepted_action_id)
        return result
