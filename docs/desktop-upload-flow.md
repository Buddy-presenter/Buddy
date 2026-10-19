# Desktop upload progress and recovery (#37)

Library → **Upload PDF or PPTX…** uses the native file picker and the existing private
upload API. Choose PDF for presentation. PPTX is preserved for storage only; export it
to PDF yourself to present it. Original file bytes are sent unchanged. No worker,
analysis job, content cleaning or new privacy gate is introduced.

The upload panel distinguishes **Preparing file**, **Checking saved upload**,
**Uploading**, and **Validating and saving**. The determinate bar measures bytes passed
to the network transport, not server receipt or processing. Sending every byte is not
confirmation that the deck was saved. The validation/save stage has no invented percentage.
Only the API's completed reservation confirms success, followed by a real library refresh.
The deck remains **Awaiting processing** until a separate processing workflow exists.
PDF playback does not depend on that workflow.

**Cancel upload** stops local preparation, transfer or waiting for the response. It
does not delete a deck the server already committed. **Retry upload** checks/reuses
the same reservation; receiving/cleanup states are polled with Retry-After before
another transfer is allowed. Transient failures resend the complete file, not a byte
range. Lost responses and cancellation keep the retry identity for this account session;
confirmed success retires it, so deliberately selecting the same file again creates
a separate deck. Keep the same unchanged file when recovering an uncertain transfer.
Retry identities are in memory, not durable across app restarts or account/backend changes;
after reopening, refresh Library and check for the deck before uploading again.

Sign-out, backend/account changes and shutdown cancel active work and clear private UI
state. Navigating to another workspace remains available and does not cancel the upload.
Returning to Library refreshes real saved state. Network requests have bounded timeouts;
waiting for a reply is cancellable, including on Windows. Initial DNS/connect/TLS setup
can still take up to the configured request timeout. Local selection is capped at 64 MiB;
the server can enforce smaller limits and validates malformed/encrypted/unsupported content.

## Run and test manually

From a checkout of this branch, use the supported Python environment prepared by the
[development guide](development.md). Close an older Buddy instance first. In PowerShell
at the repository root, load the checked-in public staging settings and launch:

```powershell
$config = Get-Content infra/staging/public-config.json -Raw | ConvertFrom-Json
foreach ($setting in $config.PSObject.Properties) {
    [Environment]::SetEnvironmentVariable($setting.Name, [string]$setting.Value, 'Process')
}
.\.venv\Scripts\python.exe -m buddy_desktop
```

On the current development computer, if `.venv` uses an unsupported Python version,
use the already-prepared `build\pr91-runtime\Scripts\python.exe` instead. This runs the
branch's desktop against the deployed staging API; it does not deploy the branch.
Uploads create real private staging decks. Use files you intend to store there.

1. Sign in on Home, then open Library. Choose a small PDF. Expect phase feedback,
   followed by one real **Awaiting processing** deck. Select it and press **Present PDF**;
   the original pages should open before analysis is ready.
2. Upload a larger PDF (under 64 MiB) to make transfer feedback easier to see. Move
   between Home and Library during upload; the window must stay responsive. Fast local
   transfers may pass through the byte-progress stage too quickly to observe.
3. During an upload, press **Cancel upload**, then **Retry upload**. If the server already
   saved the deck, retry must discover it without creating a second one. Otherwise retry
   sends the same file again after server cleanup. A settling reservation may take time.
4. Disconnect the network during transfer, restore it, and press **Retry upload**. Confirm
   there is one saved deck for that attempt. Once success is confirmed, deliberately
   selecting the same PDF again should create a separate deck.
5. Try an empty PDF, a damaged file named `.pdf`, and a file over 64 MiB. Expect clear
   errors and no saved deck. File-picker filtering is convenience; server content validation
   is authoritative. A normal PPTX should save but explain that processing/playback is
   unsupported; **Present PDF** stays disabled for it.
6. Start another upload, navigate Home and sign out. Library must clear private state;
   another account must not see the previous account's deck or upload result. Also close
   the app during upload and confirm it exits without freezing. Cancellation cannot undo
   an already accepted deck, so check Library after signing back in.
7. Resize to 800×600. Tab through upload, Cancel and Retry controls; use Space to activate
   them. Controls and status must remain readable/reachable using the existing page scroll.

Automated checks additionally reproduce a dropped response after a real PostgreSQL deck
commit and check that retry uses the same reservation with only one storage write.
Real account login and staging acceptance still need the human checks above.
