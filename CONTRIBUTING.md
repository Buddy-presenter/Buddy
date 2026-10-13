# Contribution workflow

Use the GitHub Project's assignments and fields as the source of truth. The initial
issue-body Sprint metadata may be stale. Start only issues assigned to you and respect
wave precedence: complete and review dependencies from the earlier wave before starting
dependent work in a later wave. Do not treat an unmerged PR as completed work on main.

1. Set the issue's Project Status to **In Progress**.
2. Branch from current main for one issue. Use its exact title with spaces replaced by
   hyphens (Git cannot use spaces in ref names). Preserve the remaining title wording;
   normalize any other Git-forbidden characters explicitly when necessary.
3. Implement the acceptance criteria with real service contracts and persisted state
   where required. Keep unrelated issue workflows separate. Document scope limits.
4. Run the relevant quality, type, build, and behavioral checks. Keep paid provider
   calls and production credentials out of tests.
5. Push the issue branch and open a PR targeting main, with `Closes #<issue>` and a
   concise description of behavior, validation, and limitations.
6. Request review from **Lahhamix** for HusseinMoukalled's implementation work and set
   Project Status to **Waiting for review**. Leave the issue open until the PR merges.
7. Respond to review and keep CI passing. Main requires a teammate approval and CI;
   do not merge or push implementation directly to main.

The empty main baseline was initialized solely to make the first PR possible. Its
one-time creation exception is not permission to bypass protections for future work.
