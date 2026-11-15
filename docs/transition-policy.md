# Autonomous transition policy (#21)

## Current implementation and dependencies

The desktop policy connects to the PDF player and saved Windows PowerPoint sessions,
owner-bound eligibility checks and versioned session persistence. It defaults off.
PowerPoint uses the exact saved source hash, native slide map and validated Office
runtime; local unbound slideshows cannot enable automation. Native commands recheck
source bytes and cancellation before dispatch. Manual input invalidates pending speech,
and undo restores the previous native slide/view through the same adapter.

Synthetic tests cover native admission, completion, persistence, undo and changed
source/partial/manual/disconnect rejection with Office substituted at its boundary.
They do not establish actual Office behavior, animation timing or real-world accuracy.
No permissioned recordings, measured production calibration or independent holdout
results are supplied. Those acceptance requirements remain open; use
[evaluation recording preparation](evaluation-recordings.md) to collect the inputs.

## Presenter controls

1. Open a saved PDF or supported saved PowerPoint session and enable **Live suggestions** with its existing cloud
   processing disclosure. Start the microphone and wait for the session to sync.
2. Explicitly enable **Automatic transitions**. Only unambiguous completed next/previous
   commands from the production deterministic navigation path work without calibration.
   Quoted, conditional and negated commands remain on the model path; they do not gain
   navigation permission from a keyword.
3. For model-driven moves, first load a current consented calibration for the presentation format with
   **Load calibration…**, then enable automation again. No profile is bundled or loaded
   implicitly. Without one the policy keeps the view and explains why.
4. Manual controls take priority and clear pending evidence. Manual changes impose a
   five-second hold. **Undo automatic move** disables automation and restores the previous
   slide/focus through the existing player, then saves that observed state. Manual changes,
   new sessions and source changes invalidate undo; an old undo cannot move another deck.

Failures stop automatic movement. Re-enable only after the session is synchronized.
Leaving Present, sign-out, disabling suggestions or changing the session turns automation
off. A saved `autonomy_mode` never opts a new desktop instance in. Regular keyboard,
fullscreen, reset and recovery controls remain available. No new animation is added;
PDF changes use the existing immediate player commits and respect reduced-motion settings.

## Deterministic admission and execution

`buddy_desktop.transition_policy` is independent of Qt, provider calls and execution.
It retains target identity, bounded decision identifiers and evidence sequence watermarks,
not speech text/history. `Settings` controls persistence, confirmations, cooldown,
manual hold and hysteresis without modifying the decision provider.

- Check the current session, analysis, immutable source, state version, stream, context
  epoch, language, completed revision and five-second capture freshness.
- Reject partials, silence, no-action, missing/latest-evidence mismatches, unknown targets,
  current focus and busy/disconnected players. No-action or contradictory predictions
  clear pending persistence; replaying the same completed evidence cannot confirm itself.
  The request sequence is the maximum revision watermark across the completed window,
  which can differ from the chronologically latest utterance's sequence. Advancing an
  earlier utterance's revision never counts as a new latest-utterance confirmation.
- Model proposals need a format-matched, unexpired calibration, finite confidence over
  its measured threshold, at least 700 ms of completed speech and more than a brief keyword.
  Two distinct completed evidence revisions must agree for at least 800 ms, with no gap
  longer than four seconds. Switching an existing regional focus needs three confirmations
  and a confidence threshold 0.05 higher. These are conservative configurable heuristics,
  not a language-independent proof that a sentence is semantically complete.
- A completed production navigation command can take one adjacent step without a model
  probability threshold. Every branch still checks freshness, bounds, cooldown and opt-in.
- Recheck the target through authenticated
  `POST /api/v1/sessions/{id}/transition-check`. The server uses current ownership, active
  autonomous session state, exact analysis/source lineage and canonical eligibility/geometry.
  Stale, foreign, invented and newly unusable targets deny admission. This endpoint makes
  no model calls, reserves no transition, mutates no session and reports no applied action.
