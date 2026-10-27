"""Offline user guide for the capabilities available in the desktop application."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QPushButton

from buddy_desktop.views import Page, group
from buddy_desktop.visuals import command


class TutorialPage(Page):
    route_requested = Signal(int)

    def __init__(self) -> None:
        super().__init__(
            "How to use",
            "Start with a local PDF, or sign in to save decks and review suggested actions. "
            "This guide is always available, even offline.",
        )
        sections = (
            (
                "Start here: present your first PDF",
                "1. Open Present in the sidebar and choose Open PDF…\n"
                "2. Select a PDF on your computer. No account or internet connection is needed.\n"
                "3. Use Next, Previous or Go to slide to move through the pages.\n"
                "4. Drag over an area of the slide, then choose Highlight or Zoom. "
                "Reset view clears the highlight and returns to the full page.\n"
                "5. Choose Full screen (F11) for an audience view. "
                "Escape returns to the workspace. "
                "Check which monitor shows the audience view before presenting.\n\n"
                "Highlights and zoom are temporary. Your original PDF is not changed.",
                "Open Present",
                3,
            ),
            (
                "Find your way around",
                "Home is the starting point. Library contains decks saved to your account. "
                "Review shows saved summaries, suggested regions and evidence for a selected PDF. "
                "Present contains manual PDF and Windows PowerPoint controls. "
                "Settings contains connection and reduced-motion preferences.\n\n"
                "Use Sign in at the top right for account access. Once signed in, "
                "open the account menu to view Profile or sign out.",
                None,
                None,
            ),
            (
                "Sign in and save a deck",
                "1. Choose Sign in at the top right and complete the browser sign-in. "
                "Use the browser's registration or password-recovery options if needed. "
                "Return to Buddy when the browser confirms the result.\n"
                "2. Open Library and choose Upload PDF or PPTX…\n"
                "3. Select one file and follow its upload progress. Uploading saves a private "
                "copy to your account and requires an internet connection.\n"
                "4. Wait for the processing state to update. PDF analysis runs on the service; "
                "saving a file does not mean its analysis is ready. Refresh Library as needed.\n"
                "5. Select the saved PDF and choose Review to inspect analysis, or Present PDF "
                "to use the manual viewer. You can present a PDF before analysis finishes.\n\n"
                "If a job fails, read its message and use the available retry or cancel control. "
                "Retry an interrupted upload through the existing upload card. "
                "Choosing the file again after a successful upload creates another deck. "
                "For summaries and in-app playback, export a PowerPoint deck to PDF first.",
                "Open Library",
                1,
            ),
            (
                "Review summaries and suggested actions",
                "1. Select a saved PDF in Library and choose Review. "
                "Opening Review directly will ask you to choose a presentation.\n"
                "2. Read the presentation summary, then browse the slide summaries.\n"
                "3. Select an action row. The outline shows its proposed target region; "
                "the details describe its purpose and validation result.\n"
                "4. Choose available validation evidence and Open evidence to inspect the report "
                "or before/after image.\n\n"
                "A candidate is awaiting checks. Rejected and needs-review actions require "
                "attention and must not be relied on. Execution validation checks whether an "
                "action runs and renders; it does not establish that the box surrounds the "
                "intended content. Inspect every proposed box yourself, even if its status "
                "says validated. Empty action lists are possible.\n\n"
                "Review displays saved results; it does not edit boxes or automatically play "
                "suggestions. Use Present to select and highlight or zoom a region manually. "
                "Automatic visual verification and correction are not available yet.",
                None,
                None,
            ),
            (
                "PDF keyboard shortcuts",
                "Use these while the presentation surface has focus:\n\n"
                "Next slide: Right arrow, Space or Page Down\n"
                "Previous slide: Left arrow or Page Up\n"
                "Choose a slide: G\n"
                "Highlight a dragged selection: H\n"
                "Zoom to a dragged selection: Z\n"
                "Reset view: R\n"
                "Full screen: F11; leave full screen: Escape or F11\n\n"
                "Workspace shortcuts: Alt+1 Home, Alt+2 Library, Alt+3 Review, "
                "Alt+4 Present, Alt+5 Settings, Alt+6 Profile, Alt+7 How to use. "
                "Use Tab to move between controls and Enter or Space to activate a button.",
                None,
                None,
            ),
            (
                "Use desktop PowerPoint on Windows",
                "Install desktop Microsoft PowerPoint first. In Present, choose Open PowerPoint… "
                "to start a local .pptx, or Connect to slideshow… to select an existing slideshow. "
                "This local connection needs no Buddy account.\n\n"
                "Use the PowerPoint controls for slide navigation and native annotations. "
                "Select a region on the reference preview, choose an annotation and apply it. "
                "Undo last and Erase all annotations remove marks; Refresh preview updates "
                "the static reference image. Watch the real PowerPoint slideshow to confirm "
                "the result. Disconnect detaches Buddy from the slideshow.\n\n"
                "PDF keyboard shortcuts apply to Buddy's PDF viewer. PowerPoint has separate "
                "controls and coordinate units; region zoom is not available through this path. "
                "On macOS, or without desktop PowerPoint, export the deck to PDF and use Present.",
                None,
                None,
            ),
            (
                "If something does not work",
                "Cannot sign in or load Library? Check your internet connection, then open "
                "Settings and choose Check connection. Confirm the backend address with your "
                "project administrator before changing it. "
                "Sign in again if the session expired.\n\n"
                "Browser sign-in taking too long? Use Cancel sign-in in Buddy, then retry. "
                "Registration may require email verification.\n\n"
                "PDF will not open? Use a valid, unencrypted PDF. Export password-protected, "
                "damaged or unsupported files to a new PDF and try again.\n\n"
                "Analysis missing or still processing? Refresh Library and reopen Review. "
                "Read the processing error if one appears. "
                "You can still present the PDF manually.\n\n"
                "Box targets the wrong content? Inspect its evidence and use a manual selection "
                "in Present. The validated label alone does not prove correct targeting.\n\n"
                "Want less animation? Turn on Reduce motion in Settings.\n\n"
                "Signing out or changing the backend clears private review and presentation "
                "content from the workspace. Local viewing does not upload your file; "
                "Library upload sends a copy to the configured service for storage and processing.",
                "Open Settings",
                4,
            ),
        )
        for title, text, button_text, route in sections:
            layout = group(title, self.body)
            label = QLabel(text)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(label)
            if button_text is not None and route is not None:
                button = QPushButton(button_text)
                command(button, "arrow-right")
                button.clicked.connect(
                    lambda checked=False, destination=route: self.route_requested.emit(destination)
                )
                layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.body.addStretch()
