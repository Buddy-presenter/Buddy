# Buddy visual guidelines

For every change to the desktop UI, read this guide and the [Apple Design skill](skills/apple-design/SKILL.md) first. Treat the skill's interaction principles as guidance for PySide6: translate the ideas to native Qt behavior instead of copying web-only CSS or JavaScript examples.

## Color palette

Current product direction: use a pure black (`#000000`) app canvas, neutral black/gray
raised surfaces, readable white/gray text and Buddy orange as the sole chromatic UI
accent. This supersedes blue-black/navy surfaces in the historical references below.
Presentation slide contents retain their original colors.

The values below are sampled from the six provided Buddy reference images. Several references use grain, glow, antialiasing, and gradients, so the gradient stops and glow colors are representative samples rather than claims about a single flat source color.

| Token | Hex | Use |
| --- | --- | --- |
| Canvas black | `#06090D` | Main dark canvas; common sampled background. |
| Deep black | `#050508` | Darker full-bleed backgrounds. |
| Blue-black | `#0E1019` | Subtle cool depth behind content. |
| Raised dark surface | `#1D1E25` | Dark cards, inactive marks, and secondary shapes. |
| Primary soft white | `#B5B8BA` | Main headlines and prominent text on dark backgrounds. |
| Secondary gray | `#65666D` | Supporting copy and labels; verify contrast at actual text sizes. |
| Buddy rust | `#B45032` | Restrained brand accent, including the quote-mark motif. |
| Warm gradient start | `#F65635` | Bright coral-orange emphasis. |
| Warm gradient middle | `#F5843D` | Transition through the Buddy orange. |
| Warm gradient end | `#F6A142` | Amber end of warm emphasis and waveform accents. |
| Mascot amber | `#D57D3B` | Warm, dimensional Buddy mark and glow. |
| Spotlight cream | `#B0A69A` | Selective audience/focus highlights against muted dark figures. |
| Light card | `#DBD9D4` | Intentional high-contrast information panel in the dashboard reference. |
| Chart lavender | `#B6BEDF` | Supporting chart series on the light card. |
| Chart blue | `#2E4EE8` | One strong chart highlight; reserve for data emphasis. |

## Applying the palette

- Keep the overall experience dark, cinematic, and calm. Use near-black backgrounds, soft-white typography, and restrained warm highlights.
- Use the coral-to-amber ramp for meaningful emphasis, such as a key word, waveform, progress, or focused action. Avoid using the full gradient on routine controls.
- Keep rust, mascot amber, and the coral-to-amber gradient related but distinct: rust is quiet brand detail, amber belongs to the mascot/glow, and the gradient is for high-energy emphasis.
- Reserve the light card and blue/lavender chart colors for a deliberate data surface. They are supporting colors from one reference, not the default application palette.
- Use the cream highlight sparingly to direct attention. Preserve readable contrast and do not communicate status through color alone.
- The source images contain subtle grain and atmospheric glows. Keep any such treatment low-contrast and behind content so it never reduces text legibility.

