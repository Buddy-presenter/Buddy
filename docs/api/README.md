# Buddy API foundation

The API is a separate server process used by the native desktop client and future
processing workers. It is not a browser frontend. This increment has only operational
endpoints. [PostgreSQL persistence](../database.md) supplies internal durable records;
account, deck and presentation workflows remain separate issues.

## Start locally

Install Python 3.12–3.14 and use the repository's virtual environment:

```powershell
# Windows PowerShell, from the repository root
.\.venv\Scripts\python.exe -m pip install -e ".[api,dev]"
$env:BUDDY_ENVIRONMENT = 'development'
.\.venv\Scripts\python.exe -m buddy_api
```

```sh
# macOS/Linux, from the repository root
.venv/bin/python -m pip install -e '.[api,dev]'
export BUDDY_ENVIRONMENT=development
.venv/bin/python -m buddy_api
```

`buddy-api` is the equivalent installed console command. The default listener is
`127.0.0.1:8000`; the desktop's default backend URL already points there. Start the
desktop in another terminal and click Check connection. Development API documentation
is at `http://127.0.0.1:8000/docs`; its generated schema is at `/openapi.json`.

The package contains both namespaces; installing the `api` extra adds the pinned
FastAPI/Uvicorn/Pydantic dependencies. API requests do not create a Qt event loop or
import the desktop UI. Signed installers/service distribution remain separate release work.

## Configuration and failure behavior

| Variable | Requirement/default |
| --- | --- |
| `BUDDY_ENVIRONMENT` | Required: `development`, `test`, or `production`; missing/invalid values exit with code 2 before binding a socket. |
| `BUDDY_ALLOWED_HOSTS` | Comma-separated exact hosts, without schemes/ports/wildcards. Default `localhost,127.0.0.1,::1` in development/test. Explicit value required in production. |
| `BUDDY_BIND_HOST` | Default `127.0.0.1`. Exposing another interface is an explicit deployment choice. |
| `BUDDY_PORT` | Default `8000`; integer 1–65535. |
| `BUDDY_READINESS_TIMEOUT_SECONDS` | Default `2`; finite 0.1–30 seconds per asynchronous dependency probe. |
| `BUDDY_DATABASE_URL` | PostgreSQL connection secret. Required for production; absent development/test database fails readiness with 503. See the database guide for verified TLS and explicit migrations. |
| `BUDDY_DATABASE_SCHEMA` | Default `public`; operator-created lowercase schema identifier. |
| `BUDDY_DATABASE_TIMEOUT_SECONDS` | Default `3`; finite 0.1–30 seconds. |

Database credentials come from the backend environment/secret manager, never a bundled
file or desktop configuration. Liveness can run without a database in development;
readiness requires a reachable, explicitly migrated database. Errors never echo configured values.

Production requires a TLS ingress and private deployment configuration owned by #27/#26;
setting `production` is not evidence that authentication, quotas or other release controls
are implemented. Interactive docs/schema routes are disabled in production. Proxy-header
trust and access logging are disabled by default to avoid trusting forwarded identity
or logging sensitive URLs. Configure trusted ingress deliberately in the deployment work.

## Contracts and request validation

| Endpoint | Behavior |
| --- | --- |
| `GET /health` | Compatibility endpoint: `{"status":"ok","request_id":"..."}` for the existing desktop adapter. |
| `GET /api/v1/health` | Liveness: running request handler returns `{"data":{"status":"ok"},"request_id":"..."}`. |
| `GET /api/v1/ready` | 200 only after startup and all registered dependency probes succeed. Returns explicit dependency statuses. Failures/timeouts return 503 with redacted reasons. |
| `GET /api/v1/version?api_version=v1` | Actual installed service version and API version. An unsupported version query returns validated 422. |

Each response receives a server-generated UUID in `X-Request-ID`; success/error bodies
carry the same ID. Incoming IDs are not trusted. Success bodies use `data`/`request_id`
except the documented legacy health endpoint. Errors use
`{"error":{"code":"...","message":"...","details":[]},"request_id":"..."}`.
Validation details include field locations/types, not submitted values. 400 host errors,
404/405 routing errors, 422 validation, 503 readiness and unexpected 500 errors share
that envelope. Framework-generated docs/schema responses remain their native format.

All responses have `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.
Domain routers must use typed wire models, the shared error model and `/api/v1` prefix;
private workflows additionally require #5/#31 authorization. Do not return an unvalidated
dict, invent demo data, or put internal exception text in a client error.

## Readiness extension boundary

`create_app(settings, probes=...)` registers typed asynchronous adapters with unique safe
names. A probe performs a real, bounded read-only dependency check, returns normally on
success, and raises on failure. Checks run concurrently; failures/timeouts fail readiness
while liveness stays available. Actual dependency messages are never exposed.

The PostgreSQL adapter checks connectivity, current revision and required tables without
reading private rows or applying migrations. Storage/provider features register their own
adapters when implemented. Startup/shutdown state prevents
readiness before startup or after shutdown. Adapters must remain cancellable and must
not swallow cancellation or block the event loop with synchronous network work.

## Schema workflow and checks

```sh
python -m buddy_api.schema                  # regenerate docs/api/openapi.json
python -m buddy_api.schema --check          # exact snapshot matches real route models
python -m buddy_api.schema --check --compare-ref origin/main
python -m pytest tests/api
python scripts/smoke_api.py
```

Generation uses explicit test configuration and needs no secrets or running backend.
CI checks the snapshot and compares existing endpoints/models with main. Endpoint/model
removal, changed existing operations/fields/required sets/constraints and response changes
fail conservatively; new endpoints/model names/optional properties and descriptive edits
are allowed. The first introduction has no older API baseline. A detected incompatible
change must introduce a new API version, not merely regenerate the snapshot. The checker
is deliberately conservative and is not a complete semantic OpenAPI compatibility engine.
Documentation filtering applies to schema/operation annotations, not identifiers in
named maps: fields/models named `title`, `description`, or `x-*` still have their
contracts checked. Literal defaults are contract values, not documentation.

Tests exercise real ASGI lifecycle and failure contracts. The smoke script starts the
actual Uvicorn server on a prebound ephemeral socket, exercises real HTTP and the existing
desktop health adapter, and shuts it down. No real user data, production credentials or
paid provider calls are used. CI builds/installs the wheel and repeats that startup check.

Framework references:
[FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/),
[middleware](https://fastapi.tiangolo.com/advanced/middleware/), and
[error handling](https://fastapi.tiangolo.com/tutorial/handling-errors/).
