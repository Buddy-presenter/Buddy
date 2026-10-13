"""Navigation and lifecycle for one desktop window and one application event loop."""

from dataclasses import replace

from PySide6.QtCore import QByteArray, QSettings, Slot
from PySide6.QtGui import QAction, QCloseEvent, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from buddy_desktop.config import AppConfig, ConfigurationError, validate_api_url
from buddy_desktop.services import Health, HealthService
from buddy_desktop.tasks import TaskRunner
from buddy_desktop.views import HomePage, SettingsPage, UnavailablePage


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
        self.runner = TaskRunner(self)
        self.runner.succeeded.connect(self._health_succeeded)
        self.runner.failed.connect(self._health_failed)
        self.runner.idle.connect(self._task_idle)
        self.setWindowTitle("Buddy")
        self.setMinimumSize(800, 600)
        self.resize(1120, 760)
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(200)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(16, 24, 16, 16)
        brand = QLabel("Buddy")
        brand.setObjectName("brand")
        subtitle = QLabel("Presenter companion")
        subtitle.setWordWrap(True)
        sidebar_layout.addWidget(brand)
        sidebar_layout.addWidget(subtitle)
        self.navigation = QListWidget()
        self.navigation.setAccessibleName("Workspace navigation")
        self.navigation.addItems(["Home", "Library", "Present", "Settings"])
        sidebar_layout.addWidget(self.navigation)
        help_label = QLabel("Alt+1–4: switch workspace\nTab: move between controls")
        help_label.setWordWrap(True)
        sidebar_layout.addWidget(help_label)
        self.pages = QStackedWidget()
        self.home = HomePage(config.api_url)
        self.home.check_requested.connect(self.check_connection)
        self.preferences = SettingsPage(config.api_url, environment_override=environment_override)
        self.preferences.save_requested.connect(self.save_settings)
        page_widgets = [
            self.home,
            UnavailablePage(
                "Library",
                "Your deck library will be available when account access is connected.",
            ),
            UnavailablePage(
                "Present",
                "Presentation tools will be available when deck processing is connected.",
            ),
            self.preferences,
        ]
        for page in page_widgets:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setWidget(page)
            self.pages.addWidget(scroll)
        layout.addWidget(sidebar)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(root)
        self.navigation.currentRowChanged.connect(self.navigate)
        self.navigation.setCurrentRow(0)
        self._create_menu()
        self.statusBar().setAccessibleName("Application status")
        self.statusBar().showMessage("Ready. Check your backend connection to get started.")
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
        quit_action = QAction("&Quit Buddy", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        workspace_menu.addAction(quit_action)

    def _select_route(self, index: int) -> None:
        self.navigation.setCurrentRow(index)
        self.navigation.setFocus()

    @Slot(int)
    def navigate(self, index: int) -> None:
        if 0 <= index < self.pages.count():
            self.pages.setCurrentIndex(index)

    @Slot()
    def check_connection(self) -> None:
        if self.runner.busy or self._closing:
            return
        self.home.connection.show_state("Connecting…", "Checking your Buddy backend.")
        self.home.check.setEnabled(False)
        self.preferences.save.setEnabled(False)
        self.statusBar().showMessage("Checking backend connection…")
        self._active_task = self.runner.submit(HealthService(self.config).check)

    @Slot(int, object)
    def _health_succeeded(self, identifier: int, result: object) -> None:
        if identifier != self._active_task or self._closing:
            return
        if not isinstance(result, Health):
            self._health_failed(identifier, "The service returned an unsupported result.")
            return
        self.home.connection.show_state("Backend connected", "Your Buddy service reports healthy.")
        self.statusBar().showMessage("Backend connected.")

    @Slot(int, str)
    def _health_failed(self, identifier: int, message: str) -> None:
        if identifier != self._active_task or self._closing:
            return
        self.home.connection.show_state("Connection unavailable", message, retry=True)
        self.statusBar().showMessage(message)

    @Slot()
    def _task_idle(self) -> None:
        self._active_task = None
        if self._closing:
            self.close()
        else:
            self.home.check.setEnabled(True)
            self.preferences.save.setEnabled(not self.environment_override)

    @Slot(str)
    def save_settings(self, value: str) -> None:
        if self.runner.busy or self._closing or self.environment_override:
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
        self.preferences.url.setText(url)
        self.home.endpoint.setText(url)
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
        if self.runner.busy:
            self._closing = True
            self.runner.stop()
            central = self.centralWidget()
            if central is not None:
                central.setEnabled(False)
            self.statusBar().showMessage("Closing after the current connection check finishes…")
            event.ignore()
            return
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.sync()
        event.accept()
