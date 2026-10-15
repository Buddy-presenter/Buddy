# Private resource authorization

Every private HTTP operation resolves a Principal through signed Cognito tokens and
uncached provider revocation checks, then checks the persisted owner. Clients cannot
select an owner or claim an administrative role. Owner is the only supported role.
Missing/invalid tokens return 401 with WWW-Authenticate; cross-account operations return
403 without private content, and unknown records return 404. Provider/DB failures deny
access with 503. Existing public health/version/readiness routes remain public.

Client-IP throttling belongs to the trusted WAF edge. The API does not trust forwarded
IP headers or use the ALB's direct-peer address as a shared user budget. `/auth/me` has
a bounded per-process bootstrap budget keyed by an opaque credential digest (30/minute,
4096 keys maximum); different credentials do not share it. Resource operations and SSE
revocation rechecks do not consume that bootstrap budget. This is not a distributed
client-IP defense; changing tokens does not bypass the deployment's WAF policy.

GET /api/v1/resources/{kind}/{id} supports decks, analyses, rendered asset metadata,
sessions and processing jobs. Internal storage keys and source paths are not returned.
Rendered assets now have a durable deck/owner constraint (migration 0002_assets).
Storage byte delivery/upload is owned by the storage/rendering features; no public
object URL or unprotected download endpoint is introduced here.

GET /api/v1/decks adds the [owner-scoped library](library.md): bounded pagination,
literal title search, deterministic sorting and persisted processing states. Both the
result rows and total exclude other accounts, with no source/asset keys or graph data.

PATCH uses a strict body without owner identifiers. Deck titles can be changed. Session
activation/end requires the current state_version and uses a locked transaction; stale
updates and updates after end return 409. Analyses and asset/job records are immutable
through these public operations. DELETE is deliberately limited to ended sessions;
other deletions return 409 pending #35's coordinated byte/retention cleanup.

GET /api/v1/sessions/{id}/events provides bounded SSE session-state snapshots. Bearer
tokens must be in headers, including on reconnect. Both identity/revocation and current
ownership are checked for every snapshot. Revocation, deleted records, ownership changes
or provider/DB failure close the stream without more private snapshots. Snapshots are
polled every five seconds for at most 150 seconds; full presentation event transport is
owned by #33. No transcript data is sent. Clients reconnect with their current token.

Run migrations explicitly before deployment. The new table is additive; the old API can
run against it during rollout. Downgrading removes rendered asset metadata and must never
be an automatic rollback step. Database readiness requires the new migration and table.

Run python -m pytest backend/tests/api/test_authorization.py with BUDDY_TEST_DATABASE_URL pointing
to a disposable database ending _test for real persisted cross-account/owner checks.
No AWS credentials are used by tests; provider identity is deterministic, SQL is real.
