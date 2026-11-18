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
                "Review shows saved summaries, suggested actions and evidence for a selected deck. "
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
                "2. Open Library and choose Upload, then select a PDF or PPTX.\n"
                "3. Select one file and follow its upload progress. Uploading saves a private "
                "copy to your account and requires an internet connection.\n"
                "4. Wait for the processing state to update. Analysis runs on the service; "
                "saving a file does not mean its analysis is ready. Refresh Library as needed.\n"
                "5. Select the saved PDF and choose Review to inspect analysis, or Present PDF "
                "to use the manual viewer. You can present a PDF before analysis finishes.\n\n"
                "If a job fails, read its message and use the available retry or cancel control. "
                "Retry an interrupted upload through the existing upload card. "
                "Choosing the file again after a successful upload creates another deck. "
                "Saved PPTX decks have PowerPoint-specific analysis and Review. Native playback "
                "requires desktop PowerPoint on Windows; export to PDF for Buddy's PDF viewer. "
                "Select a deck and use Delete only when you want to remove its saved data. "
                "Read the confirmation carefully; deletion cannot be undone.",
                "Open Library",
                1,
            ),
            (
                "Review summaries and suggested actions",
                "1. Select a saved PDF or PPTX in Library and choose Review. "
                "Opening Review directly will ask you to choose a presentation.\n"
                "2. Read the presentation summary, then browse the slide summaries.\n"
                "3. Select an action row. Where a preview is available, "
                "its outline shows the target; "
                "the details describe its purpose and validation result.\n"
                "4. Open Action details & evidence, choose a proof item, then use View proof "
                "to inspect its report or before/after image.\n\n"
                "A candidate is awaiting checks. Rejected and needs-review actions require "
                "attention and must not be relied on. Execution validation checks whether an "
                "action runs and renders; it does not establish that the box surrounds the "
                "intended content. Inspect every proposed box yourself, even if its status "
                "says validated. Empty action lists are possible.\n\n"
                "Review displays saved results; it does not edit boxes or automatically play "
                "suggestions. On Windows, validated PPTX suggestions can be applied explicitly "
                "to the matching connected PowerPoint slideshow when Run action is enabled. "
                "Inspect the actual slideshow after applying a suggestion. "
                "Use Present to select and highlight or zoom a PDF region manually. "
                "Automatic visual verification and correction are not available yet.",
                None,
                None,
            ),
            (
                "Saved sessions and speech recognition",
                "For a saved PDF, open Present PDF from Library and wait for the presentation "
                "session to connect. Buddy restores its saved position. Use End session to finish, "
                "Start session to begin again, or Retry sync if a position could not be saved. "
                "Manual controls remain available.\n\n"
                "For saved PowerPoint on Windows, choose the deck in Library, open Review, "
                "choose Open saved PowerPoint, then Present with Buddy. "
                "Buddy checks that the source and slideshow match the saved analysis. "
                "Its session follows the live PowerPoint slide. It does not move your show "
                "back to an old saved position.\n\n"
                "To transcribe speech, sign in and connect one of these saved presentations. "
                "Select the microphone and language in Present, wait for the session to sync, "
                "then choose Start microphone. Audio from saved PDF and saved PowerPoint sessions "
                "is sent through Buddy's service to Deepgram in the EU. Transcripts and rolling "
                "context are temporary; audio is not saved to disk.\n\n"
                "Mute and Pause stop capture; clear the control to resume. Stop microphone, "
                "leaving Present, ending the saved session or signing out stops listening. "
                "A local-only PDF has no saved speech session. PowerPoint opened locally without "
                "a saved Buddy session can capture audio locally, but it is not transcribed "
                "or used for live suggestions.\n\n"
                "If the microphone cannot open, check system microphone permissions and the "
                "selected device. On Windows, Use microphone compatibility mode can help when "
                "normal capture fails; it may prevent other apps using that microphone while "
                "Buddy listens. After a connection or recognition failure, wait for the session "
                "to reconnect, then stop and start the microphone if needed.",
                None,
                None,
            ),
            (
                "Live suggestions and automatic moves",
                "1. Connect a saved PDF or supported saved PowerPoint presentation as described "
                "above. PowerPoint requires desktop Office on Windows and the matching saved "
                "source. A local-only slideshow is not eligible.\n"
                "2. Enable Live suggestions. Recognized speech and slide summaries are sent "
                "to Cohere to propose a slide or eligible action. Wait for Ready, then speak. "
                "If preparation fails, turn Live suggestions off and on to retry.\n"
                "3. To let Buddy act, enable Automatic transitions. Clear commands such as "
                "next slide, previous slide or go to slide 3 can navigate. Speech about a topic "
                "can select an eligible slide, highlight or zoom. Stable recognized words may "
                "be considered while you continue speaking; uncertainty or unavailable actions "
                "can leave the view unchanged. Always watch the actual presentation.\n"
                "4. Use Pause automatic moves or Ctrl+Shift+P to pause movement. "
                "Resume automatic moves continues it. This keeps transcription on; use Mute, "
                "Pause or Stop microphone to stop listening.\n"
                "5. Use Undo automatic move or Ctrl+Shift+Z to return to the previous view. "
                "Undo pauses automatic moves. Manual navigation and view controls take priority "
                "and discard pending speech actions.\n\n"
                "The shortcuts work while Buddy has keyboard focus. Leaving Present, signing "
                "out or changing sessions turns live suggestions and automatic moves off. "
                "Transient sync or connection problems prevent movement until the presentation "
                "is current and fresh speech is available; read the status before retrying. "
                "Regional actions require their own checks. A successful execution check alone "
                "does not prove a box targets the right content.",
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
                "Saved presentation shortcuts with Buddy focused: Ctrl+Shift+P pauses or resumes "
                "automatic moves; Ctrl+Shift+Z undoes the last automatic move. "
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
                "controls and coordinate units. Zoom to selected area magnifies the slideshow on "
                "Windows; Reset zoom restores the display without erasing annotations. "
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
