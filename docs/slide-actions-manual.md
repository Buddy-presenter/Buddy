# Try the six slide commands in Buddy

The Present workspace opens local rendered slides. You can test all six commands without
starting the backend, signing in, using an AI service, or installing PowerPoint. The
prepared samples include wide, standard and portrait slides, plus a slide with a word,
phrase, expression and formula.

## Start the app

On this Windows checkout, open PowerShell and run:

```powershell
Set-Location 'C:\Users\Administrator\Desktop\Buddy'
.\.venv\Scripts\python.exe -m buddy_desktop
```

The environment has already been installed for this work. On a fresh checkout, first
follow the [desktop setup](development.md#desktop). The normal macOS launch command is
`.venv/bin/python -m buddy_desktop`. Use the native Qt platform for manual testing; unset
`QT_QPA_PLATFORM=offscreen` if you previously enabled it for automated tests.

1. Select **Present** in the sidebar, or use **Alt+3**.
2. Click **Open rendered slides…**.
3. In the file chooser, open this prepared manifest:

   ```text
   C:\Users\Administrator\Desktop\Buddy\frontend\tests\fixtures\slide_actions\manual-manifest.json
   ```

   On another checkout, use `frontend/tests/fixtures/slide_actions/manual-manifest.json`
   beneath its repository directory. Choose the JSON file; it points at the real slide
   images beside it. The current local player opens rendered slide manifests, rather than
   converting an arbitrary PPTX from this button.

4. Confirm the counter says **Slide 1 of 4**. The app's Home connection message can remain
   unchecked: local slide controls work independently of it.

## Test each command

| Command | What to do | What you should see |
| --- | --- | --- |
| Next | Click **Next**. | Slide 2 of 4; the image changes. |
| Previous | Click **Previous**. | Slide 1 of 4 returns. |
| Go to slide | Choose **Slide 3** in the list, then click **Go to slide**. | The portrait slide appears with its correct proportions. |
| Zoom | Drag a rectangle around the title or a whole paragraph, then click **Zoom to selection**. | The selected area is centered and enlarged without stretching. |
| Reset | Click **Reset**. | The entire current slide returns; any yellow highlight and dashed selection disappear. |
| Highlight | Go to **Slide 4**. Drag around **Buddy**, then click **Highlight selection**. | A translucent yellow rectangle marks the word; the slide does not zoom. |

The dashed rectangle is your selection. It becomes a yellow highlight only after you
click **Highlight selection**. Drawing another selection leaves the old yellow mark in
place until you apply the new highlight.

On Slide 4, repeat Highlight around **Focus on one idea.**, **price × quantity**, and
**E = mc²**. Each command replaces the previous yellow mark. You can select an image,
symbol or any other visible region on other slides in exactly the same way.

For a precise keyboard-only test, enter these values on Slide 4 and use Tab/Space to
activate **Highlight selection**:

| Content | Left | Top | Width | Height |
| --- | --- | --- | --- | --- |
| Word | 80 | 280 | 200 | 90 |
| Phrase | 80 | 440 | 600 | 90 |
| Expression | 80 | 600 | 550 | 90 |
| Formula | 80 | 760 | 320 | 90 |

These are slide coordinates, not screen pixels, so the same values work after resizing.

## Check the edge cases

- Highlight the formula, then resize the app. The yellow mark stays attached to it.
- Draw a larger area around the highlighted formula and click Zoom. The same yellow
  highlight follows the enlarged content. Select and highlight another part while zoomed.
- Press Reset: both the zoom and yellow mark disappear.
- Highlight something and change slides: the new slide has no highlight. Returning to
  the old slide shows it without a saved highlight.
- Click Previous on Slide 1, or Next on Slide 4: the counter stays at the boundary.
  A highlight stays if the current slide did not actually change.
- Click without dragging: there is no nonempty selection and the region buttons stay
  disabled. Starting a drag in the dark margin outside the slide does not select content.
- Very small selections are valid for highlighting. Zoom has a maximum magnification;
  enlarge the selected area if the status says it exceeds the 32x limit.
- Enter Left `1910`, Top `100`, Width `100`, Height `20` on Slide 4 and click Highlight:
  the status reports that the region exceeds slide bounds, and the existing mark remains.
- While a file is loading, the UI remains responsive. Reset is available for the already
  displayed slide. A missing or damaged image reports an error and leaves the previous
  slide in full view, with Reset available.

Closing Buddy discards these local highlights and selections. The source PNG/PPTX files
are unchanged. Advanced presentation sessions and automatic text lookup remain separate work.
