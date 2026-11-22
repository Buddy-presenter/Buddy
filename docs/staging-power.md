# Starting and stopping staging

To save AWS credits, the staging **API and worker are kept off** (0 tasks) when nobody is
testing. The database, load balancer, WAF, stored decks and analyses stay as they are.

## When you need staging

- **Not needed:** writing code, running tests and checks, opening or merging PRs, CI.
- **Needed:** using the Buddy desktop app against staging — sign-in, Library, uploads,
  Review and checks, Present, Buddy Mini with live speech.

If Buddy says it can't reach the server, staging is probably off.

## Turn staging on or off

1. Open the repository's **Actions** tab and choose **Staging on/off**.
2. Select **Run workflow**, keep branch `main`, and pick `on`, `off` or `status`.
3. The run waits for the `staging` environment approval. Approve it (any of the
   environment reviewers can, including the person who started it).
4. `on` finishes when both services are running, about 1–3 minutes. The run summary
   shows the result.

Turn staging **off** when you finish, after checking that nobody else is mid-test.
A merge to `main` that deploys to staging keeps the current number of tasks, so a
deploy does not turn a stopped staging back on.

The workflow uses the existing staging deploy role (GitHub OIDC); nobody needs a
personal AWS account. It only changes the desired count of the `api` and `worker`
services in the `buddy-staging` cluster.
