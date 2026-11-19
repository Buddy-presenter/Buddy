# Visual QA of saved slide actions (#50)

The visual QA host runs a saved action through the same isolated tools as
[preflight](preflight-runner.md). It loads the action's source slide, captures
the full slide, applies the catalog action, reads its resulting state and
captures the audience viewport again. Shape and camera parameters are checked
by the format-specific player before the vision model sees the images.
PDF uses Buddy's Qt/PDFium player. PPTX uses desktop PowerPoint on Windows.

The host verifies that both captures belong to the exact action and slide.
The independent vision call receives the two images, the action type and the
intended purpose. It cannot invoke the runner or modify the saved action.
The response must contain one structured verdict and a visual observation.
The model is asked for one or two short sentences. The host accepts up to 1,200
characters so a useful detailed explanation does not discard an otherwise valid
verdict; empty, oversized or malformed responses still remain unavailable.
Completed visual checks return validated or rejected. Technical failures and
malformed or conflicting responses return unavailable.
Visual judgment uses a functional standard: target text locates the intended
entity rather than defining an exact coverage mask. Useful emphasis may include
extra words, a card's identifying heading, a connector to the same unambiguous
card, or zoomed surrounding context. These are not failures on their own.
Missing an explicitly requested entity/comparison, emphasizing a different
entity, obscuring essential content, or producing no useful effect still fails.
Confidence is recorded alongside the original model verdict for inspection. A successful action with
identical before/after captures is rejected without a provider call. An image
above the Bedrock input size limit is retained for inspection and stays
unavailable with `visual_image_limit`.

Each run writes a private, versioned report and, when available, its exact PNG
evidence. The report binds the source revision/hash, action hash, slide map,
renderer, display profile and evidence hashes. A retry uses a fresh directory
and isolated player. Treat reports as private deck data and retain them only in
the owner's approved storage. The CLI is an operator entry point: the trusted
host supplies an already authorized saved snapshot and owns the output
directory. Use the owner-scoped runner to retrieve transient evidence.

## Run a saved action from Buddy

In the source development build, sign in to Buddy and go to **Library → Review**.
Select a slide and one of its generated actions, then click **Run visual QA**.
Buddy uses its authenticated Review download to prepare the exact source and action
automatically. The result and private report path appear above the action list.
Close unrelated PowerPoint presentations before testing PPTX. Buddy closes its own
Review preview before starting the isolated Office player.

Choose **Run visual QA on all actions** to check the full deck, including saved
actions that Review marks unavailable. Each action gets its own report. The
status shows progress and totals. **Stop visual QA** lets the current check finish
and leaves the remaining actions unchecked.
During a batch, **V** counts validated actions and **R** counts rejected actions.
Unavailable checks and execution failures have separate counters. Counts update
after each completed action, including checks from other slides in the deck.

The source build runs the local operator using the configured AWS profile, or
`buddy-staging-tools` by default. The development virtual environment must have
the backend development dependencies and an active AWS login for the operator.
The packaged app needs a deployed QA service for end-user operation.
Correction and revalidation orchestration is tracked by #51. Publishing reports
in Review and gating Live tools is tracked by #53.

## Run synthetic PDF and PowerPoint cases on Windows

Use a virtual environment with both backend and desktop development
dependencies installed. Sign in to the approved Bedrock AWS profile and use a
vision model that supports tool use. Private screenshots are sent to the
configured Bedrock region/model. Use synthetic fixtures for a first test.
Save and close existing PowerPoint decks and use an idle interactive desktop.
Use the physical pixel dimensions of the primary display for Office and
choose the matching 1x or 2x PDF display profile. Office uses physical capture
with the runner’s `wide-1x` profile independently of PDF scaling.

```powershell
$env:AWS_PROFILE = "buddy-staging-tools"
$python = ".\.venv\Scripts\python.exe"
$qaFixtures = "build/visual-qa-inputs"
$qaProfile = "wide-2x"
$qaWidth = 2560
$qaHeight = 1600
& $python tests/integration/smoke_preflight.py --output $qaFixtures --qa-snapshots `
  --profile $qaProfile --office-viewport $qaWidth $qaHeight
