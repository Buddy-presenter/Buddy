# Working on Buddy

Before editing, read [the architecture map](docs/architecture.md) and the relevant
[development](docs/development.md) or feature guide. Use the GitHub Project's Waves view
and Sprint/Wave fields as the execution order; issue-body planning metadata may be stale.

- Work only on issues assigned to the requesting contributor unless they explicitly
  authorize an exception. Never reassign an implementation issue without explicit direction.
- Complete and review earlier-wave prerequisites before dependent later-wave work.
- Read the full issue, including acceptance criteria, dependencies, and definition of done.
- Use one branch per issue, based on current `main`, named with the exact issue title and
  spaces replaced by hyphens. Preserve wording and capitalization; explain any further
  normalization required by Git.
- Keep implementation in its owning component. Do not introduce features, services,
  generic shared layers, or unrelated refactors while reorganizing code.
- Run relevant component and repository checks, update docs when setup or behavior changes,
  and preserve security boundaries. See [CONTRIBUTING.md](CONTRIBUTING.md) and the
  [security baseline](docs/security-baseline.md).
- Open one linked PR for review; never push implementation directly to `main`. Assign
  PRs to `Lahhamix` and `yasminahanna`, request all other repository collaborators as
  reviewers (excluding the PR author), and wait for teammate approval and current-head CI.
  Merge only when the requester explicitly asks.
- Preserve issue ownership and wave order. Issue assignees and PR reviewers are separate.
  Project status is distinct from issue open/closed state. Requesting review sets
  **Waiting for review**; submitted feedback sets **Reviewed**; merge completion owns
  **Done**. Follow [Project status automation](docs/project-status-automation.md).

Keep this file short and link to authoritative guides instead of duplicating them.
