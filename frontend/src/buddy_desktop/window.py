"""Navigation and lifecycle for one desktop window and one application event loop."""

from dataclasses import replace

from PySide6.QtCore import QByteArray, QSettings, QSize, Slot
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
    QGuiApplication,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from buddy_desktop.account_widget import AccountWidget
from buddy_desktop.config import AppConfig, ConfigurationError, validate_api_url
from buddy_desktop.import_surface import dropped_path
from buddy_desktop.library_widget import LibraryWidget
from buddy_desktop.present_widget import PresentWidget
from buddy_desktop.presentation_service import DownloadedPDF
from buddy_desktop.profile_page import ProfilePage
from buddy_desktop.services import Health, HealthService
from buddy_desktop.slide_executor import Completion
from buddy_desktop.tasks import TaskRunner
from buddy_desktop.views import HomePage, SettingsPage
from buddy_desktop.visuals import BuddyMark, command, icon


class MainWindow(QMainWindow):
    def __init__(
        self,
        config: AppConfig,
        settings: QSettings,
        *,
        environment_override: bool = False,
    ) -> None:
        super().__init__()
        self.config = config
        self.settings = settings
        self.environment_override = environment_override
        self._closing = False
        self._active_task: int | None = None
        self._account_busy = False
        self._library_busy = False
        self._mascot_feedback = "idle"
        self._drag_attention = False
        self.setAcceptDrops(True)
        self.runner = TaskRunner(self)
        self.runner.succeeded.connect(self._health_succeeded)
        self.runner.failed.connect(self._health_failed)
        self.runner.idle.connect(self._task_idle)
        self.setWindowTitle("Buddy")
        self.setMinimumSize(800, 600)
        self.resize(1200, 800)
        self.setWindowIcon(icon("presentation", "#FF9F61"))
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(184)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(14, 24, 14, 18)
        wordmark = QHBoxLayout()
        wordmark.setSpacing(6)
        self.brand_mark = BuddyMark(40)
        wordmark.addWidget(self.brand_mark)
        brand = QLabel("Buddy")
        brand.setObjectName("brand")
        wordmark.addWidget(brand)
        wordmark.addStretch()
        sidebar_layout.addLayout(wordmark)
        sidebar_layout.addSpacing(32)
        workspace_label = QLabel("Workspace")
        workspace_label.setObjectName("muted")
        sidebar_layout.addWidget(workspace_label)
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        self.navigation.setIconSize(QSize(18, 18))
        self.navigation.setAccessibleName("Workspace navigation")
        self.navigation.setFixedHeight(208)
        for name, image in (
            ("Home", "house"),
            ("Library", "library-big"),
            ("Present", "presentation"),
            ("Settings", "settings-2"),
        ):
            self.navigation.addItem(QListWidgetItem(icon(image), name))
        sidebar_layout.addWidget(self.navigation)
        sidebar_layout.addStretch()
        self.sidebar_status = QLabel("Local workspace")
        self.sidebar_status.setObjectName("muted")
        self.sidebar_status.setWordWrap(True)
        sidebar_layout.addWidget(self.sidebar_status)
        self.pages = QStackedWidget()
        self.home = HomePage(config.api_url)
        self.account = AccountWidget(config.api_url)
        self.profile = ProfilePage(self.account)
        self.profile.controls_requested.connect(self._show_account_controls)
        self.library = LibraryWidget(config, self.account)
        self.present = PresentWidget()
        self._private_presentation = False
        self.library.present_requested.connect(self._open_saved_pdf)
        self.library.private_invalidated.connect(self._clear_private_pdf)
        self.present.executor.completed.connect(self._presentation_completed)
        self.present.executor.idle.connect(self._present_idle)
        self.library.activity_changed.connect(self._library_activity)
        self.library.mascot_changed.connect(self._mascot_event)
        self.library.import_surface.attention_changed.connect(self._drag_changed)
        self.library.preview.runner.idle.connect(self._preview_idle)
        self.home.account_layout.addWidget(self.account)
        self.home.route_requested.connect(self._select_route)
        self.home.check_requested.connect(self.check_connection)
        self.preferences = SettingsPage(config.api_url, environment_override=environment_override)
        self.preferences.save_requested.connect(self.save_settings)
        self.preferences.reduce_motion.setChecked(
            bool(settings.value("appearance/reduce_motion", False, type=bool))
        )
        self.preferences.reduce_motion.toggled.connect(self._motion_changed)
        self.account.activity_changed.connect(self._account_activity)
        page_widgets = [
            self.home,
            self.library,
            self.present,
            self.preferences,
            self.profile,
        ]
        for page in page_widgets:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setWidget(page)
            self.pages.addWidget(scroll)
        layout.addWidget(sidebar)
        workspace = QWidget()
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(0)
        header = QWidget()
        header.setObjectName("workspaceHeader")
        header.setFixedHeight(60)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(28, 0, 24, 0)
        self.route_title = QLabel("Home")
        header_layout.addWidget(self.route_title)
        header_layout.addStretch()
        self.connection_status = QLabel("Connection not checked")
        self.connection_status.setObjectName("connectionStatus")
        header_layout.addWidget(self.connection_status)
        self.activity_mark = BuddyMark(24)
        header_layout.addWidget(self.activity_mark)
        self.account_button = QPushButton("Account")
        command(self.account_button, "circle-user-round", compact=True)
        self.account_button.clicked.connect(self._show_account)
        header_layout.addWidget(self.account_button)
        workspace_layout.addWidget(header)
        workspace_layout.addWidget(self.pages, 1)
        layout.addWidget(workspace, 1)
        self.setCentralWidget(root)
        self._motion_changed(self.preferences.reduce_motion.isChecked())
        self.navigation.currentRowChanged.connect(self.navigate)
        self.navigation.setCurrentRow(0)
        self._create_menu()
        self.statusBar().setAccessibleName("Application status")
        self.statusBar().showMessage("Ready")
        geometry = settings.value("window/geometry")
        if isinstance(geometry, QByteArray):
            self.restoreGeometry(geometry)
            self._keep_window_visible()

    def _create_menu(self) -> None:
        workspace_menu = self.menuBar().addMenu("&Workspace")
        for index, label in enumerate(["&Home", "&Library", "&Present", "&Settings"]):
            action = QAction(label, self)
            action.setShortcut(QKeySequence(f"Alt+{index + 1}"))
            action.triggered.connect(lambda checked=False, row=index: self._select_route(row))
            workspace_menu.addAction(action)
        workspace_menu.addSeparator()
        account_action = QAction("&Account profile", self)
        account_action.setShortcut(QKeySequence("Alt+5"))
        account_action.triggered.connect(self._show_account)
        workspace_menu.addAction(account_action)
        fullscreen_action = QAction("Presentation &full screen", self)
        fullscreen_action.setShortcut(QKeySequence("F11"))
        fullscreen_action.triggered.connect(self.present.enter_fullscreen)
        workspace_menu.addAction(fullscreen_action)
        quit_action = QAction("&Quit Buddy", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        workspace_menu.addAction(quit_action)

    def _open_saved_pdf(self, result: DownloadedPDF) -> None:
        if self.present.executor.busy:
            return
        self._private_presentation = True
        self.navigation.setCurrentRow(2)
        self.present.load_pdf(result.content, deck_id=result.deck_id, revision=result.revision)

    def _clear_private_pdf(self) -> None:
        if self._private_presentation:
            self._private_presentation = False
            if self.present.fullscreen is not None:
                self.present.fullscreen.close()
            self.present.executor.clear()
            self.present.counter.setText("No slides loaded")
            self.present._refresh()

    def _presentation_completed(self, result: Completion) -> None:
        if result.status in {"completed", "failed"}:
            manifest = self.present.executor.manifest
            self._private_presentation = bool(manifest and manifest.account_bound)

    def _select_route(self, index: int) -> None:
        self.navigation.setCurrentRow(index)
        self.navigation.setFocus()

    @Slot()
    def _show_account(self) -> None:
        self.navigation.setCurrentRow(-1)
        self.navigate(4)
        self.profile.controls.setFocus()

    @Slot()
    def _show_account_controls(self) -> None:
        self._select_route(0)
        scroll = self.pages.widget(0)
        if isinstance(scroll, QScrollArea):
            scroll.ensureWidgetVisible(self.account)

    @Slot(int)
    def navigate(self, index: int) -> None:
        if 0 <= index < self.pages.count():
            previous = self.pages.currentIndex()
            self.pages.setCurrentIndex(index)
            self.route_title.setText(("Home", "Library", "Present", "Settings", "Profile")[index])
            self.profile.activate(index == 4)
            self.library.activate(index == 1)
            self._update_activity()
            if index == 2:
                self.statusBar().showMessage(
                    "PDF presentation controls run locally after opening or downloading the file."
                )
            elif previous == 2:
                self.statusBar().clearMessage()

    @Slot(bool)
    def _motion_changed(self, reduced: bool) -> None:
        self.settings.setValue("appearance/reduce_motion", reduced)
        for mark in self.findChildren(BuddyMark):
            mark.reduced_motion = reduced
            mark.set_state(mark.state)

    def _drag_changed(self, active: bool) -> None:
        self._drag_attention = active
        self._update_activity()

    def _mascot_event(self, state: str) -> None:
        self._mascot_feedback = state
        self._update_activity()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if (
            self.library.token()
            and not self.library.runner.busy
            and not self._closing
            and dropped_path(event.mimeData()) is not None
        ):
            self._drag_changed(True)
            self.statusBar().showMessage("Release to import your presentation into Library.")
            event.acceptProposedAction()
            return
        self.statusBar().showMessage(
            "Import one local PDF or PPTX. Sign in and finish the current operation first."
        )
        event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._drag_changed(False)
        self.statusBar().clearMessage()
        event.accept()

    def dropEvent(self, event: QDropEvent) -> None:
        path = dropped_path(event.mimeData())
        self._drag_changed(False)
        if (
            path is not None
            and self.library.token()
            and not self.library.runner.busy
            and not self._closing
        ):
            # Navigate without scheduling a competing list request ahead of the upload.
            self.pages.setCurrentIndex(1)
            self.library.active = True
            self.library.start_upload(path)
            self.navigation.setCurrentRow(1)
            self.statusBar().showMessage("Importing presentation…")
            event.acceptProposedAction()
        else:
            event.ignore()

    @Slot()
    def check_connection(self) -> None:
        if self.runner.busy or self._closing:
            return
        self.home.connection.show_state("Connecting…", "Checking your Buddy backend.")
        self.connection_status.setText("Checking connection")
        self.activity_mark.set_activity(True)
        self.home.mascot.set_activity(True)
        self.home.check.setEnabled(False)
        self.preferences.save.setEnabled(False)
        self.statusBar().showMessage("Checking backend connection…")
        self._active_task = self.runner.submit(HealthService(self.config).check)
        self._update_activity()

    @Slot(int, object)
    def _health_succeeded(self, identifier: int, result: object) -> None:
        if identifier != self._active_task or self._closing:
            return
        if not isinstance(result, Health):
            self._health_failed(identifier, "The service returned an unsupported result.")
            return
        self.home.connection.show_state("Backend connected", "Your Buddy service reports healthy.")
        self.connection_status.setText("Backend connected")
        self.statusBar().showMessage("Backend connected.")
        self._mascot_feedback = "idle"

    @Slot(int, str)
    def _health_failed(self, identifier: int, message: str) -> None:
        if identifier != self._active_task or self._closing:
            return
        self.home.connection.show_state("Connection unavailable", message, retry=True)
        self.connection_status.setText("Connection unavailable")
        self.statusBar().showMessage(message)
        self._mascot_feedback = "error"

    @Slot()
    def _task_idle(self) -> None:
        self._active_task = None
        self._update_activity()
        if self._closing:
            self.close()
        else:
            self.home.check.setEnabled(True)
            self.preferences.save.setEnabled(
                not self.environment_override
                and not self.account.runner.busy
                and not self.library.runner.busy
            )

    @Slot(bool)
    def _account_activity(self, busy: bool) -> None:
        self._account_busy = busy
        self._update_activity()
        self.preferences.save.setEnabled(
            not busy
            and not self.runner.busy
            and not self.library.runner.busy
            and not self.environment_override
        )
        if self._closing and not busy:
            self.close()

    @Slot(bool)
    def _library_activity(self, busy: bool) -> None:
        self._library_busy = busy
        self._update_activity()
        self.preferences.save.setEnabled(
            not busy
            and not self.runner.busy
            and not self.account.runner.busy
            and not self.environment_override
        )
        if self._closing and not busy:
            self.close()

    def _update_activity(self) -> None:
        state = self._mascot_feedback
        if self.pages.currentIndex() == 2 and self.present.surface.slide is not None:
            state = "presenting"
        elif self._active_task is not None or self._account_busy or self._library_busy:
            state = "working"
        elif self._drag_attention:
            state = "attention"
        for mark in self.findChildren(BuddyMark):
            mark.set_state(state)

    def _preview_idle(self) -> None:
        if self._closing:
            self.close()

    @Slot()
    def _present_idle(self) -> None:
        if self.present.surface.slide is not None:
            self.home.workspace_state.setText(self.present.counter.text())
        self._update_activity()
        if self._closing:
            self.close()

    @Slot(str)
    def save_settings(self, value: str) -> None:
        if (
            self.runner.busy
            or self.account.runner.busy
            or self.library.runner.busy
            or self._closing
            or self.environment_override
        ):
            return
        try:
            url = validate_api_url(value)
        except ConfigurationError as exc:
            self.preferences.feedback.setText(str(exc))
            self.preferences.url.setFocus()
            return
        self.settings.setValue("connection/api_url", url)
        self.settings.sync()
        if self.settings.status() != QSettings.Status.NoError:
            self.preferences.feedback.setText(
                "Cannot save settings. Check access to local preferences."
            )
            return
        self.config = replace(self.config, api_url=url)
        self.account.configure(url)
        self.library.configure(self.config)
        self.preferences.url.setText(url)
        self.home.set_endpoint(url)
        self.connection_status.setText("Connection not checked")
        self.home.connection.show_state(
            "Connection not checked", "Check the updated backend address."
        )
        self.preferences.feedback.setText("Backend settings saved.")
        self.statusBar().showMessage("Settings saved. Check the new backend connection.")

    def _keep_window_visible(self) -> None:
        screens = QGuiApplication.screens()
        if screens and not any(
            screen.availableGeometry().intersects(self.frameGeometry()) for screen in screens
        ):
            self.move(screens[0].availableGeometry().topLeft())

    def closeEvent(self, event: QCloseEvent) -> None:
        self.profile.activate(False)
        self.account.shutdown()
        self.library.shutdown()
        self.present.shutdown()
        if (
            self.runner.busy
            or self.account.runner.busy
            or self.library.runner.busy
            or self.present.executor.busy
            or self.library.preview.runner.busy
        ):
            self._closing = True
            self.runner.stop()
            central = self.centralWidget()
            if central is not None:
                central.setEnabled(False)
            self.statusBar().showMessage("Closing after the current operation finishes…")
            event.ignore()
            return
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.sync()
        event.accept()
