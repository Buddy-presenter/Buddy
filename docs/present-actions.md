# Saved actions in Present (#153)

Present uses the existing analysis, PDF speech and native PowerPoint execution paths.
There is no second action catalog or speech loop.

## Connect the exact saved presentation

For PDF, select the deck in Library and choose **Present PDF**. For PowerPoint, use
Windows with desktop PowerPoint installed: open the deck in **Review**, choose
**Open saved PowerPoint**, then **Present with Buddy**. Opening an unrelated local PPTX
does not attach it to saved actions. Wait for the saved session to connect.

The source hash/revision, analysis, native slide IDs, slide dimensions and slideshow must
match. Office version/build is checked again before a saved PowerPoint action executes.
Editing/replacing the source or updating Office can require reopening and revalidation.
See [PowerPoint analysis](powerpoint-analysis.md) and [action checks](preflight-publication.md).

## Load, apply and recover

Saved actions load automatically once per connected analysis. **Load saved actions**
refreshes them explicitly. The list follows the current slide; choosing a row does not
execute anything. **Apply saved action** re-reads the analysis before dispatching the
selected typed action. A slide, deck, session, analysis or source change during that read
prevents execution. Duplicate clicks cannot queue another load or apply while busy.

Only usable, validated and visually verified actions can be applied. Execution-only
proof does not count. Disabled rows explain pending visual verification, failed checks,
stale evidence, Windows/Office requirements or unsupported live types. Open Review for
details and evidence. No action is applied after sign-out or session end during loading.

PowerPoint's saved catalog supports highlight, zoom, rectangle, rounded rectangle, oval,
underline, arrow, line, diamond, triangle, hexagon, star, filled rectangle/oval, callout and
text box. All map to the existing typed native executor. Text placement can refuse when
there is no clear space; zoom requires the exact saved source object. Freehand, pointer,
erase, undo, reset and navigation remain manual controls. PDF saved actions are zoom and
highlight only. Speech-triggered PowerPoint regions use the same eligible saved actions.

PowerPoint zoom additionally requires a foreground, unobscured slideshow on one display.
The #153 local run confirmed native annotations but recorded zoom as blocked by Windows
focus restrictions. A static preview or a passing unit test is not a native zoom pass.

Failures leave manual controls available. PowerPoint's **Undo last** removes the last
Buddy annotation; **Reset annotations** removes Buddy annotations; **Reset zoom** restores
the full view. PDF's **Reset view** restores the slide and removes its temporary highlight.
These controls do not change the original saved file. **Undo automatic move** applies to
automatic transitions and is distinct from native annotation undo. Manual navigation takes
priority over speech actions. PDF records a saved-action ID only after executor success.

## Reproduce evidence

```powershell
python -m pytest frontend/tests/test_saved_actions.py frontend/tests/test_powerpoint_review.py frontend/tests/test_powerpoint_present.py frontend/tests/test_transition_player.py frontend/tests/test_agent.py -q
```

For real execution, use an interactive Windows/Office session with no existing PowerPoint
process. This script generates its own PDF/PPTX, drives the saved-action controls, checks
annotations and recovery, and closes its own Office instance without saving annotations:

```powershell
python frontend/scripts/smoke_saved_actions.py --output build/issue153-new
```

Use a new output directory. The report records OS, Office version/build, source-code hashes,
separate PDF/PowerPoint results and substituted boundaries. Analysis/eligibility and session
transport are explicit local fixtures. This proves player execution and recovery, not
server publication, independent semantic verification, microphone/provider accuracy or
deployment readiness. Only the server can publish usable results for real saved decks.

See [issue #153 evidence](verification/powerpoint-actions-153.md) for measured results and
the remaining live-service boundary supplied to #229. CI uses Office substitutes; those
tests must not be described as native Office acceptance.
