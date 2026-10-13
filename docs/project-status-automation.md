# Project status automation

Project #2 uses Backlog, In Progress, Waiting for review, Reviewed, and Done.
Link an issue with `Closes #57` or the PR Development sidebar; plain mentions do not count.

- No review cycle or draft: In Progress.
- Review requested: Waiting for review.
- Another person's submitted approval, requested changes, or comment review: Reviewed.
- A later review request: Waiting for review again.
- Removing the final outstanding person/team request: In Progress if no feedback exists;
  otherwise Reviewed. Remaining requests and later re-requests still wait for feedback.
- Reviewed describes feedback, not merge eligibility. Branch protection still enforces approval and CI.
- Existing item-closed/PR-merged Project workflows own Done. This automation never closes an issue.

The workflow reads current PR/review history, paginates API responses, and reconciles every 15 minutes (GitHub schedules may be delayed). Multiple active PRs use In Progress before Waiting for review before Reviewed. Draft conversion starts a new review cycle. Closed/archived issues are excluded. An unmerged closed PR is reconciled to In Progress on its close event.

Repository secret `PROJECT_TOKEN`: Hussein's classic PAT with `project` scope. Public Buddy repository reads need no repo scope. Set an expiration and rotate the repository secret before it expires. Never commit the token.

Same-repository PR events run the inline pinned github-script action without checking out or executing PR source. Fork PR jobs skip because secrets are unavailable; the scheduled/manual reconciliation on main covers them. Repository collaborators who can edit workflows are trusted with repository secrets. PR text is never interpolated into shell or JavaScript source.

After merge, `workflow_dispatch` can reconcile immediately; otherwise events/schedule apply. Before merge, same-repository PR events allow live validation using this PR. This workflow is not a required CI gate; missing/expired token fails visibly in its own run. Run `node scripts/test_project_status.cjs` for mocked API regression checks.

Run logs report the reconciled status for linked open issues and the observed status for
closed issues. These are the same Project items across the Issues and Waves views;
Sprint/Wave fields are untouched. No local Project token is needed to trigger repository
events or dispatch the workflow. Start-of-work updates without a linked PR still need
Project write access or a manual board update.
