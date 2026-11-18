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
The response must contain one structured verdict. Malformed, conflicting or
low-confidence verdicts stay in needs-review. A successful action with
identical before/after captures is rejected without a provider call. An image
above the Bedrock input size limit is retained for inspection and stays in
needs-review with `visual_image_limit`.

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
target and rerun in a new output directory. A rejected or uncertain verdict
returns exit code 1 and retains the report and available captures. For
PowerPoint zoom, generate another snapshot directory with
`--office-action zoom_region` and use action ID `test-zoom_region`.

Run automated checks with
`python -m pytest frontend/tests/test_preflight.py frontend/tests/test_preflight_sampling.py -q`.
These tests can substitute the external model response while retaining real
native PDF capture. Unsupported or obstructed desktops cannot yield an approved
verdict.
