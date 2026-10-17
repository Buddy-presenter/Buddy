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
same UUID. `/api/v1/auth/me` returns only that UUID in the existing request-ID envelope.
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

Windows installation registers the `org.buddy.desktop` URL protocol under HKCU, without
administrator rights and notifies Windows that protocol associations changed. macOS
packages declare it in CFBundleURLTypes and are re-signed after metadata generation,
before notarization. Windows launches a second process that
forwards the callback to the existing instance over a user-only named pipe. macOS sends
QFileOpenEvent to the application. Unix-domain IPC uses a private user-owned directory;
there is no HTTP listener. Regular source-only runs do not register themselves with the OS:
install the preview package or use the explicit Windows local launcher below.
Cold-start/unsolicited
callbacks never create a session; start a fresh login in Buddy after a restart.

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
