# Contribution workflow

Use the GitHub Project's assignments and fields as the source of truth. The initial
issue-body Sprint metadata may be stale. Work only on issues assigned to the requesting
contributor unless they explicitly authorize an exception; respect wave precedence by
completing and reviewing earlier-wave dependencies first. Do not treat an unmerged PR as
completed work on main.

## Where changes belong

- `frontend/src/buddy_desktop/` owns PySide6 UI, the presentation surface, authenticated
  API client, and local Windows/macOS adapters. Put its tests in `frontend/tests/` and
  component-specific smoke/package tools in `frontend/scripts/`.
- `backend/src/buddy_api/` owns API endpoints, application logic, authorization,
  persistence, and worker entry points. Put API/database tests in `backend/tests/`,
  Alembic revisions in `backend/migrations/`, and backend-specific scripts in
  `backend/scripts/`.
- `shared/src/buddy_identity/` is limited to the existing identity configuration and
  transport contracts used by both applications. Keep the components independent; add
  shared code only for a concrete cross-component need.
- Cross-component tests belong in `tests/integration/`; repository tooling tests belong in
  `tests/repository/`. Keep repository-wide tooling in `scripts/`, infrastructure in
  `infra/`, executable automation in `.github/workflows/`, and guides in `docs/`.

Read [the architecture map](docs/architecture.md) and [development guide](docs/development.md)
before moving code or changing component setup. Update the relevant guide when a change
materially changes behavior, boundaries, configuration, entry points, commands, or CI.

1. Set the issue's Project Status to **In Progress** when work starts, using available
   Project write access or a linked draft PR. Ask the user to update only if neither is
   available. Issue state and Project Status are separate.
2. Branch from current main for one issue. Use its exact title with spaces replaced by
   hyphens (Git cannot use spaces in ref names). Preserve the remaining title wording;
   normalize any other Git-forbidden characters explicitly when necessary.
3. Implement the acceptance criteria with real service contracts and persisted state
   where required. Keep unrelated issue workflows separate. Document scope limits.
4. Run the relevant quality, type, build, and behavioral checks. Keep paid provider
   calls and production credentials out of tests.
5. Push the issue branch and open a PR targeting main, with `Closes #<issue>` and a
   concise description of behavior, validation, and limitations.
6. Self-review the complete diff and test the actual implementation. Assign the PR to
   **Lahhamix** and **yasminahanna** and request all other repository collaborators as
   reviewers, excluding the PR author; then
   verify the status workflow sets **Waiting for review**. Submitted feedback sets
   **Reviewed**; a later review request returns to **Waiting for review**. Leave the issue
   open until the PR merges, then verify the Project completion workflow sets **Done**.
7. Check current-head CI after opening the PR and fix failures on the existing branch.
   Respond to review and keep CI passing. Main requires a teammate approval and CI;
   do not merge or push implementation directly to main.

Keep implementation issues assigned to their owner. PR assignees/reviewers are separate;
do not reassign another contributor's issue or change its status as part of your work.

The empty main baseline was initialized solely to make the first PR possible. Its
one-time creation exception is not permission to bypass protections for future work.

Read the [authorization](docs/authorization.md), [CI](docs/ci.md), and
[deployment](docs/deployment.md) guides for the applicable security and operations controls.

See [Project status automation](docs/project-status-automation.md) for workflow setup,
status semantics, and limitations. The Issues and Waves views share the same Status field.
