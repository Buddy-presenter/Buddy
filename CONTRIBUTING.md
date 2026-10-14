# Contribution workflow

Use the GitHub Project's assignments and fields as the source of truth. The initial
issue-body Sprint metadata may be stale. Start only issues assigned to you and respect
wave precedence: complete and review dependencies from the earlier wave before starting
dependent work in a later wave. Do not treat an unmerged PR as completed work on main.

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

Read [the security baseline](docs/security-baseline.md) for required controls, implementation
owners, and unresolved release blockers. It is a requirement document, not a claim that
the listed authentication, privacy or deployment controls have already shipped.

See [Project status automation](docs/project-status-automation.md) for workflow setup,
status semantics, and limitations. The Issues and Waves views share the same Status field.
