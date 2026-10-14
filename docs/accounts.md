# Account lifecycle (#5)

Buddy uses an Amazon Cognito user pool and a public desktop app client. Account creation,
email verification, password validation, recovery codes and recovery errors are handled
by Cognito's managed login in the system browser. Buddy never collects a password.
The Home page offers Sign in, Create account, Recover account, Resume session and Sign out.
Recovery finishes in the browser; continue signing in there to return to Buddy.

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
- Callback and sign-out URL `http://localhost:8765/callback`.
- Access/ID lifetime 5 minutes; refresh lifetime 1 day; refresh rotation and revocation
  enabled, with no refresh replay grace period. Never enable `ALLOW_REFRESH_TOKEN_AUTH`
  alongside rotation. Updated refresh tokens are mandatory in Buddy's token responses.
- Email recovery and verification, user-existence error suppression, password policy,
  and an associated WAF rate limit as provisioned by #27.

AWS documents HTTP localhost callbacks as an exception intended for testing. This
checkout uses that supported development flow with a listener bound only to 127.0.0.1.
A production installer needs the team's reviewed native callback registration or HTTPS
handoff before release (#38); this PR does not claim that installer integration exists.

## Sessions and failure behavior

Sign-in uses a fresh random state and S256 PKCE verifier. The loopback listener checks
the exact path, Host and state; invalid requests do not complete login. It waits at most
five minutes and is cancellable, including on window close. Only the single-use OAuth
code travels through the callback URL; access, ID and refresh tokens never do. Callback
logs are disabled, pages have no-store/no-referrer headers, and remote requests reject
redirects and oversized responses. ID tokens are discarded, not used as identity proof.

Only refresh tokens persist, through the explicit Windows Credential Manager or macOS
Keychain implementation. There is no plaintext or third-party keyring fallback. Each
entry is isolated by backend, issuer and client. Access tokens live only in process
memory. After restart, **Resume session** rotates the saved refresh token and revalidates
the account. While signed in, the account check runs each minute and renews tokens before
expiry; failure removes signed-in state and exposes a recoverable message. An expired
refresh token requires a new browser sign-in.

Sign-out clears memory, records a non-secret local tombstone before attempting vault
deletion, revokes the refresh token online and clears the Cognito browser session. The
tombstone prevents restoring a saved token when credential deletion fails; only a fresh
successful sign-in removes it. Failure is shown rather than reported as success. Failed
online revocation can be retried while the app remains open; after closing, a token that
could not be revoked might remain valid remotely until expiry. This requires operator
revocation if a device is lost. If both local storage and network fail, close Buddy and
repair the credential store/revoke the session before relying on sign-out.

The API validates RS256 signature, fixed issuer, app client, token type, expiry and subject.
It then makes an uncached Cognito `GetUser` request: offline JWT verification alone cannot
detect Cognito revocation. Provider/network failure denies account access (503). Missing,
invalid and revoked credentials produce 401. A bounded limiter permits 30 checks per
direct peer per minute per API process, with 429/Retry-After. Deployment WAF provides the
shared per-client-IP limit and protects managed login. Do not enable arbitrary forwarded
headers; the local limit can also constrain users behind the same proxy. Neither limiter
is a substitute for provider-level login throttling.

## Validation and deployment limits

`python -m pytest` covers signed JWT boundaries, online revocation, browser state/PKCE,
recovery callback rejection, restart/renewal, vault failure and sign-out retries. Tests use
local HTTP and deterministic provider responses, never real passwords or paid providers.
The existing PostgreSQL CI job also exercises persisted account IDs across API restarts.
Run `python scripts/quality.py all check`, `python -m mypy`, schema compatibility, build,
and both startup smoke checks before review.

No AWS account, pool, credentials or domains were available while implementing this PR.
After #27 is provisioned, staging acceptance must verify actual signup/verification,
invalid credentials, valid/invalid recovery codes, native vault behavior, refresh rotation,
online sign-out, and denial of the revoked access token. Passing local tests does not
replace that live provider acceptance or production callback integration.

References: [Cognito authorization/PKCE](https://docs.aws.amazon.com/cognito/latest/developerguide/authorization-endpoint.html),
[token rotation](https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-using-the-refresh-token.html),
[revocation](https://docs.aws.amazon.com/cognito/latest/developerguide/token-revocation.html),
[GetUser](https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_GetUser.html).
