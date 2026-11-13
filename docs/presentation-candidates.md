# Presentation candidates (#19)

Issue #19 uses the current persisted slide summaries and validated action purposes,
as agreed for the PDF-first analysis design. It does not reintroduce the historical
semantic graph or add a model/provider request. Candidate retrieval proposes targets;
#20 owns choosing an action and subsequent policy/executor checks still apply.

`POST /api/v1/sessions/{id}/candidates` is authenticated, owner-scoped and read-only.
The API checks the active session, expected state version, current deck/source/job
and pinned analysis. It reads the actual typed analysis and current validation state
in one repeatable-read transaction. Stale or changed state/analysis returns 409;
missing/foreign identity follows existing 401/403/404 behavior. Invalid input is a
redacted 422; oversized input is 413; dependency errors are recoverable 503 responses.
Responses are no-store. Request bodies and speech are never added to ordinary logs
or persisted by this route. Existing speech journal retention is unchanged.

## Protocol and freshness

The versioned request `presentation-candidates.v1` carries expected_state_version,
context_epoch, stream_id, sequence, an aware sent_at timestamp and at most 8192
characters of recent text. Bodies are limited to 64 KiB even with chunked transfer;
body receipt and the database operation each have five-second deadlines. Timestamps
older than five seconds or more than two seconds in the future are rejected. Clients
must keep wall clocks synchronized. This timestamp bounds request transit, not proof
of audio capture; the live desktop capture clock owns capture freshness.

`CandidateService.resolve` accepts #18's actual ContextSnapshot and saved session,
checks capture freshness/window and session/version before network access, sends the
bearer token only as a header and validates the bounded response. Call it outside the
GUI thread. The caller must recheck the live session/version/epoch against the result
before using it; a frozen snapshot or returned candidate cannot authorize an action.
This service is the callable boundary for #20; Present does not yet run an autonomous
agent or make a provider call for each partial transcript.

The response binds session/analysis/state version, epoch, stream, revision sequence,
resolver version and SHA-256 of the exact request. Identical requests against identical
saved analysis/validation produce the same result. No transcript is echoed. Target
records contain kind, existing slide_id, optional validated action_id, bounded score
and a finite reason code. Region parameters remain in the authoritative action record;
clients cannot propose rectangles or assert usability through this API.

## Retrieval and bounds

Resolver `summary-overlap-1` normalizes Unicode (NFKC/casefold), tokenizes letters/numbers
and removes a small English stop-word set. It scans at most the validated 500-slide map
and the bounded saved action list. Scoring counts distinct overlapping words in available
summaries and usable action purposes. A unique highest positive match may propose an
earlier/later slide; a tie abstains from non-linear navigation. Current-slide and explicit
no-change candidates are always present. Whole-utterance English next/previous commands
produce a valid adjacent target except at deck boundaries. Negated or embedded command
phrases are not interpreted as navigation commands. At most two matching usable region
views from the shortlisted slides are included, with stable ordering and at most eight
candidates overall. PDF actions require validated, usable and semantic-verified results.
No rejected, pending or execution-only region action becomes eligible.

This first resolver is deliberately lexical: it does not guarantee synonym, pronoun,
translation or conceptual inference. Unicode topic matching works in other languages
when words agree, but explicit navigation phrases and stop words are English in v1.
Missing/ambiguous evidence returns conservative choices, including no change. Future
semantic retrieval can replace this versioned implementation if replay evaluation
shows a concrete need, without expanding the executable action contract.

## Verification

Run backend/tests/api/test_presentation_candidates.py with a disposable PostgreSQL
*_test database for real authorization, persisted session/source/analysis checks,
staleness, repeatability, read-only behavior, bounds and privacy. Replay tests cover
current, adjacent and earlier topics, ambiguous speech, validated views and 500 slides.
frontend/tests/test_candidates.py uses actual local HTTP for response binding, malformed
responses and stale-context refusal. tests/integration/test_speech_stream.py's deterministic
case exercises the production Qt speech parser, live context, candidate client, HTTP API
and PostgreSQL together. Only identity/speech-provider boundaries are substituted. No paid
provider or human microphone recording is required. These tests are local evidence,
not a claim of deployed or human-language accuracy acceptance.
