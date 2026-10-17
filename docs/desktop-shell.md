# Desktop shell boundaries

The application has one `QApplication` and one event loop. `app.py` validates environment
configuration before constructing Qt, loads native preferences, and owns shutdown.
`MainWindow` manages routes and app lifecycle; `views.py` contains reusable pages and
consistent status presentation. Domain features should add views and typed adapters
without introducing another event loop or running I/O inside widget callbacks.

`HealthService` is a real synchronous HTTP adapter with sanitized errors and a response
size limit. `TaskRunner` dispatches it on a private one-worker `QThreadPool`. Worker
signals reach receiver slots on the GUI thread; only those slots mutate widgets.
There is at most one in-flight task, and operation IDs prevent unrelated/stale results
from changing the current view. Expected service errors are safe to display. Unexpected
exceptions become a generic recovery message without logging provider payloads.

When the user closes during a request, the window ignores the first close event, stops
accepting new work, and lets the event loop finish delivering worker completion. The
window then saves geometry and closes. A final pool join occurs only after `app.exec()`
returns; no blocking joins run in the GUI event loop. HTTP timeouts bound socket waits,
not a total wall-clock deadline against a deliberately trickling response. The service
must remain a trusted configured backend; general-purpose provider work will need its
own cancellation/deadline strategy.

Settings validate URLs before persistence and never contain secrets. Native `QSettings`
preferences survive restarts; malformed saved addresses fall back to the validated
default. An explicit environment URL always wins. HTTP is restricted to loopback,
redirects are disabled, and service bodies are never displayed as markup or error text.

The shell includes account controls (#5) and the [deck library](library.md) (#6).
It leaves upload/processing (#7–#12),
presentation (#13 onward), and the security baseline (#46) to their owners. This does
not claim those workflows are implemented. The included CI is the minimal desktop
foundation required by #1; #30 owns broader desktop/API security and CI policy.

Platform references:

- [Qt stacked views](https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QStackedWidget.html)
- [Qt thread pools](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QThreadPool.html)
- [Qt high-DPI behavior](https://doc.qt.io/qtforpython-6/overviews/qtdoc-highdpi.html)
- [Qt deployment](https://doc.qt.io/qtforpython-6/deployment/index.html)

Use this wheel as the input to the future release pipeline. It does not package an API,
provider secrets, or an embedded browser.

## Visual system (#90)

`visuals.py` installs the packaged charcoal/warm-white palette and QSS once per
application. Coral/amber is reserved for primary commands, navigation, and focus;
mint, red, and lavender distinguish processing statuses alongside their text labels.
Lucide icons and the original transparent Buddy character ship inside the wheel;
rendering never downloads assets. Attribution/provenance lives in `assets/README.md`.

The shell has persistent navigation, a connection-status header, responsive Home
account/connection sections, a searchable table library, and the existing local
rendered-slide Present controls. Home shortcuts navigate to real views. Present's
numeric region fields are available through the checkable Coordinates control;
drag selection and all six executor commands retain the same contracts. This is
not native PowerPoint playback, an agent, a microphone, or an upload UI.

Settings stores `appearance/reduce_motion` in native `QSettings`. This suppresses
the mascot activity arc's animation, not actual work. Hidden mascots stop their
timers; the header activity indicator reflects concurrent account/library/health
work without implying that Buddy is listening. Connection health is the last
explicit check, not continuous connectivity monitoring.

Validate at 800x600 and 1200x800, including high-DPI scaling, keyboard focus,
signed-out/loading/error states and loaded slides. `test_visuals.py` covers asset
loading, contrast, responsive geometry, motion persistence and command access.
`frontend/scripts/smoke_desktop.py --screenshot build/buddy.png` captures the real
Qt application in an isolated settings profile, without provider calls.