$qaModel = "eu.anthropic.claude-sonnet-4-6"
& $python scripts/visual_qa.py --snapshot "$qaFixtures/pdf-snapshot.json" `
  --source "$qaFixtures/source.pdf" --action test-zoom --output build/visual-qa-pdf `
  --region eu-central-1 --model $qaModel
& $python scripts/visual_qa.py --snapshot "$qaFixtures/pptx-snapshot.json" `
  --source "$qaFixtures/source.pptx" --action test-highlight_region `
  --output build/visual-qa-pptx --region eu-central-1 --model $qaModel
```

Open each output's **before.png**, **after.png** and **report.json**. The
before image shows a full slide and the after image shows the action from the
audience's view. Compare the verdict with what you see in both images. To test
rejection, change the synthetic action's purpose to name a different visible
target and rerun in a new output directory. A rejected or unavailable result
returns exit code 1 and retains the report and available captures. For
PowerPoint zoom, generate another snapshot directory with
`--office-action zoom_region` and use action ID `test-zoom_region`.

Run automated checks with
`python -m pytest frontend/tests/test_preflight.py frontend/tests/test_preflight_sampling.py -q`.
These tests can substitute the external model response while retaining real
native PDF capture. Unsupported or obstructed desktops cannot yield an approved
verdict.



## Bounded correction and revalidation (#51)

In Review, enable **Try bounded corrections**, then run visual QA on the selected
action or the whole deck. The same operator can run from PowerShell with
`--correct`. A valid first attempt finishes immediately. On rejection, the model
may propose a new rectangle using the source coordinate system.

The host resolves the original target against measured PDF source objects or
native PPTX shape metadata. Only the region can change. Action type, instance ID,
source slide, render identity, target text and shape identity are fixed. Proposals
are clamped to the target with at most 15 percent padding, retain at least 80
percent of its source area, and pass the existing format-specific geometry/camera
checks. The corrected action then runs in a fresh isolated player and gets a new
independent visual verdict. There are at most two corrections (three executions).
Repeated/unchanged, out-of-bounds or unsupported proposals stop safely.

Native PowerPoint text annotations and zoom are pinned by the existing executor
to measured text/complete source shapes. Those source bindings are preserved.
When adjusting the raw region cannot change the actual target, the report records
`correction_outside_supported_bounds`; the host does not remove the binding.
Coordinate-bound native annotations can be corrected. Ambiguous source objects,
rotated PDFs and grouped/rotated native shapes without a supported source mapping
remain unavailable for automatic correction. A validated original can still pass
without a correction mapping.

The top-level `report.json` contains `history`, original/final hashes and the final
verdict. Every `attempt-N` directory retains the executed parameters, independent
verdict and exact before/after screenshots. Only a validated final attempt has a
`corrected_action`; failed/rejected attempts never provide an eligible action.
Live eligibility publication and server-side persistence remain owned by #53.

### Manual test

1. Save and close unrelated PowerPoint windows, open Buddy and sign in.
2. Go to **Library → Review**, select an analyzed PDF or PowerPoint.
3. Select a rejected action with an incorrectly sized region. Enable **Try bounded
   corrections** and click **Run visual QA**. Leave the native player unobstructed.
4. Open the report path displayed in Buddy. Inspect `history`: version 0 is the
   saved action, each later version is the corrected region. Inspect both images
   under each attempt and verify the final result matches the intended target.
5. Test an already correct action. It should finish with one attempt.
6. Test an unsupported correction or a persistently incorrect action. It must stop
   at the bound and keep `corrected_action` null. Manual presentation remains usable.
7. Repeat with the other source format. PDF evidence does not establish Office support.

The 2026-11-18 Windows acceptance used synthetic PDF and PPTX title highlights that
initially covered half of their headings. Real Bedrock Sonnet 4.6 proposed full
source-bound regions. Both players replayed the corrections and the independent
visual verdict accepted the full headings. Windows Office was 16.0.20527; PDF used
Qt/PDFium with a 2x logical profile; Office captured a 2560×1600 physical display.
A uniform orange-box highlight case remained rejected after correction because the
vision model did not consider its subtle tint sufficient emphasis. It stopped safely.
Native macOS correction capture has not been verified.
