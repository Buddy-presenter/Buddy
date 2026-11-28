# Reproduce the evaluation and demo

Start from the repository root. Baseline:
`61a326e839fdfe701ad265b3f5a95c23e8aac550`; the package branch adds documentation and
aggregate results. Use the final PR commit to obtain the package itself; the baseline
alone predates these files. Record `git rev-parse HEAD` for every new run.

## 1. Credential-free evaluation

Requirements: Git and Python 3.12–3.14, with network access to install the pinned project
dependencies. The evaluation itself uses no account, database, microphone, Office,
cloud key or paid provider. A fresh backend-only environment is sufficient.

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ./shared
python -m pip install -e './backend[api,dev]'
```

macOS/Linux:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e ./shared
python -m pip install -e './backend[api,dev]'
```

If PowerShell activation is restricted, invoke `.\.venv\Scripts\python.exe` instead
of `python` in each command; changing the machine's execution policy is unnecessary.
Run the existing harness, without `--live-provider` or `--release`:

```sh
python -m buddy_api.replay tests/fixtures/replay/development-v6.json --gates tests/fixtures/replay/development-v6-gates.json --output replay-artifacts/course-development-new
python -m buddy_api.replay tests/fixtures/replay/holdout-v6.json --gates tests/fixtures/replay/holdout-v6-gates.json --output replay-artifacts/course-holdout-new
python scripts/check_replay_freeze.py
python -m pytest backend/tests/evaluation/test_replay.py backend/tests/api/test_security_privacy.py backend/tests/test_observability.py -q
```

Always choose **new output directories**. The harness refuses to overwrite an existing
directory. The freeze check requires an `origin/main` Git ref; fetch main if it is absent.
For a historical checkout, compare its established fixtures against the recorded baseline
with `python scripts/check_replay_freeze.py --base-ref 61a326e839fdfe701ad265b3f5a95c23e8aac550`.

Expected baseline result: both commands return exit 0 and write `report.json` and
`trace.json`; each report has 21 frames, `passed: true`, no failures,
`provenance: synthetic_contract`, `scope: agent_proposals`, `release_gate: false`.
The focused tests pass 34 cases. Replay exits 1 for a scored gate failure and 2 for
invalid input/digest/permission/output. Do not edit frozen inputs or thresholds to make
a failure disappear. Compare new reports with the [committed aggregates](results/manifest.json).

Only aggregate `report.json` files from these public synthetic corpora are bundled here.
Do not publish traces or reports from private corpora. The synthetic 139 ms P95 and
USD 0.00224 fields are fixture data, not measured provider latency or billed spending.
The [replay guide](../presentation-replay.md) defines exact metric denominators and
nearest-rank P95. This reproduces a contract evaluation, not Jev service accuracy.

## 2. Local presentation demo

Install the desktop into the same active environment:

```sh
python -m pip install -e './frontend[dev]'
python frontend/scripts/smoke_desktop.py
python -m buddy_desktop
```

Use a graphical Windows/macOS session. For manual use, remove any prior offscreen Qt
setting. The startup smoke verifies the shell/resampler; it does not start the microphone.
The application bundles public staging defaults; private account features need actual
configured services. This local demonstration needs neither sign-in nor API access.

1. Open **Present**, then **Open rendered slides…** and select
   `frontend/tests/fixtures/slide_actions/manual-manifest.json` in this checkout.
2. Verify **Slide 1 of 4**. Click Next and Previous. Go to Slide 3 and check portrait
   proportions. At Slide 1/4, attempts to move beyond the deck boundary must stay there.
3. On Slide 4, select a visible word/formula and apply highlight. Select a larger region
   and zoom. Resize the window; the overlay should stay attached to the content.
4. Reset and confirm the full slide returns with no temporary highlight. Source files
   remain unchanged. Follow the [precise manual steps](../slide-actions-manual.md) for
   keyboard coordinates and invalid-geometry checks.

These are manual controls on repository-owned PNG fixtures, not an autonomous speech
demonstration, PDF parsing benchmark or native PowerPoint acceptance. No model selected
the highlighted region. The fixture [provenance](../../frontend/tests/fixtures/slide_actions/README.md)
permits distribution with this checkout.

## 3. Optional connected agent demo

This is a separate mode requiring real operator configuration and willing speaker
participation. Do not start capture or paid tests unattended. Follow
[development](../development.md), [database](../database.md), [accounts](../accounts.md),
[uploads](../uploads.md), [workers](../pdf-workers.md), [speech](../speech-recognition.md)
and [Jev setup](../jev-direct-current-prototype.md). Use the current source rather than
historical setup paragraphs when provider defaults disagree.

