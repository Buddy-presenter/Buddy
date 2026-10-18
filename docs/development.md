# Development

Buddy has three installable projects: the PySide6 desktop client, the API/backend, and the
small shared identity package. Use Python 3.12, 3.13, or 3.14. Create one virtual
environment at the repository root; install only the component(s) you are working on.

## Desktop

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .\shared
.\.venv\Scripts\python.exe -m pip install -e ".\frontend[dev]"
.\.venv\Scripts\python.exe -m buddy_desktop
```

macOS:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ./shared
.venv/bin/python -m pip install -e './frontend[dev]'
.venv/bin/python -m buddy_desktop
```

The installed GUI command is `buddy`. Normal application startup loads the public
HTTPS staging defaults bundled with the preview; environment variables override them.
For local API development, explicitly set `BUDDY_API_URL=http://127.0.0.1:8000`.
Browser sign-in uses the OS-registered callback from the installed Windows/macOS package,
not a localhost HTTP listener. Windows source-only account testing can use
`python frontend/scripts/run_local_accounts.py` with the matching local API launcher;
see [local account development](accounts.md#local-account-development-windows).
No account or demo data is fabricated.

## API and database

Install the API separately in the same environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".\backend[api,dev]"
$env:BUDDY_ENVIRONMENT = 'development'
.\.venv\Scripts\python.exe -m buddy_api
```

The API listens on `127.0.0.1:8000` by default. `/health` verifies liveness; `/api/v1/ready`
requires a reachable database with current migrations. Configure PostgreSQL using
`BUDDY_DATABASE_URL`, then apply migrations with `python -m buddy_api.persistence.migrate
upgrade`. See the [API guide](api/README.md) and [database guide](database.md) for all
configuration and recovery details. PostgreSQL may be native, already available, or run in
the optional local container described by the database guide.

## Checks

Run repository-wide checks from the root using the active virtual environment:

```sh
python scripts/quality.py all check
python scripts/test.py desktop
python scripts/test.py api
python scripts/test.py all
python -m build frontend
python -m build backend
```

`python scripts/test.py database` requires `BUDDY_TEST_DATABASE_URL` to point to a
disposable PostgreSQL database whose name ends in `_test`. `python scripts/test.py
integration` runs the cross-component desktop/API/database flow and needs that same test
database. It creates and removes only isolated test schemas. The desktop startup smoke is
`python frontend/scripts/smoke_desktop.py`; API smoke checks are
`python backend/scripts/smoke_api.py` and `python backend/scripts/smoke_api.py --database`.
Tests do not call paid providers or require production credentials.

For feature-specific contracts, read the linked guide before editing. Keep tests with
their owning component unless a test intentionally crosses a component boundary; those
belong in `tests/integration/`.

Authenticated original-PPTX ingestion, storage limits, retry contracts and recovery are
documented in [private uploads](uploads.md). Library includes PDF/PPTX upload controls.
See [PDF presentations](pdf-presentations.md) for the built-in PDF viewer and page validation.

The standalone backend PPTX extraction stage and local inspection command are documented
in [extraction](extraction.md); it requires no upload, worker, renderer or paid provider.

The reusable Qt slide surface, six-action executor, standalone input schemas and real
slide-image test/evidence commands are documented in [slide actions](slide-actions.md).
For launching Buddy and trying every command in Present, follow the
[manual test guide](slide-actions-manual.md). These local controls need no backend.

The isolated PDF action validation API, private evidence, runtime setup and test commands
are documented in [action validation](action-validation.md). It uses predefined actions;
no model generation or document text extraction is required.
