# Local code quality

Use Python 3.12–3.14. From a clean checkout, create and activate a virtual environment:

```powershell
# Windows PowerShell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ./shared
python -m pip install -e "./frontend[dev]" "./backend[api,dev]"
```

```sh
# macOS/Linux
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e ./shared
python -m pip install -e './frontend[dev]' './backend[api,dev]'
```

If activation is restricted, replace `python` below with `.\.venv\Scripts\python.exe`
on Windows or `.venv/bin/python` on macOS/Linux. Installing dependencies requires
package-registry access once; running quality tools needs no credentials or network.

| Operation | Desktop | API |
| --- | --- | --- |
| Apply formatting | `python scripts/quality.py desktop format` | `python scripts/quality.py api format` |
| Check formatting | `python scripts/quality.py desktop format-check` | `python scripts/quality.py api format-check` |
| Lint | `python scripts/quality.py desktop lint` | `python scripts/quality.py api lint` |
| Strict types | `python scripts/quality.py desktop type` | `python scripts/quality.py api type` |
| All read-only checks | `python scripts/quality.py desktop check` | `python scripts/quality.py api check` |

`python scripts/quality.py all check` checks formatting/lint across the repository,
including scripts and tests, then checks both application packages with strict mypy.
`python scripts/quality.py all format` formats the whole repository. Per-application
format/lint commands target that application's source; use `all` before opening a PR.

The frontend and backend pin matching development tool versions independently; shared Ruff, mypy, and pytest configuration lives in the root `pyproject.toml`.
The wrapper uses the active Python interpreter and resolves the repository from its own
file, so `python /path/to/Buddy/scripts/quality.py all check` also works from another
directory. It stops on the first failed tool and returns its exit code. Invalid targets,
commands, or missing application directories return 2. A missing tool reports Python's
module error; reinstall the documented development extras in the active environment.

`format` changes files; review that diff. `format-check`, `lint`, `type`, and `check` are
read-only. `lint` does not automatically fix findings. Run behavior tests separately with
`python scripts/test.py all`; API contract snapshots separately with
`python -m buddy_api.schema --check --compare-ref origin/main`. These commands do not
connect to a database or require production/provider credentials. CI wiring is described
in [the CI guide](ci.md).