For a local API, install the backend as above, load its settings privately, apply
current migrations, and launch it in a separate terminal:

```powershell
$env:BUDDY_ENVIRONMENT = 'development'
# Supply BUDDY_DATABASE_URL and identity/provider/storage settings privately.
python -m buddy_api.persistence.migrate upgrade
python -m buddy_api
```

Set `BUDDY_API_URL=http://127.0.0.1:8000` in the desktop terminal before launching to
explicitly select that local API. `/health` must be live and `/api/v1/ready` must return
200 after migrations; a 503 readiness is a dependency/configuration failure. For a
reviewed deployed API, use its approved HTTPS endpoint and confirm its deployed revision
and enabled features with the operator. Never commit keys, database URLs, `.env` files
or token-bearing command transcripts.

Jev decisions require backend `BUDDY_AGENT_ENABLED=true` and `BUDDY_JEV_API_KEY`;
speech requires the documented backend Deepgram setup. Optional Cohere/search and
Bedrock analysis require their own configuration. Keys stay server-side. Do not pass
live credentials as CLI arguments. Source defaults do not prove staging configuration.

Upload an authorized original PDF/PPTX and wait for successful analysis. Saved regional
actions additionally require independent semantic visual verification. PDF: choose
**Present PDF** in Library. PowerPoint: **Open saved PowerPoint** in Review, then
**Present with Buddy**, on Windows with desktop Office and matching source/runtime.
Wait for the saved session; start the microphone deliberately, enable **Live suggestions**,
and then optionally enable **Automatic transitions**. Use the
[two-format voice acceptance matrix](../voice-navigation-acceptance.md) for direct
commands, contextual intent, negation, correction, manual override, undo and cleanup.
Stop capture and end the session when finished. Missing/unverified region options are
an explicit limitation, not permission to fabricate usable actions.

## 4. Optional native and paid evaluation

Interactive Windows, licensed desktop PowerPoint, and a foreground-capable unlocked
session are required for native Office. Save and close your own PowerPoint documents
before these scripts; they generate synthetic sources and close their own instance.

```powershell
python frontend/scripts/smoke_transition_policy.py --output replay-artifacts/course-policy-new
python frontend/scripts/smoke_saved_actions.py --output build/course-saved-actions-new
```

Retain the output and nonzero exit if Windows blocks focus/zoom. These scripts substitute
speech and/or eligibility/session transport as described in
[recording preparation](../evaluation-recordings.md) and [saved actions](../present-actions.md).
Do not reclassify their native execution evidence as authenticated service publication
or microphone accuracy. Office animation timing may remain unknown.

The [paired provider guide](../jev-direct-current-prototype.md#reproduce-the-paired-evaluation)
documents the opt-in command, fixture dependencies, disposable PostgreSQL database,
AWS login/profile/secret access, synthetic S3 use, native validation, paid Jev/Cohere/
Deepgram/Bedrock calls and cleanup report. No such calls were made for this package.
Use an authorized isolated environment and preserve failed attempts/`cleanup.json`.
Do not copy raw provider traces, audio or credentials into the course bundle.

For repeated end-to-end timings, collect desktop/backend content-free events as described
in [#29 diagnostics](../presentation-latency.md), grouped by PDF/PPTX and decision ID.
Report recognition separately, n, p50/p95, missed/false actions, all attempted calls and
dated billed usage/rates. Unknown cost or animation timing is `unknown`, never zero.
Real private holdout release evaluation has additional permission/capture requirements
in the [replay guide](../presentation-replay.md); this package does not pass that gate.

## 5. Share a reproducible source bundle

Record the final reviewed commit. From a checkout at that commit:

```sh
git rev-parse HEAD
git archive --format=zip --output=../Buddy-course-demo.zip HEAD
```

`git archive` exports tracked source, docs, public fixtures and committed aggregate
results at exactly that revision. It excludes ignored local keys, environments, build
outputs, private corpora and traces. It does not guarantee that a tracked secret is safe;
the repository's secret scan must pass before sharing. Dependencies still need installing,
and native Office/cloud configuration are external prerequisites. A ZIP has no `.git`
history: skip the Git-only freeze command there, or clone/check out the recorded commit
to perform it. The corpus/gate hashes in the manifest still identify the evaluated bytes.

For a new submission, save `git rev-parse HEAD`, `python --version`, OS/Office version,
commands and aggregate reports. Run [CI checks](../ci.md) appropriate to changed code;
persisted API/security tests require a disposable `_test` PostgreSQL database. Do not
claim skipped database/native/provider cases passed. An independent teammate walkthrough
is still pending and must be recorded before calling this package independently reproduced.
