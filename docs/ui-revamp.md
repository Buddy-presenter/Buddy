# Buddy desktop experience (#112)

## Direction

Buddy is a companion for the moments before and during a presentation. Preserve
the original orange character and the existing charcoal/coral/amber palette.
The interface should feel like a quiet presentation workbench: a clear next action,
generous slide imagery, warm highlights used with purpose and consistent native
controls. The mascot is a character with a job, not background decoration.

The requester chose **expressive but restrained** motion and **UI first**, with
actual PPTX preview rendering tracked separately. The visual guidelines and local
Apple Design skill remain authoritative. Preserve platform fonts, keyboard control,
readable contrast and the existing account/security boundaries.

## Tools and dependency decisions

| Need | Choice | Reason |
| --- | --- | --- |
| UI | Existing PySide6 Widgets and QSS | Native behavior, existing tested controls and packaging. |
| Mascot | Original logo plus separate `buddy-body.png` animation layer, QPainter | Preserve the identity; independent eyes and native rendering. No asset service or web runtime. |
| Motion | Interruptible current-value transitions, bounded activity timing | Immediate response, no input lockout; persist Reduce motion. |
| Icons | Existing local Lucide SVGs (ISC) | One consistent icon family, offline rendering. |
| PDF images | Existing isolated PDFium renderer | Real slide appearance, existing resource/privacy boundaries. |
| Verification | pytest-qt, real PDF fixtures, native Windows captures, CI | Check interaction and rendered layout together. |
| Design guidance | Local Apple Design; researched Impeccable critique principles | Apply hierarchy, restraint and product-specific identity to Qt. No new skill runtime required. |

No QFluentWidgets adoption: replacing the visual language and dependency stack
is unnecessary, and its distribution terms require a separate adoption decision.
No new renderer or provider belongs in this UI change.

## Delivery sequence

1. Audit the existing five routes and capture the current shell. Keep functional
   account, upload, library and presenter behavior intact.
2. Refine surfaces, type hierarchy, spacing, page headers and the shell. Home leads
   to the library and local presentation; technical connection details stay secondary.
3. Give Buddy independent glossy eyes, natural blinking, pointer gaze, click/keyboard
   greetings and smooth wandering. Direct dragging preserves the grab position and
   releases into bounded motion. Idle, attention, working, success, error and quiet
   presenting states use real events. Stop animation when hidden/inactive and honor
   reduced motion everywhere.
4. Introduce a persistent import surface that becomes a named transfer card.
   File picker and drag/drop share authentication, upload, cancellation, retry and
   idempotency logic. Accept one local PDF/PPTX; reject unsupported/multiple/remote
   inputs with actionable feedback. Window drops route to Library.
5. Reveal a bounded strip of real PDF slide images as the existing isolated renderer
   returns them. Images indicate local preview availability, never analysis success.
   Clear filenames/images and invalidate late work on account/backend changes.
6. Verify keyboard, focus, 800x600/1200x800 layouts, long filenames, cancel/retry,
   hidden/shutdown lifecycle and real-PDF rendering; run repository checks and builds.
   Open one review PR; do not merge without explicit requester instruction.

## State and interaction rules

The live companion occupies a dedicated 116-logical-pixel space below the workspace,
across every route. It cannot cover forms, commands or slides. Movement uses a
critically damped spring with a moving waypoint that limits normal roaming to about
128 logical pixels/second. New targets preserve current position and velocity.
Buddy pauses his trip when hovered, looks toward the pointer within the app,
blinks periodically, and greets clicks/Space/Enter with a short happy expression.
Dragging directly repositions him inside this area; it does not intercept app input.

**Let Buddy rest** pauses roaming and blinking; clicking him wakes him. Settings
persist **Show Buddy companion**, **Let Buddy wander**, rest and reduced-motion
preferences. Reduced motion stops automatic movement, blinking and spring timers;
direct input and static status feedback remain. Hidden, inactive, minimized and
closing windows stop the animation timer. Full-screen presentation is a separate
window; Buddy stays outside it and the workspace character rests while presenting.

- Drag hover: warm boundary, explicit release instruction and Buddy's attention.
- Transfer: filename, source format and actual byte/stage progress; indeterminate
  for preparation/validation. Cancel remains available. No timed fake percentage.
- Success: only after the service confirms a saved deck. Keep it visible when the
  library refreshes. Saving a PPTX does not mean it can be played or rendered.
- Failure: actionable text plus retry using the existing reservation key where
  appropriate. An uncertain cancellation does not claim the server discarded it.
- Presentation: Buddy stays quiet; there is no listening or AI state before those
  capabilities exist. Completion/error reactions never obscure slide controls.
- Private state: source images are memory-only; changing identity/backend or
  signing out clears the import card and rejects stale signals.

## Prerequisites and remaining capabilities

Merged shell/library/assets/upload/PDF/account work supplies the UI foundation.
The Project's Sprint/Wave fields govern execution. #8 (PDF workers, changes
requested at planning time), #12 (analysis), #23 (review) and #24 (integrated
processing) retain their owners and order. This change neither implements those
features nor invents progress for them. PPTX preview rendering is tracked in #113;
native PowerPoint controls remain #108–#110. Progressive PPTX thumbnails can only
ship after the rendering prerequisite is reviewed and available.

## Manual acceptance

For an honest preview without a backend, run from the repository root:

```powershell
.venv/Scripts/python.exe frontend/scripts/preview_companion.py
```

This opens the actual desktop UI with disposable settings and no configured account.
No account, saved deck, transfer or processing results are fabricated. Buddy visibly
starts a gentle trip after about 1.4 seconds in the active window. Move the pointer
near his space, hover him, click/press Space or Enter, drag him, then try resting,
hiding, reduced motion, changing routes and switching to another application.

Use Buddy-authored fixtures, not private user files, in screenshots. Check every
route at 800x600 and 1200x800. Drag a PDF/PPTX over the window while signed out,
signed in, and busy; verify picker equivalence, rejection guidance, cancellation,
retry and keyboard access. Sign out/change backend during preview work and confirm
that no filename/image or late result reappears. Turn Reduce motion on during
activity and restart to check persistence. Open a real PDF and exercise fullscreen,
navigation, selection, zoom and highlight. Real staging upload and macOS acceptance
must be distinguished from local boundary tests and CI results.
