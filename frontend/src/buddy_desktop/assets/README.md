# Desktop visual assets

`buddy.png` is an original transparent raster asset generated with OpenAI's built-in
image generation tool on 2026-10-18. The user supplied the local Buddy Pitch 1 and 2
videos as visual references. Neither video nor extracted frames are distributed.

Generation brief: glossy orange spherical companion, two small vertical black oval
eyes, no mouth or limbs, warm upper-left highlight, coral lower edge, centered on a
transparent background. Use as a character, not as a decorative background orb.

The desktop never draws `buddy.png` directly. `visuals.mascot_image` crops the measured
disc, downscales once to the exact device-pixel size and applies an antialiased circular
mask, so the edge stays smooth at every size. The same renderer produces the window,
taskbar/Dock and packaged `.ico`/`.icns` app icons (`build_desktop.write_app_icon`).

`icons/*.svg` are unmodified Lucide icons from tag `0.468.0`:
https://github.com/lucide-icons/lucide/tree/0.468.0/icons
Their ISC license is preserved in `icons/LICENSE`. The desktop recolors them at
render time; no network access is needed to render the UI.
