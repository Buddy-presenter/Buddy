"""Navigation and lifecycle for one desktop window and one application event loop."""

import sys
from dataclasses import replace
from uuid import UUID

from PySide6.QtCore import QByteArray, QSettings, QSize, Qt, Slot
from PySide6.QtGui import QAction, QCloseEvent, QGuiApplication, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
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
from buddy_desktop.auth import AccountProfile
from buddy_desktop.config import AppConfig, ConfigurationError, validate_api_url
from buddy_desktop.library_widget import LibraryWidget
from buddy_desktop.present_widget import PresentWidget
from buddy_desktop.presentation_service import DownloadedPDF
from buddy_desktop.profile_page import ProfilePage
from buddy_desktop.review_widget import ReviewWidget
from buddy_desktop.services import Health, HealthService
from buddy_desktop.slide_executor import Completion
from buddy_desktop.tasks import TaskRunner
from buddy_desktop.views import HomePage, SettingsPage, divider
from buddy_desktop.visuals import BuddyMark, app_icon, command, icon


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
        self._verified_profile: AccountProfile | None = None
        self.runner = TaskRunner(self)
        self.runner.succeeded.connect(self._health_succeeded)
        self.runner.failed.connect(self._health_failed)
        self.runner.idle.connect(self._task_idle)
        self.setWindowTitle("Buddy")
        self.setMinimumSize(800, 600)
        self.resize(1200, 800)
        self.setWindowIcon(app_icon())
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(208)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(12, 16, 12, 16)
        wordmark = QHBoxLayout()
        wordmark.setContentsMargins(6, 0, 0, 0)
        wordmark.setSpacing(8)
        wordmark.addWidget(BuddyMark(28))
        brand = QLabel("Buddy")
        brand.setObjectName("brand")
        wordmark.addWidget(brand)
        wordmark.addStretch()
        sidebar_layout.addLayout(wordmark)
        sidebar_layout.addSpacing(20)
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        self.navigation.setIconSize(QSize(18, 18))
        self.navigation.setAccessibleName("Workspace navigation")
        self.navigation.setFixedHeight(5 * 38)
        self.navigation.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for name, image in (
            ("Home", "house"),
            ("Library", "library-big"),
            ("Review", "search"),
            ("Present", "presentation"),
            ("Settings", "settings-2"),
        ):
            glyph = icon(image)
            glyph.addPixmap(icon(image, "#FFB27F").pixmap(40, 40), QIcon.Mode.Selected)
            self.navigation.addItem(QListWidgetItem(glyph, name))
        sidebar_layout.addWidget(self.navigation)
        sidebar_layout.addStretch()
        self.pages = QStackedWidget()
        self.home = HomePage(config.api_url)
        self.account = AccountWidget(config.api_url)
        self.profile = ProfilePage(self.account)
        self.profile.controls_requested.connect(self._show_account_controls)
        self.library = LibraryWidget(config, self.account)
        self.review = ReviewWidget(config, self.account)
        self.present = PresentWidget()
        self._private_presentation = False
        self.library.present_requested.connect(self._open_saved_pdf)
        self.library.review_requested.connect(self._open_review)
        self.review.back_requested.connect(lambda: self.navigation.setCurrentRow(1))
        self.review.signin_requested.connect(self._show_account_controls)
        self.library.private_invalidated.connect(self._clear_private_pdf)
        self.present.executor.completed.connect(self._presentation_completed)
        self.present.executor.idle.connect(self._present_idle)
        self.present.powerpoint_runner.idle.connect(self._powerpoint_idle)
        self.library.activity_changed.connect(self._library_activity)
        self.review.activity_changed.connect(self._review_activity)
        self.home.route_requested.connect(self._select_route)
        self.preferences = SettingsPage(config.api_url, environment_override=environment_override)
        self.preferences.save_requested.connect(self.save_settings)
        self.preferences.check_requested.connect(self.check_connection)
        self.preferences.reduce_motion.setChecked(
            bool(settings.value("appearance/reduce_motion", False, type=bool))
        )
        self.preferences.reduce_motion.toggled.connect(self._motion_changed)
        self.account.activity_changed.connect(self._account_activity)
        page_widgets = [
            self.home,
            self.library,
            self.review,
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
        header.setFixedHeight(64)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(32, 0, 20, 0)
        header_layout.setSpacing(12)
        self.route_title = QLabel("Home")
        self.route_title.setObjectName("routeTitle")
        header_layout.addWidget(self.route_title)
        header_layout.addStretch()
        self.connection_status = QLabel("Connection not checked")
        self.connection_status.setObjectName("connectionStatus")
        self.connection_status.hide()
        self.preferences.body.insertWidget(3, self.connection_status)
        # Visible only while background work runs, so it never reads as decoration.
        self.activity_mark = BuddyMark(22)
        self.activity_mark.setToolTip("Working…")
        self.activity_mark.hide()
        header_layout.addWidget(self.activity_mark)
        self.cancel_auth_button = QPushButton("Cancel sign-in")
        command(self.cancel_auth_button, "x")
        self.cancel_auth_button.setAccessibleName("Cancel browser sign-in")
        self.cancel_auth_button.setToolTip("Cancel the sign-in waiting in your browser")
        self.cancel_auth_button.clicked.connect(self.account.cancel_operation)
        self.cancel_auth_button.hide()
        header_layout.addWidget(self.cancel_auth_button)
        self.account_button = QPushButton("Sign in")
        self.account_button.setObjectName("accountButton")
        self.account_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.account_button.clicked.connect(self._show_account_controls)
        header_layout.addWidget(self.account_button)
        self.account_popover = QDialog(self, Qt.WindowType.Popup)
        self.account_popover.setObjectName("accountPopover")
        self.account_popover.setAccessibleName("Buddy account")
        self.account_popover.setFixedWidth(300)
        popover_layout = QVBoxLayout(self.account_popover)
        popover_layout.setContentsMargins(16, 16, 16, 12)
        popover_layout.setSpacing(12)
        identity = QHBoxLayout()
        identity.setSpacing(12)
        self.account_avatar = QLabel()
        self.account_avatar.setObjectName("avatar")
        self.account_avatar.setFixedSize(40, 40)
        self.account_avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        identity.addWidget(self.account_avatar)
        self.account_title = QLabel("Sign in to Buddy")
        self.account_title.setObjectName("sectionTitle")
        self.account_title.setTextFormat(Qt.TextFormat.PlainText)
        identity.addWidget(self.account_title, 1)
        popover_layout.addLayout(identity)
        self.account_divider = divider()
        popover_layout.addWidget(self.account_divider)
        menu = QVBoxLayout()
        menu.setSpacing(2)
        self.profile_action = QPushButton("Profile")
        command(self.profile_action, "circle-user-round")
        self.profile_action.setObjectName("menuItem")
        self.profile_action.setAccessibleName("View your profile")
        self.profile_action.clicked.connect(self._show_account)
        menu.addWidget(self.profile_action)
        menu.addWidget(self.account)
        popover_layout.addLayout(menu)
        self.account.session_changed.connect(self._account_identity)
        self.account.profile_loaded.connect(self._profile_identity)
        self._account_identity()
        self.library.signin_requested.connect(self._show_account_controls)
        workspace_layout.addWidget(header)
        workspace_layout.addWidget(self.pages, 1)
        layout.addWidget(workspace, 1)
        self.setCentralWidget(root)
        self._motion_changed(self.preferences.reduce_motion.isChecked())
        self.navigation.currentRowChanged.connect(self.navigate)
        self.navigation.setCurrentRow(0)
        self._create_menu()
        self.statusBar().setAccessibleName("Application status")
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().hide()
        geometry = settings.value("window/geometry")
        if isinstance(geometry, QByteArray):
            self.restoreGeometry(geometry)
            self._keep_window_visible()

    def _create_menu(self) -> None:
        workspace_menu = self.menuBar().addMenu("&Workspace")
        for index, label in enumerate(["&Home", "&Library", "&Review", "&Present", "&Settings"]):
            action = QAction(label, self)
            action.setShortcut(QKeySequence(f"Alt+{index + 1}"))
            action.triggered.connect(lambda checked=False, row=index: self._select_route(row))
            workspace_menu.addAction(action)
        workspace_menu.addSeparator()
        account_action = QAction("&Account profile", self)
        account_action.setShortcut(QKeySequence("Alt+6"))
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
        # macOS keeps its native global menu. Elsewhere a one-item menu strip is noise;
        # the window keeps every shortcut.
        if sys.platform != "darwin":
            self.addActions(workspace_menu.actions())
            self.menuBar().hide()

    def _open_saved_pdf(self, result: DownloadedPDF) -> None:
        if self.present.executor.busy:
            return
        self._private_presentation = True
        self.navigation.setCurrentRow(3)
        self.present.load_pdf(result.content, deck_id=result.deck_id, revision=result.revision)

    def _open_review(
        self, deck_id: object, title: str, status: str, source_format: str = "pdf"
    ) -> None:
        if not isinstance(deck_id, UUID):
            return
        self.review.set_deck(deck_id, title, status, source_format)
        self.navigation.setCurrentRow(2)

    def _clear_private_pdf(self) -> None:
        if self._private_presentation:
            self._private_presentation = False
            if self.present.fullscreen is not None:
                self.present.fullscreen.close()
            self.present.executor.clear()
            self.present.counter.setText("No slides loaded")
            self.home.workspace_state.hide()
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
        self.account_popover.hide()
        self.navigation.setCurrentRow(-1)
        self.navigate(5)
        self.profile.controls.setFocus()

    @Slot()
    def _show_account_controls(self) -> None:
        if self.account_popover.isVisible():
            self.account_popover.hide()
            return
        self._account_identity()
        self._fit_account_popover()
        anchor = self.account_button.mapToGlobal(self.account_button.rect().bottomRight())
        screen = self.account_button.screen().availableGeometry()
        self.account_popover.move(
            max(
                screen.left(),
                min(
                    anchor.x() - self.account_popover.width(),
                    screen.right() - self.account_popover.width(),
                ),
            ),
            max(screen.top(), min(anchor.y() + 8, screen.bottom() - self.account_popover.height())),
        )
        if self.account.account_id and not self.account.runner.busy and not self._verified_profile:
            self.account.start("profile")
        self.account_popover.show()
        self._fit_account_popover()
        self.account_popover.activateWindow()
        target = self.profile_action if self.account.account_id else self.account.buttons["signin"]
        target.setFocus()

    def _fit_account_popover(self) -> None:
        # The menu changes content between account states; a re-shown popup otherwise
        # restores its previous, taller geometry instead of shrinking to fit.
        layout = self.account_popover.layout()
        if layout is not None:
            layout.activate()
        self.account_popover.resize(
            self.account_popover.width(), self.account_popover.sizeHint().height()
        )

    def _account_identity(self) -> None:
        signed_in = bool(self.account.account_id)
        self.profile_action.setVisible(signed_in)
        self.account_divider.setVisible(signed_in)
        # Never retain an email across sign-out, expiry, or backend changes.
        if not signed_in or (
            self._verified_profile and self._verified_profile.id != self.account.account_id
        ):
            self._verified_profile = None
        email = self._verified_profile.email if self._verified_profile else ""
        initial = email[:1].upper()
        # Long addresses are elided in the menu; the tooltip and Profile show them in full.
        self.account_title.setText(
            self.account_title.fontMetrics().elidedText(
                email or ("Your account" if signed_in else "Sign in to Buddy"),
                Qt.TextElideMode.ElideMiddle,
                300 - 32 - (52 if signed_in else 0),
            )
        )
        self.account_title.setToolTip(email)
        self.account_avatar.setVisible(signed_in)
        if initial:
            self.account_avatar.setText(initial)
        else:
            self.account_avatar.setPixmap(icon("circle-user-round", "#FFF4EC").pixmap(20, 20))
        button = self.account_button
        button.setProperty("avatar", signed_in)
        if signed_in:
            button.setFixedSize(32, 32)
            button.setText(initial)
            button.setIcon(QIcon() if initial else icon("circle-user-round", "#FFF4EC"))
            button.setAccessibleName("Open your account menu")
        else:
            button.setMinimumSize(0, 0)
            button.setMaximumSize(16777215, 16777215)
            button.setText("Sign in")
            button.setIcon(QIcon())
            button.setAccessibleName("Sign in")
        button.setToolTip(email or button.accessibleName())
        button.style().unpolish(button)
        button.style().polish(button)
        if self.account_popover.isVisible():
            self._fit_account_popover()

    def _profile_identity(self, _identifier: int, profile: object) -> None:
        if isinstance(profile, AccountProfile) and profile.id == self.account.account_id:
            self._verified_profile = profile
            self._account_identity()

    @Slot(int)
    def navigate(self, index: int) -> None:
        if 0 <= index < self.pages.count():
            previous = self.pages.currentIndex()
            self.pages.setCurrentIndex(index)
            self.route_title.setText(
                ("Home", "Library", "Review", "Present", "Settings", "Profile")[index]
            )
            self.profile.activate(index == 5)
            self.library.activate(index == 1)
            self.review.activate(index == 2)
            if index == 3:
                self.statusBar().showMessage(
                    "PDF presentation controls run locally after opening or downloading the file."
                )
            elif previous == 3:
                self.statusBar().clearMessage()

    @Slot(bool)
    def _motion_changed(self, reduced: bool) -> None:
        self.settings.setValue("appearance/reduce_motion", reduced)
        for mark in self.findChildren(BuddyMark):
            mark.reduced_motion = reduced
            mark.set_activity(mark.active)

    @Slot()
    def check_connection(self) -> None:
        if self.runner.busy or self._closing:
            return
        self.preferences.connection.show_state("Connecting…", "Checking your Buddy backend.")
        self.connection_status.setText("Checking connection")
        self.activity_mark.show()
        self.activity_mark.set_activity(True)
        self.home.mascot.set_activity(True)
        self.preferences.check.setEnabled(False)
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
        self.preferences.connection.show_state(
            "Backend connected", "Your Buddy service reports healthy."
        )
        self.connection_status.setText("Backend connected")
        self.statusBar().showMessage("Backend connected.")

    @Slot(int, str)
    def _health_failed(self, identifier: int, message: str) -> None:
        if identifier != self._active_task or self._closing:
            return
        self.preferences.connection.show_state("Connection unavailable", message, retry=True)
        self.connection_status.setText("Connection unavailable")
        self.statusBar().showMessage(message)

    @Slot()
    def _task_idle(self) -> None:
        self._active_task = None
        self._update_activity()
        self.home.mascot.set_activity(False)
        if self._closing:
            self.close()
        else:
            self.preferences.check.setEnabled(True)
            self.preferences.save.setEnabled(
                not self.environment_override
                and not self.account.runner.busy
                and not self.library.runner.busy
                and not self.review.runner.busy
            )

    @Slot(bool)
    def _account_activity(self, busy: bool) -> None:
        self._account_busy = busy
        self.cancel_auth_button.setVisible(
            busy and self.account.operation in {"signin", "signup", "recovery"}
        )
        if busy and self.account.operation != "profile":
            self._verified_profile = None
            self.account_button.setToolTip("Account operation in progress")
            self.account_title.setText("Checking account…")
            self.account_title.setToolTip("")
        self._update_activity()
        self.preferences.save.setEnabled(
            not busy
            and not self.runner.busy
            and not self.library.runner.busy
            and not self.review.runner.busy
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
            and not self.review.runner.busy
            and not self.environment_override
        )
        if self._closing and not busy:
            self.close()

    def _review_activity(self, busy: bool) -> None:
        self._review_busy = busy
        self._update_activity()
        self.preferences.save.setEnabled(
            not busy
            and not self.runner.busy
            and not self.account.runner.busy
            and not self.library.runner.busy
            and not self.environment_override
        )
        if self._closing and not busy:
            self.close()

    def _update_activity(self) -> None:
        busy = (
            self._active_task is not None
            or self._account_busy
            or self._library_busy
            or getattr(self, "_review_busy", False)
        )
        self.activity_mark.setVisible(busy)
        self.activity_mark.set_activity(busy)

    @Slot()
    def _present_idle(self) -> None:
        self.present._refresh()
        if self.present.surface.slide is not None:
            self.home.workspace_state.setText(f"Presenting · {self.present.counter.text()}")
            self.home.workspace_state.show()
        if self._closing:
            self.close()

    @Slot()
    def _powerpoint_idle(self) -> None:
        self.present._refresh()
        if self._closing:
            self.close()

    @Slot(str)
    def save_settings(self, value: str) -> None:
        if (
            self.runner.busy
            or self.account.runner.busy
            or self.library.runner.busy
            or self.review.runner.busy
            or self.review.cleanup_runner.busy
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
        self.review.configure(self.config)
        self.preferences.url.setText(url)
        self.preferences.set_endpoint(url)
        self.connection_status.setText("Connection not checked")
        self.preferences.connection.show_state(
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
        self.account_popover.hide()
        self.profile.activate(False)
        self.account.shutdown()
        self.library.shutdown()
        self.review.shutdown()
        self.present.shutdown()
        if (
            self.runner.busy
            or self.account.runner.busy
            or self.library.runner.busy
            or self.review.runner.busy
            or self.present.executor.busy
            or self.review.cleanup_runner.busy
            or self.present.powerpoint_runner.busy
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
