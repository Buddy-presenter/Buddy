# Desktop PowerPoint launch and connection (#108)

Buddy's **Present** page can open a local `.pptx` in desktop Microsoft PowerPoint on
Windows and start its slideshow, or connect to a slideshow the user already started.
PowerPoint owns rendering and playback; Buddy does not convert the source or draw slides
in its PDF surface. The connection from #108 and controls from #116 identify the current
deck/slide, navigate, and add or remove Buddy annotations. Region zoom remains an
investigation in #111.

Saved PPTX analysis, review and explicit execution of validated suggestions are described
in [PowerPoint analysis](powerpoint-analysis.md). Those suggestions use a separate
PowerPoint-only catalog and never enter the PDF executor.

## User flow and lifecycle

- **Open PowerPoint…** asks for an existing `.pptx`, opens it read-only in a new desktop
  PowerPoint session, and starts the slideshow in manual-advance mode so timed transitions
  do not race through the deck before the presenter can control it. This setting applies to
  the read-only session; the source file is not saved or changed. Buddy reports the connected
  filename and slide position.
- **Connect to slideshow…** lists currently running PowerPoint slideshow windows. The user
  explicitly selects the intended presentation; the chooser includes the full path to
  distinguish files with the same name. Buddy attaches only when exactly one active show
  matches that selected path.
- Buddy checks the selected slideshow every 0.5 seconds. If it closes, Buddy reports the
  disconnected state and stops watching. Reconnecting requires an explicit selection, so
  a different deck is never adopted automatically.
- **Disconnect** stops Buddy's tracking only. It does not close PowerPoint or the
  slideshow. Closing Buddy also leaves PowerPoint and its presentation open.
- On macOS/Linux the PowerPoint controls are unavailable; local and library PDF playback
  continue to work as before.

PowerPoint automation uses Windows COM. The desktop package installs `pywin32` only on
Windows and includes its COM entry points in the frozen Windows build. COM interfaces
remain inside background service calls; widgets receive only plain presentation-state
values. Paths and provider/COM exception details are not logged or surfaced as raw errors.

## Manual Windows smoke test

From the repository root, install the desktop development package and start Buddy:

```powershell
python -m pip install -e .\shared
python -m pip install -e ".\frontend[dev]"
python -m buddy_desktop
```

1. Open **Present** from Buddy's sidebar (or press `Alt+3`). Select **Open PowerPoint…**
   and choose `tests/fixtures/slides/Buddy_Extraction_Geometry.pptx`.
2. Confirm desktop PowerPoint opens the file in read-only mode and begins its slideshow.
   Buddy should show the selected filename and “slide 1 of 2”.
3. Advance one slide from PowerPoint itself. Buddy should refresh the current slide within
   about one second. Use the separate **PowerPoint controls** below the connection status
   to navigate and annotate the connected show.
4. Press `Esc` in PowerPoint to end the slideshow. Buddy should report a disconnection.
   Start the slideshow again and use **Connect to slideshow…** to explicitly reconnect.
5. With more than one slideshow open, use the chooser's full paths to select the intended
   deck. Confirm the status shows that selected deck. Close the duplicate and reconnect if
   the same file is open in multiple slideshows.
6. Click **Disconnect** and verify PowerPoint remains open and unchanged. Reconnect if
   desired, then close Buddy while connected and verify the PowerPoint slideshow remains
   open after Buddy exits.
7. Cancel the file picker and connection chooser to verify cancellation leaves the
   current state intact. Try a damaged `.pptx` copy to confirm a recoverable error appears
   and Buddy stays responsive.

The automated UI tests use a deterministic COM boundary double. They verify launch
arguments, path-specific connection, ambiguous-session rejection, status loss, and
reconnect behavior without requiring Office on CI. Run them with:

```powershell
python -m pytest frontend/tests/test_powerpoint.py
```

## Navigation and annotations (#116)

After connecting, the **PowerPoint controls** panel appears below the connection status.
Its Previous/Next buttons navigate by actual slide number (skipping remaining builds on
the old slide); Go to slide uses the deck's numbered slide order, including hidden slides.
Commands use `GotoSlide(..., msoFalse)` to preserve existing animation state when returning.
First/last boundaries are disabled in Buddy and checked again in the adapter. PowerPoint's
own navigation is reflected by the 0.5-second status poll. The existing PDF controls above
this panel continue to operate only the PDF viewer.