- Recheck on the GUI thread immediately before dispatch and again before an asynchronous
  image load commits. Partials, manual changes, remote session updates, expiry and disconnect
  discard the old load without moving the committed view. Only a real successful player
  completion records the accepted action and queues the existing versioned session write.
  Saving failures leave the existing retry/conflict recovery available; no unsaved state
  can trigger another policy move.

Regional actions currently execute on the current slide only; off-slide regions abstain.
This preserves the existing PDF executor's source contract rather than navigating and
applying a second action against an obsolete speech snapshot. A later suggestion may
focus a region after the slide change and new speech. Accepted regional IDs are validated
again by the existing session write; navigation/reset do not invent analysis action IDs.

Preflight plus local guards cannot atomically lock a physical display against a concurrent
controller whose update has not arrived. Existing ordered events and compare-and-swap
session writes detect/reconcile that conflict. No distributed execution lease is claimed.

## Calibration and independent evaluation

Prepare permissioned, independently labelled **development** observations. Include only
actionable model proposals; abstentions and deterministic navigation must not inflate the
model-action precision counts. `correct` means the complete target matches an independent
reference, not that the provider or executor reported success:

```json
{
  "protocol": "transition-calibration-input.v1",
  "split": "development",
  "model": "command-a-03-2025",
  "source_format": "pdf",
  "permission": {
    "kind": "consented",
    "reference": "restricted-consent-record-id",
    "expires": "2026-12-01"
  },
  "samples": [
    {"id": "independent-observation-id", "confidence": 0.94, "correct": true}
  ]
}
```

The example is a schema illustration, not a usable calibration. Choose the threshold on
development data, then freeze the output **before** independent holdout evaluation:

```sh
python -m buddy_desktop.transition_calibration policy-artifacts/development.json --threshold 0.90 --output policy-artifacts/policy-profile-v1.json
```

The command refuses holdout input, malformed/duplicate observations and overwrites. It
binds exact input bytes, format, model and recording permission and writes aggregate counts
only. Live admission requires at least 100 observations above the threshold and a 95%
Wilson lower precision bound of at least 0.95. The default threshold is not an accuracy
claim. Synthetic profiles are permitted for tests but rejected for live model admission.
The model's score is only a ranking variable; the lower bound comes from reference labels.
Wilson bounds assume independent representative observations. Do not inflate counts with
overlapping windows or repeated evidence; use session-separated development and holdout
recordings, review per-session error clustering and evaluate closed-loop false transitions,
unnecessary animations, target accuracy and latency separately through #25. Do not select
thresholds on holdout failures. Changed models, prompts, retrieval, eligibility or policy
settings require a new development calibration and independent holdout run; this version
does not attest or automatically detect every upstream prompt change.

Keep input recordings, profiles and output outside Git; `policy-artifacts/` is ignored.
Follow #25's consent, cloud
processing, restricted access, withdrawal and retention rules. Consent references and
`consented` flags are operator assertions; they are not proof of consent. Expiration blocks
use but does not delete source recordings or backups. No paid calls or recording collection
are implicit in calibration or normal CI.

## Verification and capture boundary

```sh
python -m pytest frontend/tests/test_transition_policy.py frontend/tests/test_transition_player.py
python -m pytest backend/tests/api/test_transition_policy.py
```

The API test requires the disposable PostgreSQL setup. Desktop tests run the actual
policy and PDF executor, substituting only microphone/network boundaries, and verify undo,
completed-state persistence, partial-during-load, sign-out, disconnect and manual control.
They are synthetic correctness tests, not permissioned real-world release acceptance.

`PresentWidget.transition_observed` emits policy version, decision/target identity,
monotonic player completion, observed animation flag and calibration digest only after
successful execution. #25 can align this with its captured proposal/frame clock. PDF
immediate commits report `animated=false`; repeated/no-op frames emit no completion.
No transcript, consent document, credential or free-form provider explanation is logged.
Trace recording/export is explicit evaluation work and must follow recording permission.

Native Office acceptance must still separately verify the Windows interactive runtime, exact
PPTX source/native slide identities, Office version and eligible typed actions on an interactive Office installation. Passing PDF or format-agnostic rule tests cannot stand in for it.
