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

## Native checks and remaining prerequisite

CI runs the Qt suite and built-wheel GUI smoke on Windows and macOS for Python 3.12/3.14.
Windows uses native Qt. macOS CI uses Qt's offscreen platform; local interactive tests use
the normal platform unless you set `QT_QPA_PLATFORM=offscreen`. API CI runs on Linux with
PostgreSQL. Existing `frontend/scripts/smoke_desktop.py` and `backend/scripts/smoke_api.py --database` verify
the installed applications, beyond pytest's editable-install checks.

There is no processing-worker application or capture/device contract on main. Issue #8
must supply the real worker before its startup/success/error smoke can exist.
`python scripts/test.py worker` exits with a specific blocked result rather than passing
a fabricated worker test. The persisted-job smoke verifies the shared database contract;
it does not claim to execute jobs. Issue #43 is complete for its accepted desktop/API
test-foundation scope. Worker startup, execution and failure coverage belongs to #8;
microphone/device coverage belongs to #15 once its capture contracts exist. Neither is
implemented by the completed test-foundation issue.