The panel exports a **static slide reference** with extra room for precise selection. It
offers line, arrow, freehand path, rectangle outline, oval outline, underline, translucent
highlight, filled rectangle/oval, text box, and callout. Choose pen color and fill
transparency in the panel. Drag to specify a line or area; text and callouts use the
selected bounds plus entered text. You can also select PowerPoint's native pen or arrow
pointer. **Erase selected area** removes Buddy shapes intersecting the selected area;
**Undo last** removes the latest Buddy annotation on the current slide; **Erase all** clears
PowerPoint's temporary slideshow ink and all Buddy annotations on the current slide.
Annotations can also be reset. The static preview refreshes after a change and on slide
navigation; it does not reproduce live animations or media.

Buddy annotations are named PowerPoint slide shapes so erase, undo, and reapply remove the
actual old shapes rather than clearing PowerPoint's native ink layer. This prevents erased
marks from reappearing together with later marks. When Buddy opens a presentation,
PowerPoint opens the file read-only. When connecting to a presentation already open in
PowerPoint, annotations are added to that live presentation and could be saved if the
presenter saves it; **Erase all** removes Buddy annotations before saving. Disconnecting
Buddy leaves the current annotations on the slide. Buddy never automatically saves a
presentation. Native pen strokes use PowerPoint's temporary slideshow
ink. The Office 16 build tested here does not expose a working native eraser pointer, so
Buddy provides selected-area erase and erase-all instead.

Coordinates use the real `PageSetup` slide dimensions in points. The preview uses an
aspect-preserving viewport mapping and Qt logical pixels. No mouse injection, screen
position guessing, or Windows magnifier is used. This avoids dependence on slideshow
placement, display scaling, or monitor pixel origin. Selections and preview images are
invalidated when the connected window, slide ID, or dimensions change; **Refresh preview**
reloads authored slide content, and PowerPoint remains the authoritative display.

Actions enter `PowerPointAutomation.execute(PowerPointAction)` after runtime validation.
Requests carry an expected deck, window, slide ID, and dimensions and are checked again
against live state before mutation. Stale, duplicate, unavailable, off-slide, and nonfinite
requests are rejected. Actions are never automatically replayed after a COM failure.
Clicks during a poll are queued once, while explicit Disconnect invalidates outstanding
results and queued work. Deferred shutdown waits for the current bounded task to finish.

Some Office builds advertise a COM HWND property but reject reading it. The Windows
adapter then identifies the native slideshow window using PowerPoint's process, slideshow
window class, and an exact filename-delimited title match. Multiple native matches are
rejected, including different decks with duplicate filenames. Fullscreen mode uses the
native slideshow class; the windowed fallback currently recognizes the English
`PowerPoint Slide Show` frame title. Use fullscreen mode for localized Office installations
whose windowed title is different. Native fallback modules are included in Windows bundles.

## Recorded Windows acceptance and repeatable smoke

On 2026-10-19, Office 16.0 on Windows at a Qt device ratio of 2.0 (200% scaling) passed:

- A generated 960×540-point 16:9 fullscreen deck and 720×540-point 4:3 windowed deck.
- Next, previous, direct jumps, first/last boundaries, and synchronization after navigation
  performed directly in PowerPoint.
- Every annotation mode (line, arrow, rectangle, oval, underline, freehand, translucent
  highlight, filled rectangle/oval, text box, and callout), area erase, undo, native pen,
  erase-all, reset, and erase-then-reapply on both aspect ratios; the windowed show was
  moved/resized before drawing.
- Concurrent shows with similar filenames: actions targeted the connected show while the
  other show stayed on its original slide. Disconnect, explicit reconnect, and show exit
  detection passed; fixture file hashes remained unchanged.

Automated mapping tests cover 16:9, 4:3, and portrait slides at device ratios 1, 1.25, 1.5,
and 2, with letterbox rejection. Physical multi-monitor configurations, other Office versions,
and other physical scaling settings have not been exercised on this machine.

Repeat the real smoke (Windows with desktop PowerPoint installed):

```powershell
.\.venv\Scripts\python.exe frontend/scripts/smoke_powerpoint.py
# Pause at drawn/erased stages to inspect actual slideshow windows:
.\.venv\Scripts\python.exe frontend/scripts/smoke_powerpoint.py --hold
```

The smoke uses generated temporary decks, the real MainWindow/PresentWidget/background-task
flow, and private temporary settings. It closes only its generated presentations and removes
its fixture workspace afterward; existing presentations and account settings are untouched.
