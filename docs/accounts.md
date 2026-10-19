# Verified account lifecycle (#5, #74)

Buddy uses an Amazon Cognito user pool and a public desktop app client. Account creation,
email verification, password validation, recovery codes and recovery errors are handled
by Cognito's managed login in the system browser. Buddy never collects a password.
The Home page offers Sign in, Create account, Forgot password, Resume session and Sign out.
Sign-in requires email and password, not an email code. Signup requires the email
verification code. Forgot password sends a recovery code; enter that code and a new
password, then sign in with email/password in the browser to return to Buddy.

## Configuration

Set these public values for both the desktop and API (Terraform in #27 supplies them):

| Variable | Value |
| --- | --- |
| `BUDDY_AWS_REGION` | Cognito region; default deployment choice is `eu-central-1` |
| `BUDDY_COGNITO_POOL_ID` | User pool ID from that region |
| `BUDDY_COGNITO_CLIENT_ID` | Public app client ID; **no client secret** |
| `BUDDY_COGNITO_DOMAIN` | `https://<prefix>.auth.<region>.amazoncognito.com` |

The API also needs its existing database settings and completed migrations. No auth
tables or additional migration are needed: verified users map into the existing `users`
table with an issuer-qualified subject. Repeated login and API restarts preserve the
same UUID. `/api/v1/auth/me` retains that UUID and adds the provider-verified `email`
and `email_verified` fields in the existing request-ID envelope.
It requires a Bearer access token in a header; query-string tokens are never accepted.
Account identity is the boundary delivered here; resource-level authorization is #31.

An unconfigured installation disables account buttons; `/auth/me` returns 503. Partial
or invalid identity configuration fails with a sanitized configuration error. Desktop
and API must agree on the pool and client; another client's or pool's tokens are rejected.
Changing backend URLs isolates the vault entry and clears in-memory account state.

Configure the Cognito client with:

- Authorization code grant only, no implicit grant, `COGNITO` provider, no secret.
- `openid`, `email`, and `aws.cognito.signin.user.admin` scopes. The last is required
  for Cognito `GetUser` to check whether a token was revoked; it is not an AWS IAM grant.
- Callback URL `org.buddy.desktop://oauth/callback`.
- Sign-out URL `org.buddy.desktop://oauth/signed-out`. Neither route uses TCP or localhost.
- New clients use `BUDDY_COGNITO_CALLBACK_URL`, for example
  `https://<Buddy-API-host>/auth/desktop/callback`, as their Cognito redirect URI. Register
  that URL and `https://<Buddy-API-host>/auth/desktop/signed-out` on the Cognito client.
  Keep the native URLs above registered during rollout for existing installations.
- Access/ID lifetime 5 minutes; refresh lifetime 1 day; refresh rotation and revocation
  enabled, with no refresh replay grace period. Never enable `ALLOW_REFRESH_TOKEN_AUTH`
  alongside rotation. Updated refresh tokens are mandatory in Buddy's token responses.
- Email recovery and verification, user-existence error suppression, password policy,
  and an associated WAF rate limit as provisioned by #27.

The preview desktop includes the public HTTPS staging configuration from
`infra/staging/public-config.json`; no customer-owned domain or raw IP is required.
Cognito hosts login over its AWS HTTPS hostname; the API uses CloudFront HTTPS.
Environment overrides remain available for operators/development. Passwords and codes
stay in the managed browser UI, never in Buddy's widgets, API, logs or database.

For legacy native callbacks, Windows installation registers the `org.buddy.desktop`
URL protocol under HKCU, without
administrator rights and notifies Windows that protocol associations changed. macOS
packages declare it in CFBundleURLTypes and are re-signed after metadata generation,
before notarization. Windows launches a second process that
forwards the callback to the existing instance over a user-only named pipe. macOS sends
QFileOpenEvent to the application. Unix-domain IPC uses a private user-owned directory;
there is no HTTP listener. Regular source-only runs do not register themselves with the OS:
install the preview package or use the explicit Windows local launcher below.
HTTPS callbacks use the handoff below and do not need protocol registration.
Cold-start/unsolicited
callbacks never create a session; start a fresh login in Buddy after a restart.

## Browser completion and app return

### Encrypted HTTPS handoff (#104)

Desktop clients configured with an HTTPS browser callback now register a five-minute attempt
on that callback's HTTPS origin before opening managed login. The desktop generates an
ephemeral RSA-2048 private key and an independent 256-bit collection secret in memory.
Only its public key and the SHA-256 digest of the collection secret reach the API.
The OAuth state uses the versioned `buddy-v1-` prefix and fresh 256-bit randomness;
the existing independent S256 PKCE verifier still protects the code exchange.

The finish page scrubs its query immediately, fetches the attempt's public key, encrypts
the result with AES-256-GCM (state as authenticated additional data), and wraps that key
with RSA-OAEP/SHA-256. It sends only this envelope to the same-origin API. The desktop
polls every two seconds with its separate capability in an Authorization header,
decrypts the result locally, checks the exact state and exchanges the one-use code
directly with Cognito. Passwords, verification codes and session tokens never enter this
handoff. Only Buddy's existing verified `/auth/me` check establishes signed-in status.

No Windows protocol registration, app-opening browser permission, loopback listener,
local port, new hosting service or database migration is needed for this path. The
button-free page asks the user to return to Buddy manually. Browser submission failures
show a retry message; cancellation/expiry leaves the desktop signed out. Sign-out
revokes/removes credentials first, then opens Cognito logout to clear its browser session;
the desktop does not wait for an app-link acknowledgment. Complete that browser step
to clear the managed-login session too.

The API uses the existing private, KMS-encrypted data bucket through workload identity.
Handoff objects occupy only `auth-handoffs/v1/`, keyed by a hash of state; they contain
no plaintext OAuth code, private key, collection secret, password or session token.
Conditional S3 writes serialize completion and consumption across processes/restarts.
Only the authenticated collector can consume or cancel; a consumed attempt becomes
a tombstone and cannot be reused. Expiry is enforced by the API after five minutes,
independently of S3 cleanup. The scoped Terraform lifecycle expires current and historical
handoff object versions after one day and removes expired markers; AWS deletion is
asynchronous, not a promise of immediate erasure. Existing presentation retention is
unchanged. API request bodies are capped at 12 KiB, with eight concurrent storage
operations and 60 new attempts/minute per API process; deployment WAF throttling remains
required for these public pre-authentication operations. No CORS access is granted.

Deploy the reviewed API before distributing the updated desktop. Apply the reviewed
handoff lifecycle rule through the existing infrastructure process before production
acceptance. Existing S3 GetObject/PutObject and KMS permissions suffice. The API needs
the existing private storage configuration (`BUDDY_UPLOADS_ENABLED=true` plus its data
bucket/region/KMS settings); an unconfigured server returns 503 rather than fabricating
a handoff. This storage dependency is separate from the database-backed profile check.
The explicit HTTP-localhost development callback retains its legacy native return and
local launcher registration; it does not opt into remote handoff storage.
The Cognito allowlist and fixed HTTPS redirect URI stay unchanged. An older API rejects
registration before the browser opens; an older desktop continues its native protocol
path. Do not change an operator's explicit native callback configuration automatically.

Delivery is deliberately one-use: if the collection response is lost after consumption,
or the app restarts and loses its private key, start a fresh sign-in. There is no unsafe
resume of an unfinished authorization attempt. A saved completed session still uses the
OS vault and **Resume session** as before. Neither a browser finish page nor passing
automated tests proves that a real account completed signup/recovery on an installed app.

After approved API/desktop rollout, test manually:

1. Open Buddy against staging and click **Sign in**. Complete managed login. The page
   should show **Return to Buddy** with no button; returning to Buddy should show signed-in
   status, then Account should show your verified email. No app-opening prompt is needed.
2. Repeat with **Create account** and **Forgot password** using real verification/recovery
   messages. Finish browser sign-in after recovery; the same handoff must complete.
3. Cancel in Buddy before finishing the browser. A late browser return must not sign in;
   start a fresh attempt. Closing/reopening Buddy during login has the same requirement.
4. Leave an attempt unfinished for five minutes, then return: expect expiry/retry, no
   authenticated profile. Retry normally. Reloading the scrubbed page cannot repeat login.
5. Disconnect networking during the return. Expect a visible, recoverable failure; after
   reconnecting, cancel/retry if the result was consumed. No indefinite-looking wait.
6. Sign out, complete Cognito's browser sign-out, and check Account hides the old email.
   Sign in again, restart Buddy, and test **Resume session**. Repeat on installed Windows
   and macOS clients and on Chrome/Edge/Safari supported by those platforms.

### Legacy native clients

Direct native returns can open Buddy while leaving the Cognito tab looking as though it
is still loading. The new redirect first loads a real finish page on Buddy's existing API.
After the document loads, it attempts the fixed native app handoff automatically.
The page has no button and asks the user to return to Buddy and close the tab.
If the browser blocks the handoff or cannot find the OS handler, returning manually
does not deliver the code: install/register Buddy correctly and start a fresh sign-in.
The page says the browser step is complete; only Buddy can confirm the account check. Provider errors and sign-out
have their own messages, without claiming a failed login succeeded.

The page uses only its own HTML, CSS and JavaScript: no cookies, analytics, external assets
or network calls. It removes the callback query from browser history immediately, uses
no-store/no-referrer and a hash-based CSP, and excludes OAuth query parameters from the
API access-log scope. Edge/ALB logging must remain disabled or redact these parameters.
Only the one-use code/state are relayed to the native handler. PKCE verifier, passwords
and session tokens never pass through the page. The existing broker still checks state,
cancellation and single use. Ambiguous, oversized, token-bearing or unexpected parameters
do not launch the app. Reloading a scrubbed callback requires a new login from Buddy;
the page does not persist a session or code.

Deploy the reviewed API pages before shipping desktop settings that select its HTTPS
callback. The checked-in staging desktop settings select that page; they are not evidence
of deployment. Cognito allowlist additions preserve existing installed clients. An unset
callback setting retains direct native behavior for compatibility. Production uses HTTPS;
the localhost exception below is staging-only. No public bucket or new hosting service is needed.

When overriding Cognito identity settings through environment variables, an omitted
`BUDDY_COGNITO_CALLBACK_URL` uses the native callback, rather than inheriting the bundled
staging browser page. Supply your own registered browser callback explicitly to use one.
Overriding only `BUDDY_API_URL` retains the bundled identity and its matching browser page.

## Local account development (Windows)

The API/database can stay local while Cognito supplies the real managed login and
email delivery. This does not need AWS CLI credentials, create a local email service,
or change AWS infrastructure. Both processes must use the same public pool/client
configuration. Local liveness alone does not prove that identity or the database is ready.

Install the editable shared, desktop and API packages as described in development.
Configure your local PostgreSQL URL and explicitly migrate it, then start the API:

```powershell
$env:BUDDY_ENVIRONMENT = 'development'
$env:BUDDY_DATABASE_URL = 'postgresql://buddy:<password>@127.0.0.1:5432/buddy?sslmode=disable'
.\.venv\Scripts\python.exe -m buddy_api.persistence.migrate upgrade
.\.venv\Scripts\python.exe backend/scripts/run_local_accounts.py
```

In another terminal, close older Buddy windows and launch the desktop:

```powershell
.\.venv\Scripts\python.exe frontend/scripts/run_local_accounts.py
```

The local launcher selects `http://localhost:8000/auth/desktop/callback` as the browser
return page, served by the **same local API** above, plus `/auth/desktop/signed-out` for
logout. Both must be registered on the staging Cognito client. This is the explicit
development HTTP-localhost exception supported by Cognito; it is not a production URL
or a second HTTP callback listener. Restart an older local API after updating this branch.

The backend launcher requires a local database and loopback binding, and loads the
existing public Cognito configuration when not explicitly overridden. It never migrates
automatically. The desktop launcher defaults to the local API, overriding the preview's
staging default for this process, and temporarily registers the current Python environment
as the Windows callback target. Normal exit or a Python exception restores the previous
registration; a concurrent installer registration is preserved. Abrupt termination can
leave the source handler installed; reinstall Buddy to restore the packaged handler.
A source callback still requires an active single-use
state/PKCE attempt; launching an unsolicited URL does not sign in.

This registers the source-run return path without requiring an old preview executable.
Windows shell activation passes automated checks. The requester subsequently confirmed
that login worked locally. Full signup, password recovery, session renewal and macOS
acceptance still need their separate live checks.
On macOS use the packaged app's registered URL scheme. No mocked accounts or email codes
are used by either launcher. Local upload storage remains unavailable unless the real
storage service is separately configured; this account setup does not fabricate it.

Check `/api/v1/ready` before logging in. A successful browser login followed by a Buddy
error can mean an unavailable/unconfigured local API, an unmigrated database, or mismatched
pool/client configuration. A browser that cannot open Buddy usually means a missing/stale
protocol registration. Record the exact visible error and whether browser return occurred.

Stable packaging requires `--deployment-config <public-json>` with an explicitly
validated HTTPS API and Cognito target, distinct from staging. Configure the release
production environment variable `BUDDY_DESKTOP_PUBLIC_CONFIG` with those five public
settings. It contains no credentials. The existing staging-acceptance and callback
release-approval gates remain in force; native code does not establish live acceptance.

## Sessions and failure behavior

Sign-in uses a fresh random state and S256 PKCE verifier. The native callback broker checks
the exact URI, single state and exactly one code/error; invalid requests do not complete login. It waits at most
five minutes and is cancellable, including on window close. Only the single-use OAuth
code travels through the callback URL; access, ID and refresh tokens never do. No callback URL or code is logged or written to disk. Remote requests reject
redirects and oversized responses. ID tokens are discarded, not used as identity proof.

Only refresh tokens persist, through the explicit Windows Credential Manager or macOS
Keychain implementation. There is no plaintext or third-party keyring fallback. Each
entry is isolated by backend, issuer and client. Access tokens live only in process
memory. After restart, **Resume session** rotates the saved refresh token and revalidates
the account. While signed in, the account check runs each minute and renews tokens before
expiry; failure removes signed-in state and exposes a recoverable message. An expired
refresh token requires a new browser sign-in.

Windows Credential Manager limits each credential blob to 2,560 bytes. Buddy stores
refresh tokens in bounded vault entries (512 characters each, at most 32 pieces),
with a versioned manifest and integrity check in the vault. Existing single-entry
sessions remain readable and are converted when next saved. macOS keeps its existing
single Keychain entry. No token or manifest is written to a plaintext file.

Saving first marks the vault session as incomplete, writes the pieces, removes unused
pieces from the previous session, and only then publishes the complete manifest. Missing,
changed, or partially written pieces cannot be resumed. A save failure blocks resume and
attempts cleanup; sign-out attempts every bounded slot even if metadata is unavailable
or one deletion fails. The existing non-secret sign-out marker remains the restart guard.

Sign-out clears memory, records a non-secret local tombstone before attempting vault
deletion, revokes the refresh token online and clears the Cognito browser session. The
tombstone prevents restoring a saved token when credential deletion fails; only a fresh
successful sign-in removes it. Failure is shown rather than reported as success. Failed
online revocation can be retried while the app remains open; after closing, a token that
could not be revoked might remain valid remotely until expiry. This requires operator
revocation if a device is lost. If both local storage and network fail, close Buddy and
repair the credential store/revoke the session before relying on sign-out.

The API validates RS256 signature, fixed issuer, app client, token type, expiry and subject.
It then makes an uncached Cognito `GetUser` request and requires a matching subject,
nonempty email and exactly `email_verified="true"` from provider attributes (never client
profile or access-token email claims). Missing, duplicate, malformed or unverified
attributes deny access before user bootstrap, resource reads/writes or subscriptions.
This applies through the shared principal boundary to every private API.
The live provider check is required because offline JWT verification alone cannot
detect Cognito revocation. Provider/network failure denies account access (503). Missing,
invalid and revoked credentials produce 401. A bounded limiter permits 30 bootstrap checks per
credential digest per minute per API process, with 429/Retry-After. Deployment WAF provides the
shared per-client-IP limit and protects managed login. Do not enable arbitrary forwarded
headers. Neither limiter
is a substitute for provider-level login throttling.

## Verification and recovery controls

Terraform explicitly selects CONFIRM_WITH_CODE and verified-email recovery, with
user-existence suppression on the app client. Cognito issues and validates the six-digit
email codes, their expiration, replay/attempt limits, resend throttling and password
policy; Buddy does not duplicate code storage or promise a locally enforced timer.
The managed pages own wrong/expired-code, resend, recovery and delivery-error feedback.
Reset finishes in the browser, then login uses the new password and a fresh PKCE attempt.
The app's five-minute callback timeout/cancellation can require restarting the browser
flow; it does not change Cognito's code lifetime. Service delivery/attempt quotas can
produce recoverable throttling and must be exercised against the actual staging pool.

Staging currently uses COGNITO_DEFAULT email delivery, not an approved SES sender. SES
production access is not enabled. Before production, configure an approved SES identity,
permissions and appropriate quotas and rerun real delivery acceptance. Do not silently
assume that a default sender or mocked email response proves production deliverability.


## Validation and deployment limits

### Windows staging sign-in and resume check

From PowerShell in the repository root, load the checked-in public staging settings
for this process and launch Buddy:

```powershell
$config = Get-Content .\infra\staging\public-config.json -Raw | ConvertFrom-Json
foreach ($property in $config.PSObject.Properties) {
    [Environment]::SetEnvironmentVariable($property.Name, $property.Value, 'Process')
}
.\.venv\Scripts\python.exe -m buddy_desktop
```

1. Close any older Buddy window first so it does not keep the previous login code loaded.
2. On Home, click **Check connection**; expect **Backend connected**.
3. Click **Sign in**, complete the browser login, and return to Buddy; expect
   **Signed in to Buddy**, with no credential-store error.
4. Close Buddy, launch it again from the same PowerShell session, then click
   **Resume session**; expect **Signed in to Buddy**. This verifies saved-session
   loading, refresh-token rotation, and authenticated API access.
5. Click **Sign out**, complete any browser handoff, restart Buddy and try
   **Resume session**; expect no saved session. A new browser sign-in is required.

Do not copy token values from Credential Manager or include them in screenshots or logs.
The native Windows regression test uses random isolated vault entries with synthetic
values, verifies long-token save/read/rotation, and cleans those entries afterward:

```powershell
.\.venv\Scripts\python.exe -m pytest frontend/tests/test_native_token_store.py -q
```

### Automated coverage

`python -m pytest` covers signed JWT boundaries, online revocation, browser state/PKCE,
recovery callback rejection, restart/renewal, vault failure and sign-out retries. Tests use
local HTTP and deterministic provider responses, never real passwords or paid providers.
The existing PostgreSQL CI job also exercises persisted account IDs across API restarts.
Run `python scripts/quality.py all check`, `python -m mypy`, schema compatibility, build,
and both startup smoke checks before review.

Issue #73 provisioned staging; its retained evidence did not exercise signup email
delivery. Issue #74 staging acceptance must verify actual signup/verification,
invalid credentials, valid/invalid recovery codes, native vault behavior, refresh rotation,
online sign-out, and denial of the revoked access token. Passing local tests does not
replace that live provider acceptance or installed OS callback integration.

References: [Cognito authorization/PKCE](https://docs.aws.amazon.com/cognito/latest/developerguide/authorization-endpoint.html),
[token rotation](https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-using-the-refresh-token.html),
[revocation](https://docs.aws.amazon.com/cognito/latest/developerguide/token-revocation.html),
[GetUser](https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_GetUser.html).


## Issue #74 acceptance record

See [account acceptance](account-acceptance.md) for the current observed results and
remaining live checks. `frontend/scripts/staging_accounts.py` is an opt-in Windows
operator helper using the real shell, OS vault, staging API and installed URL handler.
It prints a public authorization URL for opening in a controlled browser; passwords,
verification codes, PKCE verifiers and session tokens are never printed. Close any
other native-callback Buddy instance first. macOS acceptance must use the installed
.app, because OS URL dispatch targets its registered bundle.


## Read-only account profile (#75)

Open the top-right **Account** button (keyboard: Tab then Space, or Workspace > Account
profile / Alt+5). The existing sidebar stays unchanged. A signed-in account sees its
email and **Email verified**. Signed-out users get a link to the existing account controls
on Home; the same controls provide Sign in, Sign out and Forgot password. No editing,
avatars or administrative controls are added.

Refresh profile makes one normal authenticated `/api/v1/auth/me` request through the
existing account background queue, serialized with sign-in, refresh and session checks.
The API reuses its uncached Cognito GetUser verification to return only the verified
email and status alongside the durable UUID. It does not persist another copy of the
email or trust token email claims, query parameters or client-selected account IDs.
The response remains no-store. Additive fields are optional in the compatibility schema;
this API always supplies verified values after successful authorization. An older API
that returns only the UUID still supports sign-in, but the new page reports profile
unavailable until that API is upgraded.

Leaving the page, signing out, changing accounts/backends or closing the window clears
the displayed profile. Late results cannot refill a hidden page or restore an expired
account. Network/provider failures clear profile data and offer retry without pretending
the session was revoked; an authentication failure clears signed-in state and directs
the user to sign in again. The page does not save email in preferences or the OS vault.

### Manual profile test

Before merge, use the updated local API **and** desktop on this issue branch. Follow
Local account development above to install dependencies, configure/migrate a disposable
local database, and launch `backend/scripts/run_local_accounts.py` in one terminal and
`frontend/scripts/run_local_accounts.py` in another. The database and Cognito must be
reachable; there are no fake accounts. Do not deploy the implementation branch.

After approval/merge, main's automatic CD deploys the API to staging. Wait for a successful
Backend promotion and rollback run, pull main, install the editable packages from the
Development guide, close older Buddy windows, then launch the updated source app with
these public staging settings from PowerShell in the repository root:

```powershell
$config = Get-Content .\infra\staging\public-config.json -Raw | ConvertFrom-Json
foreach ($property in $config.PSObject.Properties) {
    [Environment]::SetEnvironmentVariable($property.Name, $property.Value, 'Process')
}
.\.venv\Scripts\python.exe frontend/scripts/run_local_accounts.py
```

1. Before signing in, click Account: expect sign-in guidance and no email. Use Account
   controls to reach Home, then complete real sign-in or Resume session.
2. Click Account: briefly expect Loading profile, then your own email and Email verified.
   Refresh profile should show the same account and keep navigation responsive.
   Also start sign-in or Resume session and open Account before it finishes: expect
   Checking account, then the profile to appear automatically after success, without
   clicking Refresh. Failed/cancelled sign-in must leave no email; navigating away
   during sign-in must not refill the hidden profile.
3. Resize to 800x600 and a larger size; use Tab/Space and Alt+5. Expect readable text,
   visible controls and no horizontal scrolling. Account controls must return to Home.
4. Leave the page and reopen it: a fresh request loads the profile. Sign out through
   Account controls, then reopen Profile: no old email should remain. Sign in with a
   different account and confirm only that account's email appears.
5. Disconnect networking and refresh: expect Profile unavailable and no cached email.
   Reconnect and retry. For a revoked/expired session, expect sign-in guidance; a normal
   short access-token expiry may rotate a valid refresh token automatically.
6. With a local configurable backend (no BUDDY_API_URL environment override), change
   Settings to another backend: old profile data must disappear. Backend changes wait
   until the bounded current account request ends. Start a request and navigate away
   or close Buddy; a late response must not restore private text.

No new AWS resource, migration or Cognito setting is needed. Native browser-handler
problems are separate from this profile feature; returning manually from a blocked
browser handoff does not authenticate the desktop.
