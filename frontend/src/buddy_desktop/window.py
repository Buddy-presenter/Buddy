"""Navigation and lifecycle for one desktop window and one application event loop."""

import importlib
import sys
import time
from dataclasses import replace
from uuid import UUID

import shiboken6
from PySide6.QtCore import QByteArray, QRect, QSettings, QSize, Qt, QTimer, Slot
from PySide6.QtGui import QAction, QCloseEvent, QGuiApplication, QIcon, QKeySequence, QScreen
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
from buddy_desktop.auth import AccountProfile, AccountSession
from buddy_desktop.browser_search import launch_chrome_search
from buddy_desktop.buddy_mini import BuddyMini, mini_caption
from buddy_desktop.config import AppConfig, ConfigurationError, validate_api_url
from buddy_desktop.information_search_panel import InformationSearchPanel
from buddy_desktop.information_search_service import InformationAnswer, InformationSearchService
from buddy_desktop.library_service import AccessProvider, LibraryAccessExpired
from buddy_desktop.library_widget import LibraryWidget
from buddy_desktop.present_widget import PresentWidget
from buddy_desktop.presentation_service import DownloadedPDF
from buddy_desktop.profile_page import ProfilePage
from buddy_desktop.review_widget import ReviewWidget
from buddy_desktop.services import Health, HealthService, ServiceError
from buddy_desktop.slide_executor import Completion
from buddy_desktop.tasks import TaskRunner
from buddy_desktop.views import HomePage, SettingsPage, divider
from buddy_desktop.visuals import BuddyMark, app_icon, command, icon
from buddy_desktop.window_focus import (
    activate_native_window,
    activate_qt_window,
    allow_current_process_foreground,
    native_window_rect,
)
from buddy_identity.config import IdentityError, SessionExpired


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
        self._present_access: tuple[AccountSession, AccessProvider] | None = None
        self.runner = TaskRunner(self)
        self.runner.succeeded.connect(self._health_succeeded)
        self.runner.failed.connect(self._health_failed)
        self.runner.idle.connect(self._task_idle)
        self.information_search_runner = TaskRunner(self)
        self.information_search_runner.succeeded.connect(self._information_search_succeeded)
        self.information_search_runner.failed.connect(self._information_search_failed)
        self.information_search_runner.idle.connect(self._information_search_idle)
        self._information_search_generation = 0
        self._information_search_task: int | None = None
        self._information_search_task_generation: int | None = None
        self._queued_information_search: tuple[int, str, bool, AccessProvider] | None = None
        self._reduce_motion = bool(settings.value("appearance/reduce_motion", False, type=bool))
        self._search_panel: InformationSearchPanel | None = None
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
        self.present = PresentWidget(config, settings)
        self.present.web_search_requested.connect(self._open_voice_search)
        self.present.return_to_presentation_requested.connect(self._return_to_presentation_by_voice)
        self.present.scroll_panel_requested.connect(self._scroll_information_search)
        self._mini: BuddyMini | None = None
        self._mini_session: str | None = None
        self._mini_screen: QScreen | None = None
        self._mini_chime_paused = False
        self._mini_poll = QTimer(self, interval=100)
        self._mini_poll.timeout.connect(self._sync_mini)
        self._mini_auto_poll = QTimer(self, interval=100)
        self._mini_auto_poll.timeout.connect(self._maybe_auto_mini)
        self._mini_auto_attempts = 0
        self._mini_auto_deck: str | None = None
        self.present.mini_requested.connect(self._toggle_mini)
        self.present.fullscreen_exited.connect(self._mini_fullscreen_exited)
        self.present.fullscreen_exited.connect(self._close_information_search)
        self.present.transition_observed.connect(self._mini_completed)
        app = QGuiApplication.instance()
        if isinstance(app, QGuiApplication):
            app.screenRemoved.connect(self._mini_screen_removed)
        self._private_presentation = False
        self.library.present_requested.connect(self._open_saved_pdf)
        self.library.powerpoint_present_requested.connect(self._open_saved_powerpoint)
        self.library.review_requested.connect(self._open_review)
        self.library.desktop_check_needed.connect(self._open_desktop_check)
        self.review.powerpoint_present_requested.connect(self._present_saved_powerpoint)
        self.review.powerpoint_invalidated.connect(self.present.invalidate_saved_powerpoint)
        self.review.back_requested.connect(lambda: self.navigation.setCurrentRow(1))
        self.review.signin_requested.connect(self._show_account_controls)
        self.library.private_invalidated.connect(self._clear_private_pdf)
        self.library.deck_deleted.connect(self._deck_deleted)
        self.present.executor.completed.connect(self._presentation_completed)
        self.present.executor.idle.connect(self._present_idle)
        self.present.powerpoint_runner.idle.connect(self._powerpoint_idle)
        self.present.policy_runner.idle.connect(self._powerpoint_idle)
        self.present.generated_runner.idle.connect(self._powerpoint_idle)
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
        self.preferences.mini_corner.setCurrentIndex(
            max(
                0,
                self.preferences.mini_corner.findData(
                    settings.value("presentation/mini_corner", "bottom-right")
                ),
            )
        )
        self.preferences.mini_corner.currentIndexChanged.connect(
            lambda _: settings.setValue(
                "presentation/mini_corner", self.preferences.mini_corner.currentData()
            )
        )
        self.preferences.mini_auto_start.setChecked(
            bool(settings.value("presentation/mini_auto_start", False, type=bool))
        )
        self.preferences.mini_auto_start.toggled.connect(
            lambda enabled: settings.setValue("presentation/mini_auto_start", enabled)
        )
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
        self.present.session_access_expired.connect(self.account.verify_resource_access)
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
        self._schedule_auto_mini(result.deck_id)

    def _open_review(
        self, deck_id: object, title: str, status: str, source_format: str = "pdf"
    ) -> None:
        if not isinstance(deck_id, UUID):
            return
        self.review.set_deck(deck_id, title, status, source_format)
        # Share the server's check availability so Review and Library agree.
        page = self.library.last_page
        deck = next((d for d in page.items if d.id == deck_id), None) if page else None
        checks = deck.processing.checks if deck and deck.processing else None
        self.review.checks_available = checks.available if checks else None
        self.navigation.setCurrentRow(2)

    def _open_desktop_check(self, deck: object) -> None:
        """Open a just-processed PowerPoint in Review, which announces its checks."""
        if (
            not self.review.auto_checks
            or self.navigation.currentRow() == 3  # never interrupt Present
            or self.review.runner.busy
            or not self.review.check_overlay.isHidden()
        ):
            return
        deck_id = getattr(deck, "id", None)
        if not isinstance(deck_id, UUID):
            return
        self._open_review(deck_id, deck.title, deck.status, "pptx")  # type: ignore[attr-defined]

    def _open_saved_powerpoint(self, deck_id: object, title: str, status: str) -> None:
        if isinstance(deck_id, UUID):
            self.review.set_deck(deck_id, title, status, "pptx", present=True)
            self.navigation.setCurrentRow(2)

    def _present_saved_powerpoint(self, binding: object) -> None:
        from buddy_desktop.powerpoint_session_binding import PowerPointSessionBinding

        if not isinstance(binding, PowerPointSessionBinding):
            return
        if not self.account.account_id or not self.review.token():
            return
        self.navigation.setCurrentRow(3)
        self.present.bind_saved_powerpoint(binding)
        self._schedule_auto_mini(binding.deck_id)

    def _clear_private_pdf(self) -> None:
        if self._private_presentation:
            self._private_presentation = False
            self.present.clear_session_binding()
            if self.present.fullscreen is not None:
                self.present.fullscreen.close()
            self.present.executor.clear()
            self.present.counter.setText("No slides loaded")
            self.home.workspace_state.hide()
            self.present._refresh()

    def _deck_deleted(self, identifier: object) -> None:
        if not isinstance(identifier, UUID):
            return
        if self.review.deck_id == identifier:
            self.review.clear_private()
            self.review.state.show_state(
                "Presentation removed",
                "Its saved review is no longer available. Buddy is finishing permanent cleanup.",
            )
        if self.present._session_deck_id == str(identifier):
            self._private_presentation = False
            self.present.clear_session_binding()
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

    def _access_provider(self, session: AccountSession) -> AccessProvider:
        """One provider per sign-in that always returns a current access token.

        Access tokens can last only minutes while a presentation lasts much longer, so
        live presentation work must never keep a copy. The margin covers the longest
        single use of one token: a session event stream that the server re-checks for
        150 seconds.
        """
        cached = self._present_access
        if cached is not None and cached[0] is session:
            return cached[1]

        def access() -> str:
            try:
                return session.fresh_access(margin=180)
            except SessionExpired:
                raise LibraryAccessExpired("Buddy sign-in expired. Please sign in again.") from None
            except IdentityError as exc:
                raise ServiceError(str(exc)) from None

        self._present_access = (session, access)
        return access

    def _account_identity(self) -> None:
        signed_in = bool(self.account.account_id)
        session = self.account.session
        tokens = session.tokens if session is not None else None
        self.present.set_account_identity(
            self.account.account_id if signed_in else None,
            self._access_provider(session)
            if signed_in and session is not None and tokens is not None
            else None,
        )
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
            if index != 3:
                self._mini_auto_poll.stop()
                if self._information_search_panel().isVisible():
                    self._close_information_search()
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
        if busy and self.account.operation not in {"check", "profile"}:
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
        self.present.configure(self.config)
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

    def _schedule_auto_mini(self, deck_id: str) -> None:
        self._mini_auto_poll.stop()
        if self.preferences.mini_auto_start.isChecked():
            self._mini_auto_deck = deck_id
            self._mini_auto_attempts = 0
            self._mini_auto_poll.start()

    @Slot(str, bool)
    def _open_voice_search(self, query: str, images: bool, browser: bool = False) -> None:
        self._information_search_generation += 1
        generation = self._information_search_generation
        self._queued_information_search = None
        if browser:
            self._information_search_panel().close_panel()
            # Windows grants the current foreground process a narrow foreground
            # handoff before Chrome takes focus. No shell command is constructed.
            allow_current_process_foreground()
            result = launch_chrome_search(query, images=images)
            if not result.opened:
                self._show_voice_command_feedback(result.message, 10000)
            return

        fullscreen_owner = self.present.fullscreen
        owner: QWidget | QRect = self
        slideshow_area = self._powerpoint_slideshow_area()
        if slideshow_area is not None:
            owner = slideshow_area
        elif fullscreen_owner is not None and fullscreen_owner.isVisible():
            owner = fullscreen_owner
        elif not self.isVisible() or self.isMinimized():
            # Buddy Mini hides the workspace: show the panel on the screen in use.
            screen = self.screen() or QGuiApplication.primaryScreen()
            if screen is not None:
                owner = screen.availableGeometry()
        self._information_search_panel().present(owner, query, images=images)
        session = self.account.session
        if session is None or not self.account.account_id:
            self._information_search_panel().show_error(
                "Sign in to Buddy to search the web from your presentation."
            )
            return
        access = self._access_provider(session)
        if self.information_search_runner.busy:
            self._queued_information_search = (generation, query, images, access)
            return
        self._submit_information_search(generation, query, images, access)

    def _information_search_panel(self) -> InformationSearchPanel:
        """The search panel, recreated if anything ever deleted it (#250)."""
        panel = self._search_panel
        if panel is None or not shiboken6.isValid(panel):
            panel = InformationSearchPanel(reduce_motion=self._reduce_motion)
            panel.close_requested.connect(self._close_information_search)
            self._search_panel = panel
        return panel

    def _powerpoint_slideshow_area(self) -> QRect | None:
        """The screen area of a live PowerPoint slideshow in Qt coordinates (#250).

        Windows reports physical pixels; Qt places windows in scaled pixels. A slideshow
        fills one monitor, so use the monitor whose physical area contains its centre.
        """
        slideshow = self.present.powerpoint_slideshow_window()
        rect = native_window_rect(slideshow) if slideshow is not None else None
        if rect is None:
            return None
        left, top, right, bottom = rect
        centre_x, centre_y = (left + right) // 2, (top + bottom) // 2
        for screen in QGuiApplication.screens():
            area, ratio = screen.geometry(), screen.devicePixelRatio()
            physical = QRect(
                area.x(),
                area.y(),
                round(area.width() * ratio),
                round(area.height() * ratio),
            )
            if physical.contains(centre_x, centre_y):
                return area
        primary = QGuiApplication.primaryScreen()
        return primary.geometry() if primary is not None else None

    @Slot()
    def _return_to_presentation_by_voice(self) -> None:
        self._information_search_generation += 1
        self._queued_information_search = None
        had_information_panel = self._information_search_panel().isVisible()
        if had_information_panel:
            self._information_search_panel().close_panel()
        slideshow = self.present.powerpoint_slideshow_window()
        if slideshow is not None:
            # PowerPoint presents in its own slideshow window, not in Buddy (#236).
            if activate_native_window(slideshow):
                if not had_information_panel:
                    self._show_voice_command_feedback("Returned to the PowerPoint slideshow.", 4000)
            else:
                self._show_voice_command_feedback(
                    "Windows did not focus the PowerPoint slideshow. "
                    "Say “return to the presentation” again.",
                    10000,
                )
            return
        fullscreen = self.present.fullscreen
        target = fullscreen if fullscreen is not None and fullscreen.isVisible() else self
        if activate_qt_window(target):
            if not had_information_panel:
                self._show_voice_command_feedback("Returned to the presentation.", 4000)
        else:
            self._show_voice_command_feedback(
                "Windows did not focus Buddy. Say “return to the presentation” again.",
                10000,
            )

    @Slot(int)
    def _scroll_information_search(self, direction: int) -> None:
        if self._information_search_panel().isVisible():
            self._information_search_panel().scroll_by(direction)

    def _close_information_search(self) -> None:
        self._information_search_generation += 1
        self._queued_information_search = None
        self._information_search_panel().close_panel()

    def _submit_information_search(
        self, generation: int, query: str, images: bool, access: AccessProvider
    ) -> None:
        if generation != self._information_search_generation or self._closing:
            return
        self._information_search_task_generation = generation
        self._information_search_task = self.information_search_runner.submit(
            lambda: InformationSearchService(self.config).answer(access, query, images=images)
        )

    @Slot(int, object)
    def _information_search_succeeded(self, identifier: int, result: object) -> None:
        if (
            identifier != self._information_search_task
            or self._information_search_task_generation != self._information_search_generation
            or self._closing
            or not isinstance(result, InformationAnswer)
            or not self._information_search_panel().isVisible()
        ):
            return
        self._information_search_panel().show_answer(result)

    @Slot(int, str)
    def _information_search_failed(self, identifier: int, message: str) -> None:
        if (
            identifier != self._information_search_task
            or self._information_search_task_generation != self._information_search_generation
            or self._closing
            or not self._information_search_panel().isVisible()
        ):
            return
        self._information_search_panel().show_error(message)

    @Slot()
    def _information_search_idle(self) -> None:
        self._information_search_task = None
        self._information_search_task_generation = None
        queued, self._queued_information_search = self._queued_information_search, None
        if queued is not None and not self._closing:
            generation, query, images, access = queued
            self._submit_information_search(generation, query, images, access)
        if self._closing:
            self.close()

    def _show_voice_command_feedback(self, message: str, timeout_ms: int) -> None:
        fullscreen = self.present.fullscreen
        if fullscreen is not None and fullscreen.isVisible():
            fullscreen.show_voice_feedback(message, timeout_ms)
        else:
            self.present.show_voice_command_status(message, timeout_ms)

    def _maybe_auto_mini(self) -> None:
        self._mini_auto_attempts += 1
        if self._closing or self._mini is not None or self._mini_auto_attempts > 150:
            self._mini_auto_poll.stop()
            return
        session = self.present._session
        if (
            session is not None
            and session.deck_id == self._mini_auto_deck
            and self.present.mini_session_id()
        ):
            self._mini_auto_poll.stop()
            self._toggle_mini()

    def _toggle_mini(self) -> None:
        self._mini_auto_poll.stop()
        if self._mini is not None:
            self._mini.show()
            self.hide()
            self._sync_mini()
            return
        session_id = self.present.mini_session_id()
        if session_id and self.present._powerpoint_info is None and self.present.fullscreen is None:
            self.present.enter_fullscreen()
        screen = self.screen()
        audience = self._mini_audience_screen()
        screens = QGuiApplication.screens()
        if len(screens) > 1 and screen == audience:
            screen = next(candidate for candidate in screens if candidate != audience)
        mini = BuddyMini(
            self.present.microphone_button,
            self.present.policy_undo_button,
        )
        mini.restore_requested.connect(self._leave_mini)
        mini.settings_requested.connect(self._mini_settings)
        mini.automation_requested.connect(self._toggle_mini_automation)
        mini.closed.connect(lambda: self._leave_mini(stop=True))
        mini.system_sleep.connect(self._mini_system_sleep, Qt.ConnectionType.QueuedConnection)
        mini.chime_finished.connect(self._mini_chime_finished)
        mini.reduced_motion = self.preferences.reduce_motion.isChecked()
        if self.present.mini_chime.isChecked():
            mini.prepare_chime()
        self._mini, self._mini_session, self._mini_screen = mini, session_id, screen
        self._position_mini(mini, screen)
        if len(screens) == 1:
            mini.setToolTip("Buddy Mini may be visible when sharing this entire screen.")
        mini.show()
        self.present.mini_active = True
        self.hide()
        self._mini_poll.start()
        self._sync_mini()

    def _position_mini(self, mini: BuddyMini, screen: QScreen) -> None:
        area = screen.availableGeometry()
        corner = self.preferences.mini_corner.currentData()
        top = corner in {"top-left", "top-right"}
        left = corner in {"top-left", "bottom-left"}
        mini.set_top_corner(top)
        mini.move(
            area.left() + 12 if left else area.right() - mini.width() - 12,
            area.top() + 12 if top else area.bottom() - mini.height() - 12,
        )

    def _mini_audience_screen(self) -> QScreen | None:
        if self.present.fullscreen is not None and self.present._powerpoint_info is None:
            return self.present.fullscreen.screen()
        info = self.present._powerpoint_info
        if sys.platform == "win32" and info is not None:
            win32api = importlib.import_module("win32api")

            try:
                device = win32api.GetMonitorInfo(win32api.MonitorFromWindow(info.window_id))[
                    "Device"
                ]
                return next(
                    (
                        screen
                        for screen in QGuiApplication.screens()
                        if screen.name().casefold() == device.casefold()
                    ),
                    None,
                )
            except (OSError, ValueError, win32api.error):
                return None
        return None

    def _sync_mini(self) -> None:
        mini = self._mini
        if mini is None:
            return
        page = self.present
        session_id = page.mini_session_id()
        # A PDF render can stay loaded after fullscreen ends. Treat that as
        # standby until the presenter starts the slideshow again.
        if page._powerpoint_info is None and page.fullscreen is None:
            session_id = None
        if not mini.isVisible() or session_id != self._mini_session:
            self._mini_chime_paused = False
            mini.stop_sound()
            page.microphone.stop()
            page.agent_enabled.setChecked(False)
            self._mini_session = session_id
            if not mini.isVisible():
                mini.show()
        screens = QGuiApplication.screens()
        if screens:
            audience = self._mini_audience_screen()
            presenters = [screen for screen in screens if screen != audience] or screens
            if self._mini_screen not in presenters:
                self._mini_screen = presenters[0]
                self._position_mini(mini, self._mini_screen)
        state = (
            "Disconnected"
            if session_id is None
            else "Muted"
            if page.microphone_mute.isChecked() or page.microphone_pause.isChecked()
            else "Listening"
            if page.microphone_status.text() == "Microphone on"
            else "Mic off"
        )
        mini.sync(
            state, page.microphone_button, page.policy_undo_button, connected=bool(session_id)
        )
        mini.set_caption(mini_caption(state, page.live_caption, time.monotonic()))
        mini.sync_automation(
            bool(session_id and page.agent_enabled.isEnabled()),
            bool(session_id and page.agent_enabled.isChecked() and page.policy_enabled.isChecked()),
        )

    def _toggle_mini_automation(self) -> None:
        page = self.present
        if not self._mini_session:
            return
        if page.agent_enabled.isChecked() and page.policy_enabled.isChecked():
            page.agent_enabled.setChecked(False)
        elif page.agent_enabled.isEnabled():
            suggestions_were_on = page.agent_enabled.isChecked()
            page.agent_enabled.setChecked(True)
            page._update_agent()
            if page.policy_enabled.isEnabled():
                page.policy_enabled.setChecked(True)
            else:
                page.agent_enabled.setChecked(suggestions_were_on)
        self._sync_mini()

    def _mini_completed(self, result: object) -> None:
        if self._mini is None or not self._mini.isVisible():
            return
        page = self.present
        target = result.get("target", {}) if isinstance(result, dict) else {}
        tool = target.get("tool", "") if isinstance(target, dict) else ""
        label = {
            "zoom_region": "Zoomed in",
            "highlight_region": "Highlighted",
            "reset_view": "View reset",
        }.get(tool)
        if label is None:
            info = page._powerpoint_info
            number = info.slide_index if info is not None else page.executor.current_index + 1
            label = f"Slide {number}"
        self._mini.completed(label)
        if page.mini_chime.isChecked() and self._mini.prepare_chime():
            self._mini_chime_paused = (
                page.microphone.enabled
                and not page.microphone_pause.isChecked()
                and not page.microphone_mute.isChecked()
            )
            if self._mini_chime_paused:
                page.microphone_pause.setChecked(True)
                page._invalidate_transcript_context()
            self._mini.play_chime()

    def _mini_chime_finished(self) -> None:
        if self._mini_chime_paused:
            self._mini_chime_paused = False
            if self._mini is not None and self.present.mini_session_id() == self._mini_session:
                self.present.microphone_pause.setChecked(False)

    def _mini_screen_removed(self, screen: QScreen) -> None:
        if self._mini is not None and screen == self._mini_screen:
            self._mini_screen = None
            QTimer.singleShot(0, self._sync_mini)

    @Slot()
    def _mini_system_sleep(self) -> None:
        # Windows can send both suspend and resume, including for a Mini already closing.
        if self._mini is not None and self.sender() is self._mini:
            self._leave_mini(stop=True)

    def _mini_settings(self) -> None:
        self.present.microphone.stop()
        self.present.agent_enabled.setChecked(False)
        self.show()
        self.raise_()
        self.activateWindow()
        self.navigation.setCurrentRow(4)

    def _mini_fullscreen_exited(self) -> None:
        if self._mini is not None:
            self._leave_mini(stop=True, restore=not self._closing)

    def _leave_mini(self, *, stop: bool = False, restore: bool = True) -> None:
        mini, self._mini = self._mini, None
        if mini is None:
            return
        fullscreen = self.present.fullscreen
        stop = stop or mini.chiming or fullscreen is not None
        self._mini_chime_paused = False
        self._mini_poll.stop()
        self.present.mini_active = False
        self._mini_session = None
        self._mini_screen = None
        # Clear Mini ownership first: closing the audience window emits back into
        # _mini_fullscreen_exited. Both Escape targets must restore the surface.
        if fullscreen is not None:
            fullscreen.close()
        if stop:
            self.present.microphone.stop()
            self.present.agent_enabled.setChecked(False)
        mini.stop_sound()
        mini.hide()
        mini.deleteLater()
        if restore:
            self.show()
            self.raise_()
            self.activateWindow()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._mini_auto_poll.stop()
        self._leave_mini(stop=True, restore=False)
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
            or self.present.session_runner.busy
            or self.review.cleanup_runner.busy
            or self.present.powerpoint_runner.busy
            or self.present.policy_runner.busy
            or self.present.generated_runner.busy
            or self.information_search_runner.busy
        ):
            self._closing = True
            self.runner.stop()
            self.information_search_runner.stop()
            self._information_search_panel().close_panel()
            central = self.centralWidget()
            if central is not None:
                central.setEnabled(False)
            self.statusBar().showMessage("Closing after the current operation finishes…")
            event.ignore()
            return
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.sync()
        event.accept()
