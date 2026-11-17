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
results are supplied. The requester has authorized scripted app verification without human recordings.
Independent calibration/holdout claims remain out of scope for this delivery; use
[evaluation recording preparation](evaluation-recordings.md) to collect the inputs.

## Presenter controls

1. Open a saved PDF or supported saved PowerPoint session, enable **Live suggestions**
   with its existing cloud-processing disclosure, and start the microphone.
2. Explicitly enable **Automatic transitions**. Clear next/previous/numbered-slide
   commands use the deterministic navigation path. A stable interim phrase or completed
   speech can execute the single eligible action selected by the live agent, including a current-slide
   highlight, zoom, or reset. Ambiguous speech and a `no_action` decision leave the view
   unchanged.
3. The option is a presenter opt-in, not a calibration workflow. No local calibration
   file, profile or repeated confirmation utterance is required. Calibration artifacts
   remain available for offline evaluation only and cannot grant runtime permissions.
4. Manual controls take priority and invalidate pending speech. Manual changes impose a
   five-second hold. **Undo automatic move** disables automation and restores the previous
   slide/focus through the existing player, then saves that observed state. Manual changes,
   new sessions and source changes invalidate undo; an old undo cannot move another deck.

Recoverable proposal, preflight and execution failures leave the presenter's opt-in on;
they report the failure and wait for fresh speech. Actions still cannot run while the
session is stale, disconnected, busy, changed or ineligible. Leaving Present, sign-out,
disabling Live suggestions or changing to a genuinely new session turns automation off.
A saved `autonomy_mode` never opts a new desktop instance in. Regular keyboard,
fullscreen, reset and recovery controls remain available. No new animation is added;
PDF changes use the existing immediate player commits and respect reduced-motion settings.

## Deterministic admission and execution

`buddy_desktop.transition_policy` is independent of Qt, provider calls and execution.
It retains bounded completed-evidence identities, not speech text/history. `Settings`
controls post-action cooldown and manual hold without modifying the decision provider.

- Check the current session, analysis, immutable source, local active slide, stream, context
  epoch, language, submitted evidence and five-second capture freshness.
- Interim speech is eligible only through its stable whole-token prefix, corroborated by
  consecutive recognition hypotheses. A result remains current while that prefix remains
  intact and the presenter continues speaking; an explicit correction/negation, manual
  change or session change supersedes it. Unknown targets, current focus and
  disconnected or changed presentations are rejected. A suggestion that encounters a
  a local slide change while a result is in flight rejects that result. Navigation does
  not wait for saved-position synchronization; the server checks the request's local
  slide ID against the pinned analysis. Regional actions retain their session-state
  checks. Changed or expired input is discarded. Each submitted speech watermark can trigger at most once, even if
  duplicate HTTP decisions arrive. There is no multi-utterance confirmation queue and no
  locally loaded user profile.
- Model-selected actions require a finite provider score, evidence in the submitted
  fresh decision window and an exact match in the currently eligible target set. That score is only a
  provider ranking value; it is not represented as measured real-world accuracy. A model
  can still select the wrong eligible action, so use the explicit opt-in and evaluate
  closed-loop target accuracy and false transitions separately.
- Deterministic next/previous and absolute slide-number commands bypass semantic retrieval
  and model selection. Every branch still checks freshness, target bounds, deduplication,
  session/source identity and opt-in. Completed slide-navigation moves skip the generic
  post-action cooldown so a fresh next command can execute immediately.
- Recheck the target through authenticated
  `POST /api/v1/sessions/{id}/transition-check`. The server uses current ownership, active
  autonomous session state, exact analysis/source lineage and canonical eligibility/geometry.
  Stale, foreign, invented and newly unusable targets deny admission. This endpoint makes
  no model calls, reserves no transition, mutates no session and reports no applied action.
- Recheck on the GUI thread immediately before dispatch and again before an asynchronous
  image load commits. Partials, manual changes, remote session updates, expiry and disconnect
  discard the old load without moving the committed view. Only a real successful player
  completion records the accepted action. Regional actions continue to queue a versioned
  session write; automatic slide navigation keeps its position locally and does not update
  the saved server position. Closing and reopening can therefore restore the last manually
  saved slide, which is accepted for this live-presentation behavior.
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
only. Offline reports may compute a threshold and Wilson precision bound, but those
artifacts do not gate or enable live automatic actions. Synthetic profiles remain test
fixtures only and are never evidence of production accuracy.
The model's score is only a ranking variable; the lower bound comes from reference labels.
Wilson bounds assume independent representative observations. Do not inflate counts with
overlapping windows or repeated evidence; use session-separated development and holdout
recordings, review per-session error clustering and evaluate closed-loop false transitions,
unnecessary animations, target accuracy and latency separately through #25. Do not select
thresholds on holdout failures. Changed models, prompts, retrieval, eligibility or policy
settings require a new development calibration and independent holdout run; this version
does not attest or automatically detect every upstream prompt change. Those evaluation
requirements do not impose a per-user setup step in the desktop app.

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
monotonic player completion, observed animation flag and a null calibration digest only
after successful execution. #25 can align this with its captured proposal/frame clock. PDF
immediate commits report `animated=false`; repeated/no-op frames emit no completion.
No transcript, consent document, credential or free-form provider explanation is logged.
Trace recording/export is explicit evaluation work and must follow recording permission.

Native Office acceptance must still separately verify the Windows interactive runtime, exact
PPTX source/native slide identities, Office version and eligible typed actions on an interactive Office installation. Passing PDF or format-agnostic rule tests cannot stand in for it.


## Scripted live-player verification

`frontend/scripts/smoke_transition_policy.py` generates its own PDF/PPTX and uses the
actual app policy, PDF rendering and Office execution/undo. It records measured completion
latency and player events, with speech, eligibility HTTP and versioned session transport
explicitly substituted locally. A live run
also guards the native preview/undo race: undo retains its saved state and queues until
Office work becomes idle. Manual intent invalidates a queued undo.

Use `docs/evaluation-recordings.md` for the command and provenance rules. No independent
real-world model accuracy or Office animation-duration claim follows from these checks.


Measured generated-input sample (2026-11-15, Office 16.0.20527): PDF navigation/undo
239/291 ms; PPTX navigation/undo 279/979 ms. Each is one local sample, not P95 or an
accuracy benchmark. Both formats suppressed the duplicate decision and saved the
observed state to the file-backed test transport. Real Office all-tool navigation,
annotations, erase, reconnect and unchanged-source checks also passed on generated
960x540 and 720x540 decks. Aggregate report:
[scripted policy results](verification/scripted-policy-2026-11-15.json).
