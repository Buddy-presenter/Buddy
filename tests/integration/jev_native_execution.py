"""Permissioned execution through production isolated PDF/Office preflight players.

Execution timings deliberately include a cold isolated player start. They are not warm
interactive-window timings. The source, allowed tools, capture hashes and native state
are checked by the same production runner used for action validation.
"""

import hashlib
import time
from pathlib import Path

from buddy_desktop.preflight import PreflightRunner


class NativeExecution:
    def __init__(self, app, fixture, kind, folder, profile, viewport):
        self.document = fixture["document"]
        self.kind, self.folder = kind, Path(folder)
        self.folder.mkdir(parents=True, exist_ok=False)
        self.source = fixture["path"]
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.slides = [s.slide_id for s in self.document.slides]
        self.actions = {
            a.instance_id: a.model_dump(mode="json")
            for s in self.document.slides
            for a in s.actions
        }
        manifest = self.document.manifest.model_dump(mode="json")
        self.snapshot = dict(
            source=self.source.read_bytes(),
            source_format=kind,
            deck_id=str(fixture["deck"]),
            revision=self.document.source_revision,
            profile=profile,
            viewport=viewport,
            slides={
                s["slide_id"]: dict(
                    number=i + 1,
                    native_id=s["native_slide_id"],
                    width=s["source"]["width"],
                    height=s["source"]["height"],
                )
                if kind == "pptx"
                else {}
                for i, s in enumerate(manifest["slides"])
            },
        )
        self.owner = "synthetic-native-" + str(fixture["deck"])
        self.runner = PreflightRunner(self.owner)

    def prepare(self, case):
        self.case = case

    def execute(self, client, session, body, proposal, label):
        target = proposal.get("target", {})
        tool = target.get("tool")
        started = time.perf_counter()
        checked_ms = 0
        if tool != "no_action":
            checked_at = time.perf_counter()
            approval = client.post(
                f"/api/v1/sessions/{session['id']}/transition-check",
                json=dict(
                    expected_state_version=session["state_version"],
                    analysis_id=session["analysis_version_id"],
                    active_slide_id=body["active_slide_id"],
                    focus_view=body["focus_view"],
                    focus_region=body["focus_region"],
                    target=target,
                ),
            )
            checked_ms = (time.perf_counter() - checked_at) * 1000
            if approval.status_code != 200:
                return dict(
                    completed=False, error="transition_check_rejected", transition_ms=checked_ms
                )
        tools = [dict(op="load_slide", slide_id=self.slides[self.case["current"]])]
        if self.case["focus"] != "slide":
            first = self.document.slides[self.case["current"]].actions[0].instance_id
            tools.append(dict(op="apply_action", action_id=first))
        tools.append(dict(op="capture"))
        before_index = len(tools) - 1
        if tool in {"zoom_region", "highlight_region"}:
            tools.append(dict(op="apply_action", action_id=target["action_id"]))
        elif tool in {"go_to_slide", "next_slide", "previous_slide"}:
            tools.append(dict(op="load_slide", slide_id=target["slide_id"]))
        elif tool == "reset_view":
            tools.append(dict(op="reset"))
        elif tool == "no_action":
            tools.append(dict(op="read_state"))
        else:
            raise ValueError("Unrecognized executable target")
        tools.append(dict(op="capture"))
        executing = time.perf_counter()
        run = self.runner.run(self.owner, **self.snapshot, catalog=self.actions, tools=tools)
        execution_ms = (time.perf_counter() - executing) * 1000
        if run["state"] != "completed":
            return dict(
                completed=False,
                error=run["reason"],
                transition_ms=checked_ms,
                execution_ms=execution_ms,
            )
        before, after = run["trace"][before_index]["after"], run["trace"][-1]["after"]
        pictures = [self.runner.artifact(self.owner, run["run_id"], f"{i}.png") for i in range(2)]
        completed = after["slide_id"] == target.get("slide_id")
        if tool == "no_action":
            completed = completed and after == before and pictures[0] == pictures[1]
        elif tool == "zoom_region":
            completed = completed and (
                after.get("zoom") is not None
                if self.kind == "pptx"
                else after["region"] != before["region"]
            )
        elif tool == "highlight_region":
            completed = completed and (
                after["managed_shapes"] > before["managed_shapes"]
                if self.kind == "pptx"
                else after.get("highlight") is not None
            )
        elif tool == "reset_view":
            completed = completed and (
                after.get("zoom") is None
                if self.kind == "pptx"
                else after.get("highlight") is None and after["region"] != before["region"]
            )
        for role, data in zip(("before", "after"), pictures, strict=True):
            (self.folder / (label + "-" + role + ".png")).write_bytes(data)
        if hashlib.sha256(self.source.read_bytes()).hexdigest() != self.source_hash or not run.get(
            "source_unchanged"
        ):
            raise AssertionError("Original source changed")
        return dict(
            completed=bool(completed),
            transition_ms=checked_ms,
            execution_ms=execution_ms,
            dispatch_to_visible_ms=(time.perf_counter() - started) * 1000,
            before=before,
            after=after,
            includes_startup=True,
        )

    def close(self):
        self.runner.close()
