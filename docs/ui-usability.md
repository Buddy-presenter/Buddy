# Desktop usability revision


The first native review exposed competing calls to action and misplaced system controls.
This revision keeps Widgets/QSS and all existing account, upload, library and presenter
services. It reorganizes their existing controls rather than adding a second workflow.

- Home is a calm starting point with two destinations: the private Library and local
  presentation workspace. It contains no account console, backend URL or connection check.
- The top-right account control opens an anchored, keyboard-accessible popover. Signed-out
  users see sign-in, registration, password recovery and saved-session recovery. Signed-in
  users see their profile and sign-out. Cancellation is visible during browser sign-in.
  Verified profile email/identity is cleared on account changes, expiry and backend changes.
- Settings owns service diagnostics, the configured endpoint and connection checks, alongside
  the existing reduced-motion preference. Health checks retain the same background runner.
- Signed-out Library has a direct sign-in action instead of disabled search/import/paging
  controls. Signed-in Library keeps the existing file picker, search, sorting and pagination; presentation
  actions sit with selected-deck details. No synthetic deck data or job progress is added.
- The header names the current route, so pages do not repeat a title or tagline. Copy that
  restates the obvious (sidebar "Workspace"/"Local workspace" labels, success chatter such as
  "Ready.") is removed; status lines appear only when they carry progress or an error.
- Signed out, the header shows a **Sign in** button; signed in, it becomes an avatar with the
  verified email's initial. Its menu shows the email, **Profile** and **Sign out**. Password
  recovery and **Use saved sign-in** (the existing resume of a session stored on this device)
  are secondary links under Sign in and Create account. On Windows the one-item menu bar is
  hidden; its keyboard shortcuts stay on the window, and macOS keeps the native menu.
- Type uses Segoe UI Variable on Windows 11 (Segoe UI elsewhere on Windows; the system font on
  macOS), unhinted and antialiased. Weights use named families because Qt renders variable
  weights as faux bold. `apply_theme` resolves the font placeholders in `theme.qss`.
- Inspect all routes and the account popover at 800x600 and 1200x800, including keyboard
  dismissal, sign-in cancellation, expiry and account/backend changes. Verify existing PDF
  and private-upload regressions as well as rendered evidence before review.

Research references: [Qt UI technology comparison](https://doc.qt.io/qt-6/topics-ui.html)
confirms that both Widgets and Quick support custom desktop interfaces; changing runtimes
is a migration rather than a layout fix. Microsoft's
[Windows App account interaction](https://learn.microsoft.com/en-us/windows-app/user-account-settings-add-remove-manage)
uses a profile entry point for account management. The local Apple Design guidance informs
hierarchy, restrained motion, anchored popovers and predictable feedback.
