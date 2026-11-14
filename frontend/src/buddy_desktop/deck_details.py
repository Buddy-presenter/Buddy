"""Deck-detail controls; network work stays in the library's serialized task runner."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from buddy_desktop.library_service import DeckDetails
from buddy_desktop.visuals import command


class DeckDetailsPanel(QWidget):
    back_requested = Signal()
    refresh_requested = Signal()
    rename_requested = Signal(str)
    delete_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.data: DeckDetails | None = None
        self.busy = False
        self.setAccessibleName("Presentation details")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        actions = QHBoxLayout()
        self.back = QPushButton("Back to Library")
        command(self.back, "chevron-left")
        self.back.clicked.connect(self.back_requested)
        self.refresh = QPushButton("Refresh details")
        command(self.refresh, "refresh-cw")
        self.refresh.clicked.connect(self.refresh_requested)
        self.delete = QPushButton("Delete presentation")
        self.delete.clicked.connect(self.delete_requested)
        for button in (self.back, self.refresh, self.delete):
            button.setAccessibleName(button.text())
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        title_row = QHBoxLayout()
        name_label = QLabel("&Name")
        self.name = QLineEdit()
        self.name.setMaxLength(256)
        self.name.setAccessibleName("Presentation name")
        name_label.setBuddy(self.name)
        self.save = QPushButton("Save name")
        self.save.setAccessibleName("Save presentation name")
        self.save.clicked.connect(self._rename)
        self.name.returnPressed.connect(self._rename)
        self.name.textChanged.connect(self._controls)
        title_row.addWidget(name_label)
        title_row.addWidget(self.name, 1)
        title_row.addWidget(self.save)
        layout.addLayout(title_row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setAccessibleName("Presentation details status")
        layout.addWidget(self.status)
        self.summary = QPlainTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setTabChangesFocus(True)
        self.summary.setAccessibleName("Presentation processing and analysis details")
        layout.addWidget(self.summary, 1)
        self.clear()

    def _rename(self) -> None:
        if self.save.isEnabled():
            self.rename_requested.emit(self.name.text().strip())

    def _controls(self) -> None:
        loaded = self.data is not None
        name = self.name.text().strip()
        self.name.setEnabled(loaded and not self.busy)
        self.save.setEnabled(
            bool(loaded and not self.busy and name and self.data and name != self.data.title)
        )
        self.delete.setEnabled(loaded and not self.busy)
        self.refresh.setEnabled(not self.busy)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self._controls()

    def clear(self) -> None:
        self.data = None
        self.name.clear()
        self.summary.clear()
        self.status.clear()
        self._controls()
        self.hide()

    def show_details(self, data: DeckDetails, *, renamed: bool = False) -> None:
        self.data = data
        self.name.setText(data.title)
        processing = data.processing
        processing_label = {
            "uploaded": "Uploaded",
            "queued": "Queued",
            "inspecting_pdf_metadata": (
                "Inspecting PowerPoint" if data.source_format == "pptx" else "Inspecting PDF"
            ),
            "analyzing": "Analyzing",
            "validating": "Checking suggested actions",
            "review_ready": "Results available",
            "partial": "Partial results",
            "failed": "Failed",
            "cancelled": "Cancelled",
        }[processing.state]
        analysis = {
            "unavailable": "No saved analysis yet.",
            "stale": "Saved analysis is for an earlier source or processing job.",
            "partial": "Partial analysis; some results are unavailable.",
            "awaiting_validation": "Analysis is waiting for action checks.",
            "published": "Published analysis is available in Review.",
        }[data.analysis_status]
        version = str(data.analysis_version) if data.analysis_version is not None else "None"
        self.summary.setPlainText(
            f"Type: {data.source_format.upper()}\n"
            f"Added: {data.created_at.astimezone().strftime('%Y-%m-%d %H:%M')}\n"
            f"Processing: {processing_label}\n"
            f"Progress: {processing.progress}%\nAttempts: {processing.attempts}\n"
            f"Analysis: {analysis}\n"
            f"Analysis version: {version}\n"
            f"Slides in saved analysis: {data.slide_count}\n"
            + (
                f"Processing issue: {processing.error_code.replace('_', ' ')}\n"
                if processing.error_code
                else ""
            )
            + "\nPublished analysis and execution checks do not prove correct visual targeting."
        )
        self.status.setText("Presentation name saved." if renamed else "")
        self._controls()
