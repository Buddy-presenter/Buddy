# PostgreSQL persistence

Issue #3 adds durable users, decks, processing jobs, analysis versions and presentation
sessions through PostgreSQL, SQLAlchemy and Alembic. Repository reads require owner IDs;
composite foreign keys enforce that child records use the same owner and deck. These are
internal persistence boundaries. Public workflows/authentication remain in their issues.

Docker is optional: use an existing PostgreSQL 18 server, a native installation, or a
local container. Database credentials come from environment configuration. Migrations
run explicitly before API startup; the API never creates or upgrades tables itself.

## Install and choose a local server

Use Python 3.12–3.14 and install the shared package and backend API extra in the virtual
environment: `python -m pip install -e ./shared` followed by
`python -m pip install -e './backend[api,dev]'`. PostgreSQL **18** is tested; use the
current supported patch release.
SQLAlchemy's asynchronous asyncpg driver keeps database I/O outside the API event loop.

**Native or existing PostgreSQL:** install from [PostgreSQL's official downloads](https://www.postgresql.org/download/)
or use an existing private server. As its administrator, run `createuser --pwprompt buddy`
and `createdb --owner=buddy buddy`. Use the server's actual host/port. This development
role owns migrations and records; production uses separate roles described below.

**Optional local Docker:** Docker hosts only PostgreSQL. Set
`BUDDY_LOCAL_DATABASE_PASSWORD` to a local password, then run
`docker compose -f infra/compose.database.yml up -d`. The listener is loopback-only, port
5432 by default; set `BUDDY_LOCAL_DATABASE_PORT` if occupied. The named volume preserves
data across container restarts/recreation. `docker compose -f infra/compose.database.yml down`
stops it without deleting the volume. Do not use `down -v` on data you want to keep.
The image is pinned to the tested PostgreSQL 18 digest; update it deliberately.

## Configure, migrate and start

```powershell
# Windows PowerShell; replace placeholders, do not commit credentials
$env:BUDDY_ENVIRONMENT = 'development'
$env:BUDDY_DATABASE_URL = 'postgresql://buddy:<url-encoded-password>@127.0.0.1:5432/buddy?sslmode=disable'
.\.venv\Scripts\python.exe -m buddy_api.persistence.migrate upgrade
.\.venv\Scripts\python.exe -m buddy_api.persistence.migrate current
.\.venv\Scripts\python.exe -m buddy_api.persistence.migrate check
.\.venv\Scripts\python.exe -m buddy_api
```

```sh
# macOS/Linux; replace placeholders, use your active virtual environment
export BUDDY_ENVIRONMENT=development
export BUDDY_DATABASE_URL='postgresql://buddy:<url-encoded-password>@127.0.0.1:5432/buddy?sslmode=disable'
python -m buddy_api.persistence.migrate upgrade
python -m buddy_api.persistence.migrate current
python -m buddy_api.persistence.migrate check
python -m buddy_api
```

The installed `buddy-db upgrade|current|check` command is equivalent and works from
another directory because migrations are included in the wheel. `upgrade` runs outstanding
revisions to head; repeating it is a no-op. `current` requires the database to be at the
packaged head. `check` checks revision currency and model/schema drift. One transaction
and a schema-scoped PostgreSQL advisory lock protect each migration run. Runtime startup
does not apply DDL. Run migrations once with the deployment's migration identity before
starting new application processes. Back up and test restoration before production DDL.

| Variable | Behavior |
| --- | --- |
| `BUDDY_DATABASE_URL` | PostgreSQL URL with host, database and user. `postgresql://` and `postgresql+asyncpg://` accepted. URL-encode reserved password characters. Only the `sslmode` query parameter is allowed. Required for migration commands and production API startup. |
| `BUDDY_DATABASE_SCHEMA` | Default `public`; simple lowercase SQL identifier. An operator must create a non-default schema and grant access before migration. |
| `BUDDY_DATABASE_TIMEOUT_SECONDS` | Default 3, finite 0.1–30 seconds. Bounds pool acquisition, connection, SQL/lock and driver command waits. |

Database values are omitted from configuration reprs and failure messages. CLI invalid
configuration exits 2; migration/connectivity/revision failures exit 1 with recovery
guidance. Driver exceptions, SQL parameters and submitted record data are not printed by
the boundary. Verify secret configuration, TLS trust/hostname, connectivity, schema grants
and revision history through approved operator tooling. Do not repeatedly retry failed
DDL blindly or stamp a version without applying it. If a revision fails, its transactional
DDL rolls back. Restore a verified backup or apply a reviewed forward repair; the safe
CLI deliberately has no destructive downgrade operation. Alembic's development downgrade
for the initial revision drops all records and is unsuitable for production recovery.

Without a database URL, development/test liveness remains available; readiness is 503.
Production and all remote database connections require `sslmode=verify-full` and
system-trusted server certificates; disabling TLS is allowed only for loopback development.
Readiness is 200 only after application startup, connectivity, current packaged migration
head and existence of all five record tables are verified. It reads no private rows.
Unmigrated/outdated/unavailable databases produce the shared redacted 503 envelope while
`/health` stays live. Shutdown disposes the pool; probes are asynchronous and cancellable.

## Records and transaction boundary

| Record | Durable fields and constraints |
| --- | --- |
| User | UUID, unique nonempty external identity subject, UTC creation time. Identity-provider selection/login remain #5. |
| Deck | UUID, owner, nonempty title, optional private source-object key, UTC creation time. No object is fabricated or uploaded here. |
| Processing job | Owner/deck, pending/running/succeeded/failed status and nonnegative attempts. Processing remains its own workflow. |
| Analysis version | Owner/deck, optional matching job, positive per-deck unique version, explicit JSONB graph payload. Graph validation belongs to #12; an initial payload is never invented. |
| Presentation session | Owner/deck, pinned matching analysis version, created/active/ended status and nonnegative state version. No raw audio/transcript persistence. |

All children have database-enforced owner/deck consistency. Deletion is restricted until
the explicit cleanup/deletion workflow can coordinate dependent database and storage data.
Rows use UUIDs, timezone-aware timestamps, foreign keys, uniqueness/check constraints and
owner/deck indexes. Initial migration DDL is immutable and does not import future models.

Use `Database(settings)` and `async with database.transaction() as repositories:` in
trusted backend services/workers. Successful units commit together; failures roll back
and release their session. Owned `get`/`list` calls require an explicit verified owner;
list limits are 1–100. SQLAlchemy binds values as parameters. Cross-account/missing reads
raise the same safe not-found error; invalid references/duplicates raise a safe conflict;
connectivity errors raise a safe unavailable error. `app.state.database` exposes the
configured lifecycle-managed adapter to future API dependencies. No public CRUD endpoints
are introduced; #5/#31 must derive account scope from authenticated server-side identity.
Repository ownership filters are not a replacement for that authorization.

For a new schema revision, update models and run
`python -m alembic revision --autogenerate -m 'describe the change'` from the checkout
against a disposable migrated database. Review generated DDL, name constraints, plan
rollout compatibility/data backfills, test upgrade/failure recovery and commit the new
revision. Do not change already-applied revision files. Readiness discovers packaged heads
automatically. `check` must pass after upgrade.

## Production boundary

Provisioning, backups/encryption at rest, certificate/secret rotation, deployment and
deletion policies remain #27/#26/#35/#38 release requirements. Require private network
access and a non-superuser runtime role. Use a separate DDL-owning migration identity.
Grant runtime only schema USAGE and SELECT/INSERT/UPDATE/DELETE on the five record tables,
plus SELECT on `alembic_version`; grant no schema CREATE, role management or migration DDL.
The runtime needs no sequence grants because IDs are application-generated UUIDs.
Apply appropriate default privileges for later migrations. Keep credentials in the
backend's secret manager/environment, never the desktop, source files or URLs in logs.

## Verification

Create a disposable **buddy_test** database owned by a test role, on native/hosted
PostgreSQL or an optional container. Set `BUDDY_TEST_DATABASE_URL` to its PostgreSQL URL
(PowerShell `$env:... = '...'` or shell `export ...='...'`) and run:

```sh
python scripts/test.py database
python -m pytest
python -m ruff format --check .
python -m ruff check .
python -m mypy
python -m buddy_api.schema --check --compare-ref origin/main
python -m build
python backend/scripts/smoke_api.py --database   # uses migrated BUDDY_DATABASE_URL
```

Tests refuse a database name not ending `_test`. Each test creates and drops only its own
randomly named schema; no existing application tables are touched. Integration tests skip
when the explicit test URL is absent. They cover all record round trips across API restarts,
owner isolation/composite constraints, uniqueness, parameterized hostile text, transaction
rollback/recovery, empty/repeated/concurrent migrations, model drift, failed DDL rollback,
missing/outdated schemas, connection failures and redaction. The ordinary smoke command
without `--database` verifies honest 503 readiness for an unconfigured database.

The existing CI is extended with PostgreSQL 18 integration jobs on Python 3.12/3.14 and
installed-wheel migrations from outside the checkout. Its required CI aggregate includes
these jobs. This verifies issue #3; Yasmina's #30 still owns workflow consolidation and
security gates, and is not implemented or closed by this PR.

Implementation references: [SQLAlchemy async I/O](https://docs.sqlalchemy.org/en/21/orm/extensions/asyncio.html),
[Alembic migrations](https://alembic.sqlalchemy.org/en/latest/tutorial.html),
[asyncpg](https://magicstack.github.io/asyncpg/current/).
