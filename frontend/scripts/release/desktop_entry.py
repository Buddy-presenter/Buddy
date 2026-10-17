"""Frozen desktop entry point with an isolated real-window acceptance mode."""

import sys

if "--pdf-worker" in sys.argv:
    from buddy_desktop.pdf_worker import main
elif "--smoke-test" in sys.argv:
    from frontend.scripts.smoke_desktop import main

    sys.argv = [sys.argv[0]]
else:
    from buddy_desktop.app import main

raise SystemExit(main())
