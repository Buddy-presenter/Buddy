# Session events and reconnect

Migration `0018_session_events` adds a durable per-session event sequence and a bounded
notification journal. Apply migrations before upgrading the API. Existing REST routes and
the older metadata SSE endpoint remain available during rollout. Session lifecycle and
state changes append notifications in the same locked transaction as the saved state;
ordering works across processes and restarts without a process-local message broker.
The pinned rollback bridge accepts this additive head; the existing previous-image
PostgreSQL probe exercises legacy reads, writes and sessions against the upgraded schema.

## Public protocol

- `GET /api/v1/sessions/{id}/sync/snapshot` reads the current snapshot and committed cursor
  atomically. Its OpenAPI `EventEnvelope` describes all notification payloads.
- `GET /api/v1/sessions/{id}/sync/events` streams `session_event` frames. The integer SSE
  `id` matches the JSON `session-events.v1` envelope's sequence. Envelopes also contain
  session ID, state version, creation time and a discriminated payload. Send the current
  bearer token in the Authorization header on every connection, never in a URL.
- The first connection sends the authoritative `player.state` snapshot. Reconnect with
  `Last-Event-ID` set to the last consumed sequence. A contiguous, unexpired backlog of at
  most 32 events replays in order; an expired or larger backlog becomes one current
  snapshot at the high-water cursor. A cursor ahead of the session returns 409.
- `POST /api/v1/sessions/{id}/sync/commands` accepts only `kind: player.state`, a UUID
  `command_id`, timezone-aware `sent_at`, and `state` using the existing strict
  `UpdateSessionState` contract, including `expected_state_version`. Bodies are limited to
  8 KiB, including chunked transfers. Commands older than 10 seconds or more than 5 seconds
  in the future return 409; keep client clocks synchronized.
- An acknowledgment returns command ID, committed event sequence, duplicate flag, and
  current snapshot. Retrying the same ID/content returns its receipt without applying
  twice. Reusing an ID for different content, stale versions, and updates after end return
  409. After receipt expiry, timestamps and version checks still reject old commands.

## Producer and execution boundaries

Public clients cannot publish speech or decisions. The speech adapter journals real
partial/final results after they are recognized and before forwarding them to the speaker.
Journal failure closes the speech stream with a retryable error. The speech event's state
version is the session state at publication; it does not identify the slide at capture
time. Downstream decisions must enforce capture-time freshness independently.
Other backend producers call
`publish_server_event` within their transaction; it locks and checks ownership, active
lifecycle and expected state version. Schema-validated inputs are `speech.transcript`
partial/final segments (stream/segment IDs, revision, language, offsets and bounded text),
or `decision.status` notifications with bounded outcome/reason codes and optional navigation
action names. Neither mutates player state. Rolling
context to #18, and decision execution/policy to their own issues.

Events are observations, never an executable action queue. Duplicate/older sequence IDs
must be discarded. A decision notification or a saved `last_accepted_action_id` must not
execute that action again. The desktop restores absolute player state and exposes non-state
notifications to later consumers without executing decisions. Consumers must check state
freshness and stream/segment revisions. Region actions are not added to a live allowlist.

## Bounds, privacy and recovery

The stream polls every 500 ms, rechecking live identity, ownership and deck deletion each
time, and closes after 150 seconds. Heartbeats prevent idle ingress timeouts. Each process
admits at most 2 streams per account and 128 total (429 with Retry-After); the trusted edge
still owns distributed throttling. Batches contain at most 32 journal entries.

The desktop reads bounded 32 KiB frames off the GUI thread. It queues one frame for GUI
acknowledgment and advances its reconnect cursor after consumption. Retry delays grow from
1 to 8 seconds, with at most 8 consecutive failed reconnects before Retry sync is required.
Sign-out, account/deck changes, session end and shutdown cancel subscriptions; old-generation
callbacks cannot update the player. Access failure or exhausted reconnects stop microphone
capture. Manual controls remain responsive; unsent manual navigation takes priority over
remote state observations.
The subscriber also emits generation-tagged connecting/connected/reconnecting signals so
rolling-context consumers can invalidate speech across a transport gap.

At most 256 events remain per session. Events expire from replay after 5 minutes; the API
cleanup task deletes expired rows every 30 seconds while running. Ending a session clears
the journal before appending the final control snapshot, removing retained speech.
Deck/session deletion cascades journal deletion. While the API is stopped, expired rows
remain on disk until cleanup resumes, but cannot be replayed. Speech is not copied into
the durable session state. Database encryption/backups follow existing deployment policy;
this journal does not replace recording consent or retention controls. Ordinary logs
contain a warning-level stream summary with its bounded outcome, delivery count, and
duration, never audio, text, commands or tokens. Summaries remain visible with staging's
warning-level logging and expose connection and delivery health.

## Verification

Run `python -m pytest frontend/tests/test_session_events.py
frontend/tests/test_presentation_sessions.py frontend/tests/test_presentation_stage.py` for
actual local HTTP streams and Qt queued delivery. With `BUDDY_TEST_DATABASE_URL` pointing to
a disposable PostgreSQL database ending `_test`, run `python -m pytest
backend/tests/api/test_session_events.py backend/tests/api/test_authorization.py` for real
persisted ordering, concurrent duplicates, cursor expiry, bounds, authorization and stale
producers. Provider identity is the external test boundary; SQL and protocol parsing are real.

These checks do not claim deployed ingress or human-speech acceptance. Staging verification
must cover idle reconnect, credential expiry, two controllers, manual override and session
end through the deployed HTTPS path.
