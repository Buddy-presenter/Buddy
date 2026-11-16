# Isolated desktop preflight (#48)

The trusted local host uses `buddy_desktop.preflight.PreflightRunner` to inspect an
immutable, already-processed deck snapshot. It launches a fresh native desktop process
for each bounded plan. PDF uses the production `SlideExecutor`, PDFium and `SlideSurface`;
PowerPoint uses the production `PowerPointAutomation`, generated-action contract and
native audience capture. This extends #49's execution foundation; it does not duplicate
its independent per-action pixel/geometry verdict or implement #50's visual QA agent.

## Host and agent boundary

Create the runner inside an authenticated deck-owner boundary, with the verified owner
principal. Fetch that owner's exact source revision and saved action catalog there.
The host supplies source bytes (never a source URL), deck ID, revision, slide map, catalog,
display profile and explicit viewport. No API account client, production session, credentials
or network tool is installed in the child. The child environment drops AWS, database,
Buddy API, auth and provider variables and isolates HOME, config, cache and temporary paths.
This is a local callable, not a new public backend route. The host must not expose source,
profile or catalog configuration as model tools. Work is synchronous; use a worker thread.

`Tool` is the typed agent-facing interface. A plan accepts only:

| Tool | Input | Behavior |
| --- | --- | --- |
| `load_slide` | `slide_id` | Load an allowlisted slide and reset to full slide. |
| `reset` | none | Clear the current camera/annotations through the production executor. |
| `apply_action` | `action_id` | Reset to the catalog action's source slide, then apply exactly that action. |
| `read_state` | none | Record current slide and view state. |
| `capture` | none | Capture the settled real audience viewport. |

Extra fields, unknown IDs and arbitrary operations are rejected before launch. Agents
cannot provide action JSON, shell commands, filesystem paths, script, URLs, HWNDs or
credentials. A new plan starts a new process; replay the bounded plan to continue an
inspection. Every action gets a deterministic clean source-slide baseline, even after
an earlier zoom/highlight. The trace records before, baseline and after states.

PDF slide IDs must match the digest/revision-derived production manifest. PDF actions use
the existing v1/v1.1 schema, source pixels and render ID. PowerPoint actions use the separate
`powerpoint-2` schema and native slide metadata: `{number, native_id, width, height}` with
one-based slide number and dimensions in Office points. IDs and dimensions are checked
against the opened native source; PDF coordinates never establish PowerPoint support.

## Native display requirements

PDF needs interactive Windows or macOS with Qt 6.12/PDFium 5.14. Native `QScreen.grabWindow`
captures the actual slide window after layout/render completion. A widget grab is used
only to detect occlusion/capture mismatch, never substituted as evidence. Requested logical
viewport and display scale are pinned and recorded. Unsupported DPR, unavailable desktop
capture, occlusion or macOS screen-recording denial stays `needs-review`.

PowerPoint needs interactive Windows, registered desktop Microsoft PowerPoint/Office,
pywin32/comtypes, and a dedicated test OS account/display with no running PowerPoint.
It starts a fresh Office instance, opens only a private source copy read-only, disables
macros, rejects embedded automation/external relationships, and verifies exact owned
Office PID and audience window identity. It never connects to an existing slideshow.
Production zoom requires fullscreen on the primary display; the host must explicitly
request that display's physical viewport. Mismatch fails closed. Office version/build,
Windows runtime and actual viewport are recorded. macOS PowerPoint is unsupported.
Native window hit tests and a source-render comparison reject obstructing dialogs/apps;
the source export is an occlusion guard only and never replaces desktop evidence. Office
activation/licensing prompts therefore make the case unsupported until the operator
restores a licensed runtime and repeats the smoke. Do not dismiss account/security
prompts automatically or count an obstructed capture as acceptance.
This is process/resource isolation, not a sandbox for hostile native documents; run the
Office host in a dedicated test account, not alongside personal documents/apps.

## Results, evidence and limits

Versioned `buddy.preflight.v1` reports pin source hash/revision, runner/renderer version,
OS, Qt or Office version, viewport/scale, tool traces and SHA-256 for captured PNGs.
`completed` means the requested inspection tools executed; **it does not approve the action**.
`semantic_verified` always remains false. Failures/cancellation/unsupported runtime return
`needs-review` and a stable reason. A retry creates a fresh run/process; no failed session
state is reused. #50 must inspect these captures and create its own visual verdict.

Limits: 64-MiB source, 1-MiB job JSON, 500 slides, 1,000 saved catalog actions, 16 tools,
four captures, 16 MiB per capture/64 MiB per run, 256-MiB scratch, 90-second process deadline,
and 1,536-MiB process-tree memory. Windows uses a kill-on-close Job Object including the
exact owned Office process; POSIX kills the isolated process group. Cancellation stops
the owned process tree. No operation discovers a real presentation to act upon.

Artifacts are OS-account private (Windows protected owner DACL; POSIX mode 0700), with
owner checks and opaque run IDs at read time. The runner retains at most eight runs for
one hour, expiring them on access/new runs. Source/job/profile data is removed immediately
after each run; closing the runner removes all evidence. Use it as a context manager.
Do not publish raw private-deck screenshots in public CI artifacts. The trusted owner host
may copy approved evidence to its existing owner-scoped storage with its retention policy.

## Verification and operation

```sh
python -m pytest frontend/tests/test_preflight.py -q
python tests/integration/smoke_preflight.py --output replay-artifacts/preflight-pdf
# Dedicated Windows/Office display, with its actual physical width/height:
python tests/integration/smoke_preflight.py --output replay-artifacts/preflight-office --office-viewport 1920 1080
```

The opt-in smoke uses synthetic repository fixtures and writes separate PDF/PPTX reports
and real native captures under a private output directory. It fails if a requested case
is unsupported; skipping Office never counts as Office acceptance. Normal Windows/macOS
frontend CI exercises real PDF child execution, baseline/reset pixels, native capture,
authorization, cancellation/retry, invalid tools, safe fallback and cleanup. Managed macOS
hosts without native capture permissions or a verifiable unobstructed audience explicitly
skip the capture acceptance case after asserting the fail-closed result;
run that case on an authorized native Mac before claiming its capture profile is supported.

This adds a desktop library/worker, not a server feature switch. Rebuild/install the desktop
wheel on the dedicated runner host for changes to take effect. Committing or deploying the
API alone does not install this worker; no staging infrastructure/migration is required.
