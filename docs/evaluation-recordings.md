# Evaluation inputs and scripted app measurements

The requester has authorized proceeding without human recordings. Scripted generated-input
app measurements are the current validation path. Independent real-world calibration and
holdout accuracy remain unmeasured; they do not block this authorized development work.
The private recording preparation below is optional future evaluation work.

Run the opt-in local measurement on interactive Windows with installed PowerPoint:

```powershell
python frontend/scripts/smoke_transition_policy.py --output replay-artifacts/scripted-live-new
```

It generates a PDF and PPTX, drives the actual PDF renderer/player and native Office
policy adapter, checks completion, duplicate suppression and undo, and saves aggregate
timings, player events and file-backed test states locally. No microphone is recorded
and no provider is called. Eligibility HTTP and session transport are explicit local test
substitutes; this does not replace the PostgreSQL/API integration checks in CI.
Office animation timing remains unknown and is not reported as zero.

Reports must retain `scope: scripted_live_players`, generated input provenance, substituted
boundaries, and `independent_holdout: false`. Never mark them as consented human recordings
or production model calibration. Use a new output directory for every run.


Put private inputs in `tests/fixtures/replay/private/` (ignored by Git). Never commit
recordings, transcripts, deck contents, consent documents or calibrated profiles.
Use `development/` and `holdout/` subdirectories. Split entire recording sessions,
not frames from one recording, to prevent leakage. Keep holdout labels independent
of provider predictions and freeze them before tuning development thresholds.

For each recording, provide:

- The original PDF or PPTX and its saved Buddy analysis export. Keep original source
  bytes and native slide identities; PDF analysis cannot stand in for PowerPoint.
- A timestamped transcript with completed utterances, their start/end times and
  speaker corrections. Include hesitation, digression, returns, silence, ambiguity
  and recognition errors. A screen recording helps label actual transitions.
- Independently labelled expected actions: slide identifier, highlight/zoom region
  when relevant, and times where Buddy must stay still. Record actual player actions
  separately, including animation timing; never infer execution from a proposal.
- A permission note naming the session, consent reference, expiration/deletion date,
  allowed uses and whether cloud provider processing is permitted. Permission is
  required from every recorded participant and for the deck content.

A convenient initial folder layout (these files are preparation inputs, not yet a
validated replay corpus):

```text
private/development/pdf-session-01/
  deck.pdf
  analysis.json
  transcript.json
  reference-actions.json
  permission.json
private/holdout/pptx-session-02/
  deck.pptx
  analysis.json
  transcript.json
  reference-actions.json
  permission.json
```

Example `transcript.json`:

```json
[{"id":"utterance-1","start_ms":0,"end_ms":1800,"final":true,
  "text":"Replace with the actual recognized utterance"}]
```

Example `reference-actions.json`:

```json
[{"at_ms":1800,"tool":"no_action","slide_id":"actual-saved-slide-id"}]
```

Example permission note:

```json
{"kind":"consented","reference":"private-consent-record-reference",
 "expires":"YYYY-MM-DD","provider_processing":false}
```

Replace every example field with real data. Do not label synthetic examples as consented
recordings. Raw audio is optional if a permissioned transcript and screen/action record
supply sufficient evidence. If audio is provided, keep it private and apply its retention
policy to all copies and backups. Expiration checks do not delete files automatically.

The replay corpus and trace schemas are documented in `presentation-replay.md` on the
replay branch. They bind reports to exact corpus/gate hashes. Development calibration
uses `transition-calibration-input.v1` with format/model identity, permission and samples
of `{id, confidence, correct}`. Labels must score the complete action target against an
independent reference. Build calibration on development only; freeze the profile before
running held-out policy/player evaluation. Report PDF and PowerPoint separately.

Until these inputs and actual interactive Windows/Office execution evidence exist,
unit tests and synthetic CI runs establish code behavior only, not release acceptance.
