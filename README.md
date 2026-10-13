# Buddy

A context-aware presenter companion with a Python/PySide6 desktop shell and a separate
API foundation. Authentication, deck processing, and live presentation are separate issues.

## Desktop development

Use Python 3.12, 3.13, or 3.14. Supported development targets are Windows x64 and
macOS 13+ on Apple Silicon or Intel, subject to the pinned Qt wheel availability.
Python and PySide6 are not a native PowerPoint playback engine.

```powershell
# Windows PowerShell, from the repository root
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m buddy_desktop
```

```sh
# macOS, from the repository root
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m buddy_desktop
```

The installation also provides the `buddy` GUI entry point. The dependency
`PySide6-Essentials==6.12.0` provides Qt Core, Gui, Widgets, and the PySide6 import
namespace without the unused WebEngine/add-on downloads. Runtime and direct development
dependencies are version-pinned in `pyproject.toml`.

Home checks a **real** configured backend when requested. It does not invent successful
responses or start a backend. Start [the API service](docs/api/README.md) in another
terminal to use the connection check. Library and Present explain their availability; there are no
fake decks, sessions, or sign-in flows.

## Configuration

| Variable | Default | Validation |
| --- | --- | --- |
| `BUDDY_API_URL` | `http://127.0.0.1:8000` | HTTPS remotely; HTTP only for loopback. No credentials, query, or fragment. Optional base path. |
| `BUDDY_REQUEST_TIMEOUT_SECONDS` | `5` | Finite number from 0.1 to 30 seconds. |

Invalid environment configuration exits with code 2 before creating the application.
For example, set `$env:BUDDY_API_URL = 'https://your-backend.example'` in PowerShell
or `export BUDDY_API_URL=https://your-backend.example` on macOS before starting.
No provider secret or account token belongs in either variable.

Without `BUDDY_API_URL`, Settings can save the backend URL in native `QSettings`.
An environment URL overrides and disables editing the saved URL. Native preferences also
retain window geometry. On Windows, Qt stores these under the current user's registry
preferences for `Buddy/BuddyDesktop`; on macOS, it uses the user's preferences system.
No deck, transcript, or credentials are persisted by this shell.

The compatibility adapter contract is `GET <base-url>/health`, returning HTTP 200 with
`Content-Type: application/json` and `{"status":"ok"}` (or `"healthy"`). Redirects,
unhealthy/malformed results, and responses larger than 64 KiB are rejected. This contract
is implemented by the API foundation. Versioned endpoints use `/api/v1` and typed envelopes.
Checks are manual, one at a time, on a background worker. While a check is active,
navigation stays available; backend changes are disabled to avoid stale results.

## Navigation and lifecycle

- Alt+1: Home; Alt+2: Library; Alt+3: Present; Alt+4: Settings.
- Tab/Shift+Tab move between controls. Arrow keys move through workspace navigation.
- The Workspace menu exposes the same routes and the native Quit shortcut.
- The minimum supported window is 800×600 logical pixels. Pages scroll when needed.
- Qt 6 supplies native high-DPI scaling. The theme uses the OS palette and visible focus.
- Closing during a check keeps the event loop responsive while the bounded worker
  finishes. No new requests are accepted during shutdown.

See [the desktop architecture](docs/desktop-shell.md) for extension boundaries and
[the contribution workflow](CONTRIBUTING.md) before beginning another issue.

## Verification

Run from the repository root, using the virtual environment's Python:

```sh
python -m ruff format --check .
python -m ruff check .
python -m mypy
python -m pytest
python -m build
python scripts/smoke_desktop.py
```

For a review screenshot: `python scripts/smoke_desktop.py --screenshot build/shell.png`.
The smoke check uses an isolated settings profile and closes automatically.
Tests use local HTTP fixtures and isolated preferences; they need no provider credentials,
backend deployment, microphone, or paid service. CI runs checks, tests, wheel building,
wheel installation, and shell startup on Windows and macOS. Windows uses the native Qt
platform; macOS uses the offscreen platform. Leave `QT_QPA_PLATFORM` unset when running
the documented native desktop tests locally.

Installer signing/notarization and deployment pipelines belong to #38. This issue builds
an installable Python wheel and source archive, not a signed desktop installer.
