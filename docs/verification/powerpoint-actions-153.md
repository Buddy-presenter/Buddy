# Issue #153 native action evidence — 2026-11-28

The [sanitized aggregate](powerpoint-actions-153.json) records a generated-deck run on
Windows 11 build 26200 and desktop PowerPoint **16.0.20625**. It includes hashes of the
three production modules exercised. The source PDF/PPTX were generated locally; no private
deck, microphone, paid provider or production service was used.

| Runtime | Observed result |
| --- | --- |
| Real PDFium viewer | Saved highlight and zoom applied through Present; reset restored the full view, and the successful action identity reached the synthetic session transport. |
| Real desktop PowerPoint | All 15 saved annotation types applied through Present. Native shape counts increased; annotation undo and reset restored the baseline. Each successful saved-action identity reached the synthetic session transport. |
| PowerPoint zoom | **Blocked**, not passed: Windows refused to foreground the slideshow. Buddy returned “PowerPoint lost focus. Return to Buddy and try zoom again.” Reset remained available. No successful zoom action was persisted. |

The annotation cases are callout, arrow, diamond, hexagon, line, oval, rectangle,
rounded rectangle, star, triangle, underline, filled oval, filled rectangle, highlight
and text box. The catalog and typed native executor also support zoom, but this run does
not establish a successful zoom viewport on this Windows session.

## Supported boundary and remaining acceptance

PowerPoint zoom requires an unobscured foreground slideshow, released modifiers/buttons,
one display, and a source object whose Office bounds match the saved analysis. The runtime
guard remains enforced. Do not label a focus-denied run as successful, automatically retry
by weakening the guard, or infer zoom support from a static slide export. Repeat the
documented smoke in a foreground-capable interactive Office session for a zoom pass.

Analysis/eligibility reads and versioned session writes are explicit local fixtures. They
exercise the existing controls, source/action checks and real native executor, but do not
prove authenticated server publication or independent visual verification. Production
actions still require the server's matching usable/visual result. Real microphone/provider
accuracy and live-service acceptance remain separate evidence for #229.

Earlier harness attempts exposed assumed text-box dimensions and an event-loop race
around background Office status checks. Office-measured bounds and waiting for enabled
controls corrected those fixture errors. The source/foreground guards were preserved.

Reproduce with the commands in [saved actions in Present](../present-actions.md). The
smoke returns a nonzero result when a native case is blocked and marks `complete: false`;
the aggregate intentionally retains that result. Generated decks and local screenshots
stay under ignored `build/`, rather than becoming production validation evidence.
