# Buddy

Buddy is a PySide6 desktop companion for presentations, backed by a separate API. The
repository keeps the desktop client, backend, and the small identity package shared by
both in separate component directories.

## Quick start

Use Python 3.12, 3.13, or 3.14 and create a virtual environment at the repository root.
Install the shared identity package, then install either application independently:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .\shared
.\.venv\Scripts\python.exe -m pip install -e ".\frontend[dev]"
.\.venv\Scripts\python.exe -m buddy_desktop
```

To install the API in the same environment, install its own dependencies and start it in
a second terminal:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".\backend[api,dev]"
$env:BUDDY_ENVIRONMENT = 'development'
.\.venv\Scripts\python.exe -m buddy_api
```

The desktop defaults to `http://127.0.0.1:8000`. The API is live without PostgreSQL, but
readiness and persisted account/library features require a configured, migrated database.
Sign-in additionally requires Cognito settings; see [account setup](docs/accounts.md).
Docker is optional for local PostgreSQL. See [development](docs/development.md) for the
complete setup, test commands, and macOS equivalents.

## Project guides

- [Architecture and ownership](docs/architecture.md)
- [Development and testing](docs/development.md)
- [Deployment and release](docs/deployment.md)
- [Security baseline](docs/security-baseline.md)
- [Contribution workflow](CONTRIBUTING.md)
