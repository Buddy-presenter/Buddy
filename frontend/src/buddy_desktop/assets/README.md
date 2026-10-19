# Desktop visual assets

`buddy.png` is an original transparent raster asset generated with OpenAI's built-in
image generation tool on 2026-10-18. The user supplied the local Buddy Pitch 1 and 2
videos as visual references. Neither video nor extracted frames are distributed.

Generation brief: glossy orange spherical companion, two small vertical black oval
eyes, no mouth or limbs, warm upper-left highlight, coral lower edge, centered on a
transparent background. Use as a character, not as a decorative background orb.

`buddy-body.png` is a separate animation-only body layer derived from `buddy.png`
with the built-in Image Generation tool on 2026-10-19. The original logo is unchanged.
The companion paints independent glossy oval eyes over this body so gaze, blinking,
sleepy eyes and happy expressions do not require a sprite sheet or a new runtime.

Final edit prompt: remove only both black eyes and their recessed rims; reconstruct
the uninterrupted orange surface beneath them. Preserve the original silhouette,
square framing, size, centering, amber upper-left highlight, glossy reflections,
coral-red shading, material and transparent alpha boundary. No face, mouth, limbs,
background, text, or new highlights/shadows. Code draws the eyes separately.

`icons/*.svg` are unmodified Lucide icons from tag `0.468.0`:
https://github.com/lucide-icons/lucide/tree/0.468.0/icons
Their ISC license is preserved in `icons/LICENSE`. The desktop recolors them at
render time; no network access is needed to render the UI.
