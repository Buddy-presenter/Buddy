# Presentation session API

Presentation sessions are durable, account-owned server records. A session pins the
published analysis version used by the presentation and stores only bounded control state:
the active stable slide ID, full-slide or region focus, autonomy mode, last accepted
validated action ID, lifecycle status, and monotonically increasing state version. Audio
and full transcripts are not session data.

## Lifecycle

- `POST /api/v1/sessions` with `{ "deck_id": "…" }` starts a session or idempotently
  resumes the active session for that owner and deck. The API checks ownership, persisted
  source, latest processing result, published analysis, and slide count. A deck that is
  still processing, partial, stale, or missing its source is rejected with `409`.
- `GET /api/v1/sessions/{id}` reads an owner's session state.
- `POST /api/v1/sessions/{id}/resume` resumes a created session when its pinned analysis is
  still current. An ended session is terminal; start a new session to present again.
- `PATCH /api/v1/sessions/{id}/state` replaces the bounded state and requires
  `expected_state_version`. The API validates slide and accepted-action references against
  the pinned analysis, then increments the version in the same transaction.
- `POST /api/v1/sessions/{id}/end` requires `expected_state_version` and marks the session
  ended. Repeating end on an already ended session is idempotent.

The slide ID is an analysis-stable identifier, not a screen index; the desktop maps it to
the ordered slide in its loaded source. Focus regions are in the slide's source coordinate
plane and must fit within that slide's recorded width and height. `manual`,
`assisted`, and `autonomous` are persisted mode selections; storing a mode does not itself
enable speech recognition, model decisions, or automatic action execution. Those behaviors
belong to their own presentation-control issues.

Every state write is protected by a row lock and compare-and-set version check. A `409`
means another controller committed newer state; the caller must fetch the current snapshot
and reconcile rather than replaying an outdated write. Requests reject unknown fields, so
transcript or arbitrary client payloads cannot be added to stored session state.

The older generic resource route remains read-only for session mutation. Session lifecycle
and state writes must use these typed endpoints so readiness, slide references, action
validation, and version checks cannot be bypassed.
