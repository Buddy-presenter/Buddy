# Working on Buddy

Read the full assigned issue: objective, acceptance criteria, dependencies, and definition
of done. Use the GitHub Project's **Waves** view and its Sprint/Wave fields for ordering.
The Issues tab is a list, not the execution order; issue-body sprint metadata can be stale.

- Work only on issues assigned to HusseinMoukalled unless the user directs otherwise.
- Finish and peer-review earlier-wave prerequisites before dependent later-wave work.
- Each issue has its own branch, starting from current main. Use the exact issue title,
  replacing spaces with hyphens because Git forbids spaces in ref names. Preserve wording
  and capitalization; explicitly explain any additional normalization Git requires.
- Open one PR per issue targeting main and request **Lahhamix** as reviewer.
- Do not push implementation directly to main. Merge only after teammate approval,
  required checks passing on the current head, and the user's instruction to merge.
- For requested changes, fix the existing branch/PR and request Lahhamix's review again.
- The user manages Project statuses while Project-write access is unavailable. Tell them
  to set **In Progress** when work starts, **Waiting for review** when the PR is ready,
  and **Done** after merging. Issue open/closed state and Project status are separate.
- Keep real integrations, typed boundaries, recoverable failures, tests appropriate to
  the change, and reviewable documentation. Record unavailable prerequisite code rather
  than treating a Done card or closed issue as proof that it was implemented.

Follow `CONTRIBUTING.md` and `docs/security-baseline.md`. The security baseline states
requirements and release blockers; it does not claim unfinished controls are implemented.
