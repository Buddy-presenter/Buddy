"""Frozen desktop entry point with an isolated real-window acceptance mode."""

import sys

if "--smoke-test" in sys.argv:
    from frontend.scripts.smoke_desktop import main

    sys.argv = [sys.argv[0]]
else:
    from buddy_desktop.app import main

raise SystemExit(main())
