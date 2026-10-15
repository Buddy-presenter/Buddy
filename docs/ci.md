# Continuous integration (#30)

`ci.yml` runs on every pull request and push to main, without path filters. It replaces
`desktop.yml` and `project-status-tests.yml`. CI installs the local projects from `shared/`,
`frontend/`, and `backend/`; application behavior does not change.

| Job | Coverage |
| --- | --- |
| Frontend | Existing Windows/macOS x Python 3.12/3.14 matrix; repository format/lint, strict types, API schema compatibility, all desktop/shared/API unit tests, distributions, wheel installation, desktop and HTTP API startup. Windows uses native Qt; macOS uses offscreen Qt. |
| Backend | Python 3.12/3.14 on Linux with disposable PostgreSQL 18; API quality/schema checks, all API tests including real database integration, distributions, wheel installation, repeated packaged migrations, schema check/current, database-backed HTTP readiness. |
| Automation | Existing Project status regressions plus CI gate failure-path regressions; Node on the hosted runner; no `PROJECT_TOKEN`. |
| Security | Calls `security.yml` from the same commit; dependency, secret and static analysis jobs. |
| CI | Always runs after all four groups. Only all-success passes; failures, cancellation, unexpected skips, missing or malformed results fail. |

Frontend intentionally has no database URL, so PostgreSQL tests skip there. Backend sets
both database URLs and runs them against a real service. No job-level skips are allowed.
The cross-platform API smoke/unit coverage is retained even though Backend also runs API
tests on Linux. All previous build and startup checks remain present.

## Security and required checks

Keep the existing required check **CI** in the main branch ruleset/protection. Because
Security is a direct prerequisite of CI, required security scans cannot pass by being
skipped or fail independently of the merge gate. An administrator must verify that CI
is actually required: workflow code alone cannot enable repository branch protection.
At implementation time the live main ruleset required CI, one teammate approval,
approval of the latest push, stale-review dismissal, and an up-to-date branch.

Dependency audit blocks all reported vulnerabilities and lookup failures (`--strict`).
`pip freeze` excludes the local `buddy-desktop`, `buddy-backend`, and `buddy-identity`
distributions and audits their resolved third-party dependencies.
Gitleaks blocks detected secrets in the PR/push history with redacted output and without
PR comments or uploaded reports. Bandit blocks high-severity findings at every confidence
level in `frontend/src`, `backend/src`, `shared/src`, and `scripts`. There are no blanket finding allowlists or continue-on-error
steps. Lower-severity static findings remain review work; changing thresholds needs review.
Scanner versions and action commits are pinned. Advisory-service or scanner failures
block CI; fixing infrastructure or rerunning a transient failure is preferable to bypassing it.

PR jobs receive only a read-only GitHub token, no AWS, signing, Project or production
secrets. The gitleaks action supports this user-owned public repository without a license
secret. It must be reassessed if moved into an organization.

## Workflow ownership and existing work

The desktop/API foundation PRs #54/#56, automation #58, quality #60 and persistence #61
are already merged into the starting main revision. Active PR #64 changes the Project
status updater and its regressions; this consolidation does not edit either file. Its
tests will continue running from whichever versions are merged.

`project-status.yml` continues to own actual Project writes. `security.yml` is a reusable
workflow called by CI, so it needs no duplicate push/PR trigger. Infrastructure validation
belongs to #27; backend deploy/promotion/rollback (`deploy.yml`) and desktop
packaging/signing/publishing (`release.yml`) belong to their implementation issues.
No empty workflows are added for those responsibilities, and no worker implementation
is claimed while none exists. Component paths in this guide follow #59's ownership layout.

## Local checks

After installing `./shared`, `./frontend[dev]`, and `./backend[api,dev]` in a virtual environment:

```sh
python scripts/quality.py all check
python scripts/test.py all
python -m buddy_api.schema --check --compare-ref origin/main
python -m build frontend
python -m build backend
python frontend/scripts/smoke_desktop.py
python backend/scripts/smoke_api.py
node scripts/test_project_status.cjs
node scripts/test_ci_gate.cjs
python -m pip install -r .github/security-requirements.txt
python -m pip freeze --exclude buddy-desktop --exclude buddy-backend --exclude buddy-identity > audit-requirements.txt
python -m pip_audit --strict --no-deps --disable-pip -r audit-requirements.txt --progress-spinner off
python scripts/security_scan.py
```

For PostgreSQL tests and migration smoke checks, follow [database setup](database.md).
The audit queries public vulnerability metadata; it does not use paid AI/ASR providers.
The exported audit requirements are temporary, not a committed dependency lockfile.
Install Gitleaks 8.24.2 and run `gitleaks git --redact` to scan local history. Validate
the real PR's matrix, security scans and aggregate before merging. #30 was already
closed when implementation began; this PR does not reopen or reassign it.
