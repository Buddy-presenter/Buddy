"""Native import card and bounded local PDF previews; no network or analysis claims."""

from pathlib import Path
from threading import Event

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import (
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
    QImage,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from buddy_desktop.pdf_document import render
from buddy_desktop.presentation_service import MAX_BYTES
from buddy_desktop.services import ServiceError
from buddy_desktop.slide_contract import ContractError
from buddy_desktop.tasks import TaskRunner
from buddy_desktop.visuals import BuddyMark, command


def dropped_path(mime: QMimeData) -> Path | None:
    """Only one local file, never a URL, remote share or directory."""
    urls = mime.urls()
    if len(urls) != 1 or not urls[0].isLocalFile() or urls[0].host():
        return None
    path = Path(urls[0].toLocalFile())
    if str(path).startswith(("\\\\", "//")) or path.suffix.lower() not in {".pdf", ".pptx"}:
        return None
    try:
        return path if path.is_file() else None
    except OSError:
        return None


class ImportSurface(QWidget):
    file_dropped = Signal(object)
    browse_requested = Signal()
    attention_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("importSurface")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAcceptDrops(True)
        self.available = False
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(16)
        self.mascot = BuddyMark(76)
        layout.addWidget(self.mascot, alignment=Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        self.content = text
        text.setSpacing(6)
        self.title = QLabel("Bring your next presentation.")
        self.title.setObjectName("importTitle")
        self.title.setWordWrap(True)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.message = QLabel("Drop one PDF or PowerPoint here, or choose a file.")
        self.message.setObjectName("muted")
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        text.addWidget(self.title)
        text.addWidget(self.message)
        self.browse = QPushButton("Choose presentation")
        command(self.browse, "folder-open")
        self.browse.clicked.connect(self.browse_requested.emit)
        text.addWidget(self.browse, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addLayout(text, 1)

    def set_available(self, available: bool) -> None:
        self.available = available
        self.browse.setEnabled(available)

    def set_attention(self, active: bool) -> None:
        self.setProperty("dragOver", active)
        self.style().unpolish(self)
        self.style().polish(self)
        self.mascot.set_state("attention" if active else "idle")
        self.attention_changed.emit(active)
        self.update()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self.available and dropped_path(event.mimeData()) is not None:
            self.set_attention(True)
            self.message.setText("Release to import. Your original file stays unchanged.")
            event.acceptProposedAction()
        else:
            self.message.setText(
                "Choose one local PDF or PPTX file."
                if self.available
                else "Sign in and wait for the current operation to finish to import."
            )
            event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self.set_attention(False)
        self.message.setText("Drop one PDF or PowerPoint here, or choose a file.")
        event.accept()

    def dropEvent(self, event: QDropEvent) -> None:
        path = dropped_path(event.mimeData())
        self.set_attention(False)
        if self.available and path is not None:
            event.acceptProposedAction()
            self.file_dropped.emit(path)
        else:
            event.ignore()


class SlideThumbnail(QLabel):
    def __init__(self, image: QImage) -> None:
        super().__init__()
        self.source = image
        self.setObjectName("slideThumbnail")
        self.setFixedHeight(92)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.setPixmap(
            QPixmap.fromImage(self.source).scaled(
                max(1, self.width() - 4),
                88,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def clear(self) -> None:
        self.source = QImage()
        super().clear()


class ImportPreview(QWidget):
    """At most three actual PDF slides, emitted progressively with stale-result rejection."""

    page_ready = Signal(object, int, object, int)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("importPreview")
        self.runner = TaskRunner(self)
        self.pending_path: Path | None = None
        self.closing = False
        self.runner.idle.connect(self._start_pending)
        self.cancelled = Event()
        self.runner.failed.connect(self.failed)
        self.page_ready.connect(self.display)
        self.title = QLabel()
        self.title.setObjectName("muted")
        self.title.setWordWrap(True)
        self.row = QHBoxLayout()
        self.row.setSpacing(10)
        self.images: list[QLabel] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.addWidget(self.title)
        layout.addLayout(self.row)
        self.hide()

    def clear(self) -> None:
        self.pending_path = None
        self.cancelled.set()
        for label in self.images:
            self.row.removeWidget(label)
            label.clear()
            label.deleteLater()
        self.images.clear()
        self.title.clear()
        self.hide()

    def start(self, path: Path) -> None:
        self.clear()
        if self.closing:
            return
        if path.suffix.lower() != ".pdf":
            self.title.setText("PowerPoint original · slide previews are not available yet.")
            self.show()
            return
        if self.runner.busy:
            self.pending_path = path
            self.title.setText("Waiting for the previous slide preview to stop…")
            self.show()
            return
        operation = Event()
        self.cancelled = operation
        self.title.setText("Preparing local slide previews…")
        self.show()

        def work() -> None:
            try:
                with path.open("rb") as stream:
                    content = stream.read(MAX_BYTES + 1)
                if not content.startswith(b"%PDF-") or not 0 < len(content) <= MAX_BYTES:
                    raise ServiceError("Slide preview unavailable. Upload validation is separate.")
                for index in range(3):
                    if operation.is_set():
                        return
                    metadata, image = render(content, index)
                    if operation.is_set():
                        return
                    image = image.scaled(
                        240,
                        144,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    count = len(metadata["sizes"])
                    self.page_ready.emit(operation, index, image, count)
                    if index + 1 >= count:
                        return
            except (OSError, ContractError):
                if not operation.is_set():
                    raise ServiceError(
                        "Slide preview unavailable. You can still import the original."
                    ) from None

        self.runner.submit(work)

    def display(self, operation: object, index: int, image: QImage, count: int) -> None:
        if operation is not self.cancelled or self.cancelled.is_set():
            return
        if index != len(self.images):
            return
        label = SlideThumbnail(image)
        label.setAccessibleName(f"Slide {index + 1} preview")
        label.setToolTip(f"Slide {index + 1} of {count}")
        self.images.append(label)
        self.row.addWidget(label, 1)
        self.title.setText(
            f"{count} slides · local preview · showing {len(self.images)} of {min(3, count)}"
        )

    def failed(self, _identifier: int, message: str) -> None:
        if not self.cancelled.is_set():
            self.title.setText(message)

    def shutdown(self) -> None:
        self.closing = True
        self.clear()
        self.runner.stop()

    def _start_pending(self) -> None:
        if self.pending_path is not None and not self.closing:
            self.start(self.pending_path)
