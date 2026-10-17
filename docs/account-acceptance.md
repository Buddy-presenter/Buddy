# Verified account acceptance (#74)

This record separates automated regressions, actual staging configuration and user-assisted
acceptance. Pending entries are not proof of issue completion or production readiness.
No passwords, email codes, refresh/access tokens or private mailbox address belongs here.

| Check | Observed result |
| --- | --- |
| Full local suite with disposable PostgreSQL | 615 passed; 5 existing optional corpus/symlink skips. |
| Additional production-config regression | Passed; stable builds reject implicit staging, local HTTP and extra settings. |
| Formatting, lint, strict typing, schema compatibility | Passed locally. |
| Real Qt callback forwarding from another process | Passed on Windows; single-use state/code and cancellation/replay rejection covered. |
| macOS FileOpen URL event boundary | Passed as a Qt event regression; installed macOS provider acceptance remains pending. |
| Windows preview build and actual installer/frozen startup smoke | Passed; preview installed per user with quoted protocol command. |
| Staging Cognito client callback update | Applied: org.buddy.desktop://oauth/callback and org.buddy.desktop://oauth/signed-out. |
| Unrelated Cognito client settings | Before/after comparison changed only CallbackURLs, LogoutURLs and LastModifiedDate. |
| Pool verification/recovery | Read from AWS: CONFIRM_WITH_CODE, automatic email verification, verified_email recovery. |
| Email sender | COGNITO_DEFAULT; SES production access false. Approved production sender/quota acceptance pending. |
| Real signup email delivery/verification | Pending user-assisted browser completion. |
| Real recovery delivery/wrong/expired/reused code and subsequent login | Pending user-assisted browser completion. |
| Installed browser-to-app PKCE return and native vault restart | Pending user-assisted browser completion. |
| Newly implemented backend deployed | Not deployed. Existing deployment policy requires successful exact-main checks and reviewed release; PR acceptance is separate. |
| Real deployed denial of an unverified identity on every private boundary | Pending new-backend deployment; signed-token/private-route bypass regressions pass locally. |

## Live procedure

1. Install the Windows preview or macOS .app. The preview has public staging HTTPS
   settings; no localhost callback, DNS ownership or raw-IP certificate workaround is needed.
2. Start Buddy, choose Create account, and enter the test email/password directly in
   Cognito. Confirm that only that mailbox receives the six-digit code. Wrong/expired
   codes and resend throttling must remain recoverable without granting app access.
3. Complete email verification and browser sign-in. Confirm the OS returns the code to
   the running app, which exchanges it with its original PKCE verifier and verifies the
   account against the staging API. Confirm Signed in to Buddy.
4. Restart Buddy and Resume session to exercise the real OS vault and token rotation.
5. Sign out, choose Recover account, and enter the mailbox. Verify wrong/expired/reused
   codes are rejected, choose a policy-compliant new password directly in Cognito, then
   log in with the new password and verify the old password is rejected.
6. After the reviewed backend is deployed, exercise the verified-email trust boundary
   with a disposable unverified identity and real tokens where Cognito permits them.
   Denial at the signup page alone does not prove private API enforcement.
7. Record sanitized outcomes and tested revision, without credentials or mailbox contents.

Cognito owns code lifetimes, attempt limits, resend throttling and managed-page feedback.
Do not describe an app-local timeout as an email-code expiry guarantee. Staging's default
sender and SES sandbox do not establish production deliverability. Production signing,
installed callback acceptance on both supported systems and explicit release approval
remain separate release gates.
