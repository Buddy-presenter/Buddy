# Desktop PowerPoint launch and connection (#108)

Buddy's **Present** page can open a local `.pptx` in desktop Microsoft PowerPoint on
Windows and start its slideshow, or connect to a slideshow the user already started.
PowerPoint owns rendering and playback; Buddy does not convert the source or draw slides
in its PDF surface. This initial connection issue reports the deck and current slide.
Navigation controls are tracked in #109, drawing and erase in #110, and region zoom
investigation in #111.

## User flow and lifecycle

- **Open PowerPoint…** asks for an existing `.pptx`, opens it read-only in a new desktop
  PowerPoint instance, and starts the slideshow in manual-advance mode so timed transitions
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
   about one second. This issue does not yet wire Buddy's navigation buttons to
   PowerPoint; that is #109.
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
