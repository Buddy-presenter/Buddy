# Test foundations

Use Python 3.12 or 3.14 and install the pinned extras from the repository root:

```sh
python -m pip install -e ./shared
python -m pip install -e './frontend[dev]' './backend[api,dev]'
python scripts/test.py all
python scripts/test.py desktop
python scripts/test.py api
```

`desktop` runs the desktop tests. `api` disables the Qt pytest plugin, so it also runs on a
server without a display. `all` runs both component suites, repository tests, and
cross-component tests; PostgreSQL-marked cases skip there if no test URL is configured. These commands propagate pytest failures and use strict markers.
Database tests skip unless a disposable database is configured; that skip is not evidence
of database acceptance. No AI, ASR, storage, payment or production identity credentials
are needed. Existing identity tests inject synthetic verifier/token-store adapters.

## PostgreSQL

Run PostgreSQL 18 locally (or the existing API container/database setup). Create a dedicated
database whose name ends `_test`; its account needs permission to create/drop schemas.
Never point tests at an application database. Set `BUDDY_TEST_DATABASE_URL`, for example:

```powershell
$env:BUDDY_TEST_DATABASE_URL = 'postgresql://buddy:buddy-ci-test-only@127.0.0.1:5432/buddy_test?sslmode=disable'
python scripts/test.py database
```

On macOS/Linux use `export BUDDY_TEST_DATABASE_URL='...'`. The `database` command fails
before collecting tests if the URL is missing; the fixture rejects database names that
do not end `_test`. Every test creates a unique schema, runs real Alembic migrations and
drops that schema on teardown even when an assertion fails. It never drops the database.
Connections and engines close before schema cleanup. CI runs both the API suite and the
explicit database command against its disposable PostgreSQL service.

## Shared fixtures

`backend/tests/api/conftest.py` provides `api_client`, `api_client_factory`, `database_settings`
and `migrated_database`. Clients enter the actual FastAPI lifespan and close on teardown.
The factory accepts explicit test `Settings`, including synthetic identity configuration;
it rejects production configuration. `backend/tests/support/database.py` exposes the isolation fixture and migration helper for
backend tests. Cross-component database/Qt coverage lives in `tests/integration/`.

`frontend/tests/conftest.py` provides:

- `desktop_window`: a factory for actual MainWindow instances with separate temporary INI
  settings, bounded network timeouts and Qt teardown. It clears identity configuration
  for its windows, avoiding the personal OS vault/browser. Pass a loopback backend URL to
  exercise the real HTTP service. Windows close and workers become idle before fixture exit.
- `signal_recorder`: record actual queued signal arguments with QSignalSpy; its returned
  function waits through pytest-qt's event loop instead of sleeping or blocking it.
- `backend`: a loopback HTTP server with controllable response, delay and release events.
  Teardown releases blocked requests and closes the socket/thread. It never calls a provider.

Reuse `qtbot.waitUntil` and `qtbot.waitSignal` for event-loop waits. Keep OS adapters
injected at their existing boundaries, as the account tests do for token storage. Do not
introduce a microphone adapter without the capture contract: capture/device code is not
implemented yet. Tests must never acquire real microphones or personal account tokens.

The new desktop smoke tests render the real window, navigate through queued signals,
check settings isolation, and exercise a denied HTTP request while a Qt timer continues.
The API smoke covers actual startup, honest unavailable readiness, host rejection and
error request IDs. The PostgreSQL smoke writes a real processing job, reads it from another
application instance and checks foreign-owner rejection. Existing API migration/restart,
identity, error, cancellation and Qt lifecycle tests remain in the suites.

`frontend/tests/test_slide_actions.py` exercises the real slide executor with checked-in,
Buddy-authored PowerPoint image exports. It checks rendered viewport pixels, coordinate
mapping, all five catalog actions, malformed/stale inputs and load failure/cancellation
recovery. See [slide actions](slide-actions.md) for fixture permission and capture commands.

## Native checks and remaining prerequisite

CI runs the Qt suite and built-wheel GUI smoke on Windows and macOS for Python 3.12/3.14.
Windows uses native Qt. macOS CI uses Qt's offscreen platform; local interactive tests use
the normal platform unless you set `QT_QPA_PLATFORM=offscreen`. API CI runs on Linux with
PostgreSQL. Existing `frontend/scripts/smoke_desktop.py` and `backend/scripts/smoke_api.py --database` verify
the installed applications, beyond pytest's editable-install checks.

Issue #43 is complete for its accepted desktop/API test foundations. Issue #8 supplies
the real processing worker. With the disposable PostgreSQL URL configured, run
`python scripts/test.py worker`: real migrations, PDF inspection, persisted output,
restart/expired leases, duplicates, retention-aware outbox publish recovery, quota/concurrency,
retry/cancel fencing, rollback on partial persistence and owner-only APIs are exercised.
Only external S3/SQS boundaries are substituted in these deterministic tests.

`docker build -f infra/container/Dockerfile.worker -t buddy-worker:local .` and
`python infra/container/smoke_worker.py --image buddy-worker:local` exercise the installed
Linux worker, real PostgreSQL with a restricted runtime role, read-only root, dropped
capabilities, bounded scratch, the actual PDF parser, health and process restart. The
smoke uses synthetic AWS HTTP responses, not cloud credentials. CI runs this as well as
the worker test target on Python 3.12/3.14.

For opt-in real staging S3/SQS checks, see [PDF workers](pdf-workers.md). These are not CI
tests and can consume AWS credits. A local-database cloud smoke is not proof of private
staging RDS networking, task-role authorization or a deployed worker service.
Microphone/device coverage remains with future issue #15; it does not exist yet.
