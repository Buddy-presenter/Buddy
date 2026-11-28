# Course security and privacy baseline (#26)

This model covers the native desktop, authenticated API, private PDF/PPTX storage,
workers, speech relay and live decisions. It inventories existing controls and identifies
residual risks; #44 owns the shared workflow evaluation. Source and CI demonstrate
implementation, not certification of a live deployment.

## Assets and boundaries

Private assets include decks, derived text/images/actions, audio, transcripts, account
tokens, provider credentials, session state and provider budget. Clients, uploaded slide
content and external provider output are untrusted. Ownership comes from verified
identity and persisted server records, never a client-supplied owner or model decision.

| Boundary | Controls | Residual risk |
| --- | --- | --- |
| Desktop/browser to API | Cognito signature, issuer, audience, expiry and provider-state checks; persisted ownership; deployment TLS | Compromised accounts can access their own data. Local development HTTP is unsuitable for remote private data. |
| API/worker to storage | Owner/deck foreign keys, private encrypted versioned S3, pinned version/checksum, immutable action hashes | Privileged administrators/services remain trusted. Actual IAM and bucket configuration must match Terraform. |
| Upload to parser/Office | Type/size/archive bounds, isolated deadlines, independent PDF/PowerPoint catalogs and validators | Parsers and Office need patching. Linux cannot establish a real Windows/Office execution pass. |
| Service to external provider | Server-held secrets/roles, fixed destinations, bounded requests/responses and deadlines, strict choice/schema checks | Providers receive private inputs. No zero-retention or complete residency guarantee is claimed. |
| Proposal to desktop execution | Server-built eligible targets, source/session/context freshness checks, presenter-controlled modes and undo | Models can select a wrong allowed action. Confidence is not authorization or proof of intent. |

## Abuse and failure cases

Test paths below are relative to `backend/tests/` unless stated otherwise.

| Case | Implemented mitigation and regression evidence |
| --- | --- |
| Cross-account access or owner override | Authentication precedes access; owner checks and tombstones deny private reads/writes. `api/test_auth.py`, `api/test_authorization.py`, `api/test_uploads.py`, `api/test_presentation_agent.py`. |
| Expired/revoked credentials during streaming | Cognito/provider checks and periodic speech/SSE reauthorization. `api/test_auth.py`, `api/test_speech.py`, `api/test_session_events.py`. |
| Malformed PDF/PPTX, traversal, macros, embedded/linked Office content, archive expansion | Bounded parser validation before storage/publication; format-specific validation. `api/test_upload_validation.py`, `analysis/test_powerpoint.py`, `processing/test_acceptance_safety.py`. |
| Slide injection requests shell execution, owner change, arbitrary URLs/tools/geometry | Untrusted text remains data; strict server-built choices, catalog/evidence/geometry/source checks bound capabilities. `evaluation/test_replay.py` includes injection/invented-choice/bad-evidence cases; `api/test_presentation_agent.py` rejects tool calls/extra fields. This does not guarantee an injected instruction cannot influence an allowed choice. |
| Provider timeout, rate limit, malformed or unbounded response | Deadlines, bounded streamed bodies, strict schemas and safe failure/no action without error-body echo. `api/test_jev_provider.py`, `api/test_presentation_agent.py`, `api/test_security_privacy.py`. |
| Audio flooding/replay or stalled upstream | Monotonic capture times, audio-rate checks, bounded queue/drain, owner/process admission. `api/test_speech.py`, `api/test_speech_backpressure.py`. |
| Stale action after source change or manual navigation | Post-call server revalidation and native context epoch/sequence checks. `api/test_direct_current.py`, `api/test_presentation_agent.py`, `frontend/tests/test_agent.py` (repository-relative). |
| Private data leaks in errors/logs | Safe codes, route templates rather than raw URLs, bounded metric fields, session conflicts omit SQL parameters/traceback. `api/test_api.py`, `test_observability.py`, `api/test_security_privacy.py`. |

## Privacy and operational limits

Buddy streams microphone audio without deliberately persisting raw audio. Recognized
speech is private data: session events retain it for the five-minute reconnect window
before pruning; ending a session removes its retained events. Desktop rolling context
is a separate short-lived copy. Audio and transcript retention must not be conflated.

Normal request/decision logs contain timings, bounded outcomes and opaque identifiers.
Opt-in diagnostics are an exception to content-free responses: enabling both diagnostics
settings for a single session returns private inputs, options and provider traces to its
authenticated owner. These are not normal logs. Saved diagnostics/replay artifacts need
explicit authorization and private handling; real transcripts, decks and credentials do
not belong in committed fixtures.

Jev may receive current-slide images/text/options; Cohere receives text; Bedrock analysis
receives its documented original source/input; Deepgram receives captured audio. Optional
web/image search discloses queries to the search service/browser. See
[presentation agent](presentation-agent.md), [analysis](analysis.md),
[speech recognition](speech-recognition.md), and [voice web search](voice-web-search.md).
A regional endpoint or inference profile is not a provider-retention guarantee.

Uploads, worker attempts, source pages/bytes, serialized results, provider responses and
speech/context are bounded at their owning boundaries. Worker IAM is separate from the
desktop; provider keys stay server-side. WAF provides deployed client-IP edge policy.
Owner/process budgets are not distributed billing ceilings and do not stop many-account
abuse. Aggregate billing controls, vendor agreements, host/Office patching, production
access and rotation remain operator responsibilities. See [authorization](authorization.md),
[uploads](uploads.md), [workers](pdf-workers.md), and [infrastructure](infrastructure.md).

## Reproduce evidence

Follow [development](development.md), then run:

```sh
python scripts/quality.py api check
python scripts/test.py api
python scripts/test.py desktop
python scripts/test.py repository
```

Backend CI supplies disposable PostgreSQL. Local database cases skip without
`BUDDY_TEST_DATABASE_URL` ending in `_test`; skips do not demonstrate persisted security.
Run `python scripts/test.py database` with that setting to require database coverage.
External boundaries use deterministic transports; these checks require no paid calls or
production credentials. CI also runs dependency, secret and static scans; see [CI](ci.md).

Report PDF and PowerPoint results separately. Fake-provider tests and builds do not prove
microphone accuracy, live availability, Office execution, semantic correctness or staging
security. #44 owns the shared failure matrix/evaluation; #229 collects measured results
and unresolved cases from #26/#29/#39/#44 after earlier prerequisites are stable and reviewed.
