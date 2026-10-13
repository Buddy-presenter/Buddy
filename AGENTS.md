# Working on Buddy

Read the full assigned issue: objective, acceptance criteria, dependencies, and definition
of done. Use the GitHub Project's **Waves** view and its Sprint/Wave fields for ordering.
The Issues tab is a list, not the execution order; issue-body sprint metadata can be stale.

- Work only on issues assigned to HusseinMoukalled unless the user directs otherwise.
- Finish and peer-review earlier-wave prerequisites before dependent later-wave work.
- Each issue has its own branch, starting from current main. Use the exact issue title,
  replacing spaces with hyphens because Git forbids spaces in ref names. Preserve wording
  and capitalization; explicitly explain any additional normalization Git requires.
- Open one PR per issue targeting main, assign the PR to **Lahhamix**, and request him
  as reviewer. Keep the implementation issue assigned to HusseinMoukalled.
- Do not push implementation directly to main. Merge only after teammate approval,
  required checks passing on the current head, and the user's instruction to merge.
- For requested changes, fix the existing branch/PR and request Lahhamix's review again.
- Use the Project status workflow for linked PR/review transitions and verify its logs.
  Requesting review sets **Waiting for review**; submitted feedback sets **Reviewed**,
  which does not imply approval. A re-request starts a new review cycle. Existing Project
  completion workflows own **Done** after merge. Issue state and Project status are separate.
  For work without a linked PR, set **In Progress** through available Project write access;
  ask the user to update only when that access is unavailable. See
  `docs/project-status-automation.md` for scope and credential setup.
- Keep real integrations, typed boundaries, recoverable failures, tests appropriate to
  the change, and reviewable documentation. Record unavailable prerequisite code rather
  than treating a Done card or closed issue as proof that it was implemented.

Follow `CONTRIBUTING.md` and `docs/security-baseline.md`. The security baseline states
requirements and release blockers; it does not claim unfinished controls are implemented.
