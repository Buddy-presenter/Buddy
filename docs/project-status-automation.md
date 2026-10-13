# Project status automation
This workflow has one write capability: update the Status field of an existing issue in Buddy Project #2.

## Rules
- Only process the explicit PR from the event (or the required manual pr_number input).
- Use GitHub's closing-issue links, not text mentions or issue-to-issue references.
- Linked open PR without a review cycle, or draft: In Progress.
- Current requested review after the last feedback: Waiting for review.
- Submitted teammate feedback: Reviewed (approval, requested changes, or comment review).
- Cancel the last request: In Progress without feedback, otherwise Reviewed.
- Closed issues, archived/non-issue cards, foreign issues, and Done cards are never changed.
- A merged PR does not trigger a status write here. GitHub's Item closed → Done workflow owns completion.

There is no scheduled scan of other PRs. Manual recovery requires a specific PR number. Removing this broad reconciliation is intentional: an unrelated PR event must not alter somebody else's work. Use one implementation PR per issue; coordinate multi-PR work manually.

## Strict boundaries
No assignee changes, issue reopen/close calls, labels, comments, Sprint/Wave edits, new Project items, deletion, or merge operations. Only updateProjectV2ItemFieldValue for the Status field is allowed. Read errors fail without guessing. Repository instructions and deliberate human edits are separate from this script.

## Project settings
Keep Item closed → Done and Pull request merged → Done enabled.
Keep Auto-close issue (Done card → close issue) disabled.
Disable the competing Pull request linked to issue → In Progress rule: it does not have this script's closed-issue/Done safeguards. Newly added items should default to Backlog.
Do not enable other built-in rules that overwrite these review statuses.

## Credentials and checks
Repository PROJECT_TOKEN (classic project scope) is required. It reads this public repository and writes Project Status only. Same-repository PR events run pinned inline code without checkout; fork events skip. To reconcile a fork PR after merge of this fix, manually dispatch with its PR number.
Run node scripts/test_project_status.cjs. Tests cover review transitions, canceled requests, closed/Done guards, plain mentions, mixed/paginated cards, and the restricted write API.

## Investigation
The audited main script from merged PR #58 had no assignment or issue-state writes. Issue #2's reopening at 2026-10-13 15:58 UTC predates #58's workflow creation and merge. The reviewed #59 assignment event records Lahhamix as actor. These observations do not prove every reported incident has the same cause; they do rule out those changes being performed by this script's mutation.
