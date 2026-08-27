"""
gui/main_window.py

Main window of the application.

Structure (per spec):
    * Menu bar
    * Sidebar (navigation: Setup / Live View / Analysis / Data management)
    * Workspace (QStackedWidget holding the individual views)
    * Status bar

The main window is the only GUI component that knows the
`MeasurementController` directly. It translates view signals (e.g. "start
measurement") into controller calls and distributes results back to the
views. This keeps the individual views decoupled from the control logic.

Thread safety:
    Errors from the DAQ thread reach the main window via the controller's
    error listener, which runs IN THE DAQ THREAD. To only touch Qt widgets
    from the GUI thread, the error is marshalled into the GUI thread via a
    Qt signal (`_acquisition_error_signal`) - Qt automatically delivers a
    signal-slot connection emitted across thread boundaries as a
    `QueuedConnection`, so the slot executes on the GUI thread.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QSize, pyqtSignal, Qt
from PyQt6.QtGui import QAction, QActionGroup, QIcon, QKeySequence
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QGraphicsDropShadowEffect,
    QStackedWidget,
    QWidget,
)

from config.configuration_manager import ConfigurationManager
from config.sensor_database import SensorDatabaseManager
from config.settings import APP_VERSION, get_resource_path
from core.controller import MeasurementController
from data.exporter import StorageWriter
from data.metadata import build_measurement_metadata, save_measurement_metadata
from data.models import (
    DeviceInfo,
    MeasurementConfig,
    MeasurementSession,
    StorageFormat,
    TriggerConfig,
    TriggerKind,
    resolve_rate_groups,
)
from gui.analysis_view import AnalysisView
from gui.i18n import connect_language_changed, get_language, set_language, t
from gui.live_view import LiveView
from gui.serial_trigger import SerialTriggerListener
from gui.setup_view import NamingScheme, SetupView
from gui.trigger_settings_dialog import TriggerSettingsDialog
from gui.workers import BackgroundWorker
from gui.theme import (
    RECORD_ICON_COLOR,
    connect_theme_changed,
    draw_magnifier_icon,
    draw_gear_icon,
    draw_play_icon,
    draw_record_icon,
    nav_shadow_color,
    get_theme,
    is_position_on_screen,
    repolish,
    set_theme,
)

logger = logging.getLogger(__name__)

_VIEW_SETUP = 0
_VIEW_LIVE = 1
_VIEW_ANALYSIS = 2

# (Row index, i18n key, icon-draw function) per navigation tile - a single
# source of truth for `_build_navigation_and_workspace()` and
# `retranslate_ui()`. Custom, theme-aware icons instead of
# QStyle.standardIcon() (see gui/theme.py - Qt's standard icons are NOT
# palette-aware here).
_NAV_ITEMS = [
    (_VIEW_SETUP, "nav_setup", draw_gear_icon),
    (_VIEW_LIVE, "nav_live_view", draw_play_icon),
    (_VIEW_ANALYSIS, "analysis", draw_magnifier_icon),
]


class MainWindow(QMainWindow):
    """Central application window with navigation and views."""

    # Signal for marshalling DAQ-thread errors into the GUI thread.
    _acquisition_error_signal = pyqtSignal(object)

    def __init__(
        self,
        controller: MeasurementController,
        configuration_manager: ConfigurationManager,
        sensor_database: SensorDatabaseManager | None = None,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._configuration_manager = configuration_manager
        self._sensor_database = sensor_database or SensorDatabaseManager()

        # References to running background workers (see gui/workers.py)
        # - must be kept alive until they finish, otherwise Python would
        # garbage-collect the QThread object prematurely.
        self._background_workers: list[BackgroundWorker] = []
        self._discovery_worker: BackgroundWorker | None = None

        self._storage_writer: StorageWriter | None = None
        last_storage = self._configuration_manager.settings.last_storage_path
        self._storage_path: Path | None = Path(last_storage) if last_storage else None
        # Prevents an automatic re-arm (`TriggerConfig.auto_rearm`) from
        # starting a new measurement while the app is closing (see
        # `closeEvent`/`_on_stop_measurement`).
        self._closing = False

        # State for automatic measurement triggers (see
        # `data/models.py::TriggerConfig`, `_on_start_measurement`). Owns
        # the current configuration itself (no longer the setup view, see
        # `gui/trigger_settings_dialog.py`) - preset with the last used
        # configuration.
        self._trigger_config: TriggerConfig = TriggerConfig.from_dict(
            configuration_manager.settings.last_trigger_config
        )
        self._start_serial_listener: SerialTriggerListener | None = None
        self._stop_serial_listener: SerialTriggerListener | None = None
        # False during the armed phase (start trigger not yet fired) -
        # controls whether `_on_stop_measurement` writes metadata (see
        # there).
        self._recording_started: bool = False

        self.setWindowTitle(t("window_title"))
        # .ico instead of .png (see main.py) - multiple resolutions for the
        # title bar/taskbar instead of a single 256px size.
        icon_path = get_resource_path("icon.ico")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self._restore_window_geometry()

        self._setup_view = SetupView(configuration_manager, self._sensor_database)
        self._live_view = LiveView(controller)
        self._analysis_view = AnalysisView()

        self._build_navigation_and_workspace()
        self._build_menu()
        self._build_shortcuts()
        self._build_status_bar()

        if self._storage_path is not None:
            self._setup_view.set_storage_path(str(self._storage_path))

        # Signal connections of the views
        self._setup_view.discover_hardware_requested.connect(self._on_discover_hardware)
        self._setup_view.open_ni_max_requested.connect(self._on_open_ni_max)
        self._setup_view.start_measurement_requested.connect(self._on_start_measurement)
        self._setup_view.stop_requested.connect(self._on_stop_measurement)
        self._setup_view.storage_path_requested.connect(self._on_choose_storage_path)
        self._setup_view.trigger_arm_toggled.connect(self._on_trigger_arm_toggled)
        self._live_view.start_requested.connect(self._on_start_measurement_from_live)
        self._live_view.stop_requested.connect(self._on_stop_measurement)
        self._live_view.trigger_fired.connect(self._on_trigger_fired)
        self._live_view.trigger_arm_toggled.connect(self._on_trigger_arm_toggled)
        self._live_view.plot_columns_changed.connect(
            self._configuration_manager.update_live_view_plot_columns
        )
        # Restore the persisted grid layout before anything is displayed.
        self._live_view.set_plot_columns(
            self._configuration_manager.settings.live_view_plot_columns
        )

        # Arm button (Setup AND live view) only visible if a trigger is
        # already configured/loaded at startup (see
        # `_on_open_trigger_settings_dialog` for refreshing after a
        # change).
        self._update_trigger_arm_available()

        # Bring DAQ-thread errors into the GUI thread thread-safely
        self._acquisition_error_signal.connect(self._on_acquisition_error_gui)
        self._controller.add_error_listener(self._acquisition_error_signal.emit)

        # Automatically scan for devices once on startup
        self._setup_view.discover_hardware_requested.emit()

        connect_language_changed(self.retranslate_ui)
        connect_theme_changed(self._retheme_nav_icons)

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    def _build_navigation_and_workspace(self) -> None:
        from PyQt6.QtWidgets import QButtonGroup, QHBoxLayout, QToolButton, QVBoxLayout

        central = QWidget()
        root_layout = QHBoxLayout(central)

        nav_container = QWidget()
        nav_container.setFixedWidth(180)
        nav_container.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        nav_layout = QVBoxLayout(nav_container)
        # Room for the tiles' drop shadows (see
        # `_update_nav_tile_elevation`) - with zero margins the shadow
        # is clipped at the column edges and the tiles look flat again.
        nav_layout.setContentsMargins(7, 7, 7, 7)
        nav_layout.setSpacing(8)
        # A real, readable bevel - but built from 1px edges and palette tones
        # instead of CSS's `outset`/`inset`, whose hard two-tone frame is what
        # made the tiles look cheap. Three cues act together, so the state is
        # unmistakable without a colored accent:
        #
        #   1. Edge direction. Raised: light on top/left, dark on bottom/right
        #      - lit from the upper left. Pressed: exactly reversed.
        #   2. Face brightness. A pressed face sits in its own shadow, so it is
        #      DARKER than a resting one (palette(mid) vs palette(button):
        #      200 vs 240 in the light theme, 40 vs 53 in the dark). Making it
        #      brighter instead read as 'lit up', not as 'pushed in'.
        #   3. Content offset. The pressed tile's icon and text sit 1px lower,
        #      the way a real key travels. Deliberate and symmetric (the
        #      bottom padding gives back what the top takes), so nothing
        #      drifts as tiles are switched.
        #
        # The gradient stops sit in the outermost few percent: on tiles a third
        # of the window tall, a full-height ramp reads as a glossy slab rather
        # than as an edge catching light.
        #
        # Only palette(...) references, no baked hex - that is what lets
        # `_retheme_nav_icons()` repolish them into the new theme.
        nav_container.setStyleSheet(
            "QToolButton {"
            "   border: 1px solid palette(mid);"
            "   border-top-color: palette(light);"
            "   border-left-color: palette(light);"
            "   border-bottom-color: palette(dark);"
            "   border-right-color: palette(dark);"
            "   border-radius: 10px;"
            "   background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            "                                stop:0 palette(light),"
            "                                stop:0.04 palette(button),"
            "                                stop:0.96 palette(button),"
            "                                stop:1 palette(mid));"
            "   padding: 8px;"
            "}"
            "QToolButton:hover:!checked {"
            "   background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            "                                stop:0 palette(light),"
            "                                stop:0.04 palette(midlight),"
            "                                stop:0.96 palette(midlight),"
            "                                stop:1 palette(mid));"
            "}"
            "QToolButton:checked {"
            "   border-top-color: palette(shadow);"
            "   border-left-color: palette(shadow);"
            "   border-bottom-color: palette(light);"
            "   border-right-color: palette(light);"
            "   background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            "                                stop:0 palette(shadow),"
            "                                stop:0.02 palette(dark),"
            "                                stop:0.06 palette(mid),"
            "                                stop:0.97 palette(mid),"
            "                                stop:1 palette(midlight));"
            "   padding: 9px 8px 7px 8px;"
            "}"
            # As soon as ANY ancestor carries a stylesheet, Qt renders
            # child QLabels through the CSS engine instead of purely from
            # the palette - without this rule the text stays black
            # regardless of theme. IMPORTANT: the QSS role name for
            # QPalette.WindowText is "foreground", NOT "window-text"
            # (otherwise it is silently ignored and the rule has no
            # effect).
            "QToolButton QLabel { color: palette(foreground); background: transparent; }"
        )

        # Icon+text are deliberately NOT set via
        # QToolButton.setIcon()/setText(): Qt's built-in label layout
        # (CE_ToolButtonLabel) doesn't keep a fixed gap between icon and
        # text for very tall buttons (full column height/3) - the icon
        # stays stuck at the top while the text ends up separately
        # vertically centered. Instead, build a self-contained,
        # tightly-grouped icon+text package and center that as a whole
        # within the button.
        self._nav_container = nav_container
        self._nav_button_group = QButtonGroup(self)
        self._nav_button_group.setExclusive(True)
        self._nav_buttons: list[QToolButton] = []
        self._nav_icon_labels: list[QLabel] = []
        self._nav_text_labels: list[QLabel] = []
        self._nav_shadows: list[QGraphicsDropShadowEffect] = []
        for index, key, icon in _NAV_ITEMS:
            button = QToolButton()
            button.setCheckable(True)
            # QToolButton has a "Fixed"/"Preferred" size policy vertically
            # by default - without "Expanding" the stretch=1 below would
            # have no effect and the buttons would stay at their minimum
            # height instead of filling the column.
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

            icon_label = QLabel()
            icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

            text_label = QLabel(t(key))
            text_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            text_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            text_font = text_label.font()
            text_font.setPointSize(11)
            text_font.setBold(True)
            text_label.setFont(text_font)

            content_layout = QVBoxLayout()
            content_layout.setContentsMargins(0, 0, 0, 0)
            content_layout.setSpacing(4)
            content_layout.addWidget(icon_label)
            content_layout.addWidget(text_label)

            button_layout = QVBoxLayout(button)
            button_layout.addStretch(1)
            button_layout.addLayout(content_layout)
            button_layout.addStretch(1)

            self._nav_button_group.addButton(button, index)
            self._nav_buttons.append(button)
            self._nav_icon_labels.append(icon_label)
            self._nav_text_labels.append(text_label)
            # One effect instance per widget - a QGraphicsEffect cannot
            # be shared between widgets.
            shadow = QGraphicsDropShadowEffect(button)
            button.setGraphicsEffect(shadow)
            self._nav_shadows.append(shadow)
            # stretch=1 on all three buttons -> they evenly share the full
            # height of the navigation column.
            nav_layout.addWidget(button, stretch=1)

        self._update_nav_tile_elevation()
        self._retheme_nav_icons()
        self._nav_button_group.idClicked.connect(self._on_nav_changed)
        root_layout.addWidget(nav_container)

        self._workspace = QStackedWidget()
        self._workspace.addWidget(self._setup_view)      # index 0
        self._workspace.addWidget(self._live_view)       # index 1
        self._workspace.addWidget(self._analysis_view)   # index 2
        # "Data management" currently shares the analysis view (loading
        # saved measurements); a dedicated management view will follow
        # later.
        root_layout.addWidget(self._workspace, stretch=1)

        self.setCentralWidget(central)
        self._set_nav_index(_VIEW_SETUP)

    def _set_nav_index(self, index: int) -> None:
        """Programmatically selects a navigation tile.

        `QButtonGroup.setChecked()` - unlike `QListWidget.setCurrentRow()`
        previously - does not fire a click signal, so the workspace page
        is set explicitly here as well.
        """
        self._nav_buttons[index].setChecked(True)
        self._workspace.setCurrentIndex(min(index, _VIEW_ANALYSIS))
        self._update_nav_tile_elevation()

    def _update_nav_tile_elevation(self) -> None:
        """Raises the resting tiles and sinks the checked one.

        Qt stylesheets have no `box-shadow`, so a border alone can only
        hint at depth. A real drop shadow underneath is what makes a tile
        look like it stands off the surface - and removing it on the
        checked tile is what makes that one look pressed into it.

        Called on every switch and after a theme change: the shadow color
        differs per theme (see `gui/theme.py::nav_shadow_color`).
        """
        color = nav_shadow_color()
        for button, shadow in zip(self._nav_buttons, self._nav_shadows):
            shadow.setColor(color)
            if button.isChecked():
                # Pressed in: no cast shadow at all, otherwise the tile
                # still floats however dark the face is.
                shadow.setEnabled(False)
            else:
                shadow.setEnabled(True)
                shadow.setBlurRadius(20)
                shadow.setXOffset(0)
                # Straight down - a light source overhead, matching the
                # light edge the stylesheet puts on the top of each tile.
                shadow.setYOffset(5)

    def _retheme_nav_icons(self) -> None:
        """Redraws the navigation icons and forces a re-polish of the
        tile stylesheets after a theme change.

        The icons are redrawn with the current `WindowText` color (see
        `gui/theme.py::draw_gear_icon` etc.), since otherwise they would
        stay stuck in the color of the theme the button was originally
        created with. The manual unpolish()/polish() additionally ensures
        that the `palette(...)` references in the tile stylesheet
        (border/gradient) are also redrawn immediately.
        """
        for (_, _, draw_icon), label in zip(_NAV_ITEMS, self._nav_icon_labels):
            label.setPixmap(draw_icon(36))
        # Repolish not just the container but EVERY individual button AND
        # every child label - a QSS cache at the child-widget level is not
        # reliably invalidated by repolishing only the parent.
        all_widgets = [
            self._nav_container,
            *self._nav_buttons,
            *self._nav_icon_labels,
            *self._nav_text_labels,
        ]
        for widget in all_widgets:
            repolish(widget)
        self._update_nav_tile_elevation()

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()

        self._file_menu = menu_bar.addMenu(f"&{t('menu_file')}")
        self._save_config_action = self._file_menu.addAction(f"{t('menu_save_config')}...")
        self._save_config_action.triggered.connect(self._on_save_config)
        self._load_config_action = self._file_menu.addAction(f"{t('menu_load_config')}...")
        self._load_config_action.triggered.connect(self._on_load_config)
        self._file_menu.addSeparator()
        self._load_measurement_action = self._file_menu.addAction(f"{t('load_measurement')}...")
        self._load_measurement_action.triggered.connect(self._on_load_measurement)
        self._file_menu.addSeparator()
        self._quit_action = self._file_menu.addAction(t("menu_quit"))
        self._quit_action.triggered.connect(self.close)

        self._settings_menu = menu_bar.addMenu(f"&{t('menu_settings')}")
        self._language_menu = self._settings_menu.addMenu(t("language"))
        self._language_action_group = QActionGroup(self)
        self._language_action_group.setExclusive(True)

        self._language_de_action = self._language_menu.addAction(t("german"))
        self._language_de_action.setCheckable(True)
        self._language_de_action.setData("de")
        self._language_action_group.addAction(self._language_de_action)

        self._language_en_action = self._language_menu.addAction(t("english"))
        self._language_en_action.setCheckable(True)
        self._language_en_action.setData("en")
        self._language_action_group.addAction(self._language_en_action)

        current_language_action = (
            self._language_de_action if get_language() == "de" else self._language_en_action
        )
        current_language_action.setChecked(True)
        self._language_action_group.triggered.connect(self._on_language_action_triggered)

        self._theme_menu = self._settings_menu.addMenu(t("menu_theme"))
        self._theme_action_group = QActionGroup(self)
        self._theme_action_group.setExclusive(True)

        self._theme_light_action = self._theme_menu.addAction(t("theme_light"))
        self._theme_light_action.setCheckable(True)
        self._theme_light_action.setData("light")
        self._theme_action_group.addAction(self._theme_light_action)

        self._theme_dark_action = self._theme_menu.addAction(t("theme_dark"))
        self._theme_dark_action.setCheckable(True)
        self._theme_dark_action.setData("dark")
        self._theme_action_group.addAction(self._theme_dark_action)

        current_theme_action = (
            self._theme_light_action if get_theme() == "light" else self._theme_dark_action
        )
        current_theme_action.setChecked(True)
        self._theme_action_group.triggered.connect(self._on_theme_action_triggered)

        self._settings_menu.addSeparator()
        self._channel_display_action = self._settings_menu.addAction(
            f"{t('menu_channel_display')}..."
        )
        self._channel_display_action.triggered.connect(self._on_open_channel_display_dialog)
        self._sensor_database_action = self._settings_menu.addAction(t("menu_sensor_database"))
        self._sensor_database_action.triggered.connect(self._on_open_sensor_database)
        self._trigger_settings_action = self._settings_menu.addAction(t("menu_trigger_settings"))
        self._trigger_settings_action.triggered.connect(self._on_open_trigger_settings_dialog)

        self._help_menu = menu_bar.addMenu(f"&{t('menu_help')}")
        self._about_action = self._help_menu.addAction(t("menu_about"))
        self._about_action.triggered.connect(self._on_about)

    def _build_shortcuts(self) -> None:
        """Global keyboard shortcuts for measurement start/stop - work
        regardless of which tab (configuration/live view/analysis) is
        currently active, since they are registered via
        `self.addAction()` on the main window itself instead of on a
        single button/view.

        Call the same handlers as the start/stop buttons (see
        `_on_start_measurement_from_live`/`_on_stop_measurement`) - both
        are already safeguarded against the case that no measurement is
        running (stop) or one is already running (start then reports the
        existing `MeasurementController` error, just like a double click
        on the start button would too).
        """
        self._start_shortcut_action = QAction(self)
        self._start_shortcut_action.setShortcut(QKeySequence("F5"))
        # F5 starts WITH storage (live_only=False), like the record
        # button - there is no dedicated shortcut for live-only display.
        self._start_shortcut_action.triggered.connect(
            lambda: self._on_start_measurement_from_live(False)
        )
        self.addAction(self._start_shortcut_action)

        self._stop_shortcut_action = QAction(self)
        self._stop_shortcut_action.setShortcut(QKeySequence("F8"))
        self._stop_shortcut_action.triggered.connect(self._on_stop_measurement)
        self.addAction(self._stop_shortcut_action)

    def _on_language_action_triggered(self, action) -> None:
        """Triggered when the user clicks a language in the Settings ->
        Language menu - takes effect immediately in the running app and
        is persisted (see `gui/i18n.py::set_language`)."""
        new_language = action.data()
        set_language(new_language)
        self._configuration_manager.update_language(new_language)

    def _on_theme_action_triggered(self, action) -> None:
        """Triggered when the user clicks Light/Dark in the Settings ->
        Theme menu - takes effect immediately in the running app and is
        persisted (see `gui/theme.py::set_theme`)."""
        new_theme = action.data()
        set_theme(new_theme)
        self._configuration_manager.update_theme(new_theme)

    def _on_open_channel_display_dialog(self) -> None:
        """Opens the channel display dialog with the channels configured
        in Setup (see `SetupView.get_configured_channels()`).

        Deliberately NOT `self._live_view.open_channel_display_dialog()`
        without an argument: the live view only knows its channels once a
        measurement is actually running (`start_display()`) - but the
        display should already be configurable beforehand, right after
        configuring in Setup - EVEN for channels that don't have an
        assigned hardware channel yet (see
        `gui/live_view.py::_channel_display_key` for the key handling
        this requires).
        """
        channels = [ch for ch in self._setup_view.get_configured_channels() if ch.enabled]
        settings = self._live_view.open_channel_display_dialog(channels)
        if settings is not None:
            # Write back into Setup so the values are preserved when the
            # configuration is saved (see
            # `SetupView.apply_channel_display_settings`) - the live view
            # only knows the copy passed to it here, not the channel table.
            self._setup_view.apply_channel_display_settings(settings)

    def _on_open_sensor_database(self) -> None:
        """Opens the sensor database management (see
        gui/sensor_database_dialog.py). Changes are persisted immediately
        there - this dialog therefore only passes through the
        `SensorDatabaseManager` instance (already created by
        `main.py`)."""
        from gui.sensor_database_dialog import SensorDatabaseDialog

        dialog = SensorDatabaseDialog(self._sensor_database, self)
        dialog.exec()

    def _on_open_trigger_settings_dialog(self) -> None:
        """Opens the trigger settings dialog (see
        `gui/trigger_settings_dialog.py::TriggerSettingsDialog`) with the
        ACTIVE channels configured in Setup as the selection for a
        threshold trigger - usable before measurement start already, like
        the channel display dialog (see
        `_on_open_channel_display_dialog`)."""
        channels = [ch for ch in self._setup_view.get_configured_channels() if ch.enabled]
        dialog = TriggerSettingsDialog(self._trigger_config, channels, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._trigger_config = dialog.results()
            self._configuration_manager.update_last_trigger_settings(self._trigger_config)
            self._update_trigger_arm_available()

    def _update_trigger_arm_available(self) -> None:
        """Shows/hides the arm button in BOTH views (Setup and Live) in
        sync - see `SetupView.set_trigger_arm_available`/
        `LiveView.set_trigger_arm_available`."""
        available = (
            self._trigger_config.start.kind != TriggerKind.NONE
            or self._trigger_config.stop.kind != TriggerKind.NONE
        )
        self._setup_view.set_trigger_arm_available(available)
        self._live_view.set_trigger_arm_available(available)

    def _build_status_bar(self) -> None:
        # Red dot to the LEFT of the text, shown only while data is
        # actually being written. "Am I recording?" is the one question a
        # measurement application has to answer at a glance, and the
        # status text alone said the same thing for a recording and for a
        # live-only run (see `_set_measurement_status`).
        self._recording_indicator = QLabel()
        self._recording_indicator.setPixmap(
            draw_record_icon(12, color=RECORD_ICON_COLOR)
        )
        self._recording_indicator.setVisible(False)
        self.statusBar().addWidget(self._recording_indicator)
        self._status_label = QLabel(t("ready"))
        self.statusBar().addWidget(self._status_label)

    def _set_measurement_status(self, config: MeasurementConfig) -> None:
        """Shows that a measurement is running, and WHETHER it is saved.

        Both cases used to share one message, so a live-only run looked
        exactly like a recording - leaving room to believe you are
        recording when you are not, or the other way round."""
        recording = config.save_to_disk
        self._recording_indicator.setVisible(recording)
        self._status_label.setText(
            t(
                "measurement_recording_named"
                if recording
                else "measurement_live_only_named",
                name=config.name,
            )
        )

    def _clear_measurement_status(self) -> None:
        """Back to idle - the dot must not outlive the measurement."""
        self._recording_indicator.setVisible(False)
        self._status_label.setText(t("ready"))

    def retranslate_ui(self) -> None:
        """Updates all static texts of the main window after a language
        change (see `gui/i18n.py::connect_language_changed`)."""
        self.setWindowTitle(t("window_title"))

        for index, key, _icon in _NAV_ITEMS:
            self._nav_text_labels[index].setText(t(key))

        self._file_menu.setTitle(f"&{t('menu_file')}")
        self._save_config_action.setText(f"{t('menu_save_config')}...")
        self._load_config_action.setText(f"{t('menu_load_config')}...")
        self._load_measurement_action.setText(f"{t('load_measurement')}...")
        self._quit_action.setText(t("menu_quit"))

        self._settings_menu.setTitle(f"&{t('menu_settings')}")
        self._language_menu.setTitle(t("language"))
        self._language_de_action.setText(t("german"))
        self._language_en_action.setText(t("english"))
        self._theme_menu.setTitle(t("menu_theme"))
        self._theme_light_action.setText(t("theme_light"))
        self._theme_dark_action.setText(t("theme_dark"))
        self._channel_display_action.setText(f"{t('menu_channel_display')}...")
        self._sensor_database_action.setText(t("menu_sensor_database"))
        self._trigger_settings_action.setText(t("menu_trigger_settings"))

        self._help_menu.setTitle(f"&{t('menu_help')}")
        self._about_action.setText(t("menu_about"))

        # The status text is usually a one-off event (start/stop/error)
        # and corrects itself on the next event. Only the idle state
        # would otherwise remain stuck in the old language permanently.
        if not self._controller.is_running:
            self._clear_measurement_status()

    # ------------------------------------------------------------------ #
    # Saved measurement configurations (File menu)
    # ------------------------------------------------------------------ #

    def _on_save_config(self) -> None:
        """Saves the configuration currently set in the setup view.

        The save location is chosen by the user via a file dialog (no
        internal name catalog) - configuration files are therefore
        regular files that can be freely stored/renamed/shared.
        """
        # Apply the current popout window position BEFORE reading out the
        # channels - see `_sync_popout_geometry_to_setup`.
        self._sync_popout_geometry_to_setup()
        config = self._setup_view.build_current_config()
        if config is None:
            return
        # As with `_on_start_measurement`: `SetupView.build_current_config()`
        # no longer knows the trigger configuration (see
        # `gui/trigger_settings_dialog.py`) - without this line, "Save
        # configuration" would silently lose the active trigger (default
        # `TriggerConfig()` instead of the real values).
        config.trigger = self._trigger_config
        filename, _ = QFileDialog.getSaveFileName(
            self,
            t("menu_save_config"),
            f"{config.name}.json",
            t("file_filter_json"),
        )
        if not filename:
            return
        file_path = Path(filename)
        if file_path.suffix.lower() != ".json":
            file_path = file_path.with_suffix(".json")
        try:
            self._configuration_manager.save_measurement_config(config, file_path)
        except OSError:
            QMessageBox.warning(
                self, t("error"), t("error_config_save_failed", path=file_path)
            )
            return
        self._status_label.setText(t("status_config_saved", filename=file_path.name))

    def _on_load_config(self) -> None:
        """Lets the user select a configuration file and loads it."""
        filename, _ = QFileDialog.getOpenFileName(
            self,
            t("menu_load_config"),
            "",
            t("file_filter_json"),
        )
        if not filename:
            return
        file_path = Path(filename)
        config = self._configuration_manager.load_measurement_config(file_path)
        if config is None:
            QMessageBox.warning(
                self, t("error"), t("error_config_load_failed", path=file_path)
            )
            return
        self._setup_view.apply_config(config)
        # The loaded configuration brings its own trigger configuration
        # along (see `_on_save_config`) - MainWindow adopts it here as the
        # new active configuration (see `_trigger_config`).
        self._trigger_config = config.trigger
        self._status_label.setText(t("status_config_loaded", filename=file_path.name))

    def _on_load_measurement(self) -> None:
        """Lets the user select a completed measurement and, on selection,
        jumps directly into the analysis tab (see
        `AnalysisView.prompt_and_load_file` - formerly a button directly
        in the analysis view, now reachable from any view).
        """
        if self._analysis_view.prompt_and_load_file():
            self._set_nav_index(_VIEW_ANALYSIS)

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #

    def _on_nav_changed(self, row: int) -> None:
        self._workspace.setCurrentIndex(min(row, _VIEW_ANALYSIS))
        self._update_nav_tile_elevation()
        # Pre-populate the live view with the channels currently
        # configured in Setup as soon as we switch there (the plot
        # windows are then already in place instead of only appearing
        # after measurement start) - only without a running measurement,
        # see `LiveView.preview_channels`. This explicitly also works for
        # channels without an assigned hardware channel (no hardware
        # connected yet).
        if row == _VIEW_LIVE and not self._controller.is_running:
            channels = [ch for ch in self._setup_view.get_configured_channels() if ch.enabled]
            self._live_view.preview_channels(channels)
        # Refresh the device list whenever the user comes back to Setup:
        # the list is a snapshot of the moment it was taken, and a cable
        # pulled in the meantime would otherwise keep showing as
        # available until the user thinks to press "search devices"
        # (see `hardware/nidaq_device.py::_is_device_connected`).
        #
        # NOT while a measurement is running: discovery now probes the
        # hardware via `self_test_device()`, which must not be fired at a
        # device that currently has a running task. Re-entry during an
        # already pending discovery is handled by `_on_discover_hardware`
        # itself.
        if row == _VIEW_SETUP and not self._controller.is_running:
            self._setup_view.discover_hardware_requested.emit()

    # ------------------------------------------------------------------ #
    # Storage location
    # ------------------------------------------------------------------ #

    def _on_choose_storage_path(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, t("choose_storage_location"))
        if not directory:
            return
        self._storage_path = Path(directory)
        self._configuration_manager.update_last_storage_path(directory)
        self._update_storage_status()
        self._setup_view.set_storage_path(str(self._storage_path))

    def _update_storage_status(self) -> None:
        # Storage location display in the status bar removed; now only shown in the setup view
        pass

    # ------------------------------------------------------------------ #
    # Hardware / measurement
    # ------------------------------------------------------------------ #

    def _on_discover_hardware(self) -> None:
        """Starts device discovery in the background (see
        `gui/workers.py::BackgroundWorker`).

        `nidaqmx.system.System.local()` plus channel iteration per device
        (`hardware/nidaq_device.py::discover_devices`) can take a
        noticeable amount of time with multiple chassis/modules or driver
        timeouts - it previously ran synchronously on the GUI thread and
        in doing so also blocked the automatic discovery run at program
        startup (see `__init__`).
        """
        if self._discovery_worker is not None:  # a request is already in progress
            return
        self._setup_view.set_discovery_in_progress(True)
        worker = BackgroundWorker(self._controller.discover_hardware)
        self._discovery_worker = worker
        worker.succeeded.connect(self._on_discover_hardware_succeeded)
        worker.failed.connect(self._on_discover_hardware_failed)
        worker.finished.connect(lambda: self._forget_background_worker(worker))
        self._background_workers.append(worker)
        worker.start()

    def _on_discover_hardware_succeeded(self, devices: list[DeviceInfo]) -> None:
        self._discovery_worker = None
        self._setup_view.set_discovery_in_progress(False)
        self._setup_view.set_discovered_devices(devices)
        # Only count devices WITH analog input channels -
        # `System.local().devices` otherwise also includes pure chassis
        # entries without their own channels (e.g. "cDAQ9185-0217ED5E" in
        # addition to its modules "...Mod1"/"...Mod2"), which artificially
        # inflates the count relative to the hardware actually usable.
        # Deliberately NARROWER than the filter in
        # `SetupView.set_discovered_devices` (which additionally allows
        # `has_any_channels`, to also show/report unsupported non-AI
        # modules like the NI9263) - here, only what this app can
        # actually configure as a channel is counted.
        usable_devices = [d for d in devices if d.num_channels > 0]
        self._status_label.setText(f"{len(usable_devices)} {t('devices_found')}")

    def _on_discover_hardware_failed(self, message: str) -> None:
        self._discovery_worker = None
        self._setup_view.set_discovery_in_progress(False)
        logger.error("Geräteerkennung fehlgeschlagen: %s", message)
        self._status_label.setText(t("device_discovery_failed"))
        # Cause (e.g. "NI-DAQmx driver not installed") visible in the
        # device browser itself, not just in the status bar/log - that's
        # where the user looks next.
        self._setup_view.show_discovery_error(message)

    def _on_open_ni_max(self) -> None:
        """Opens NI-MAX (Measurement & Automation Explorer) as a separate
        program (see `hardware/nidaq_device.py::open_ni_max`)."""
        try:
            self._controller.open_ni_max()
        except Exception as exc:
            QMessageBox.warning(self, t("error"), f"{t('ni_max_open_failed')}:\n{exc}")

    def _forget_background_worker(self, worker: BackgroundWorker) -> None:
        """Removes a finished `BackgroundWorker` reference so that
        `_background_workers` does not grow unbounded over a long program
        runtime."""
        if worker in self._background_workers:
            self._background_workers.remove(worker)
        worker.deleteLater()

    def _on_start_measurement(self, config: MeasurementConfig) -> None:
        requested_measurement_name = config.name
        # Since being generalized to start AND stop, the trigger
        # configuration belongs to `MainWindow` itself (see
        # `TriggerSettingsDialog`) - `SetupView.build_current_config()`
        # now only returns an empty default `TriggerConfig()`, here the
        # actually active configuration is fed in.
        config.trigger = self._trigger_config

        if config.save_to_disk:
            if self._storage_path is None:
                QMessageBox.warning(
                    self,
                    t("error_no_storage_title"),
                    t("error_no_storage_body"),
                )
                return

            resolved_name = self._resolve_measurement_name(
                base_name=config.name,
                storage_format=config.storage_format,
                naming=self._setup_view.get_naming_scheme(),
            )
            if resolved_name is None:
                return
            if resolved_name != config.name:
                logger.info(
                    "Messname '%s' aufgelöst zu '%s'.",
                    config.name,
                    resolved_name,
                )
                config.name = resolved_name

        try:
            # Reuse the most recently discovered device list instead of
            # rediscovering on every measurement start (see
            # `SetupView.get_discovered_devices`) - saves the noticeably
            # slow rediscovery with multiple chassis/modules.
            session = self._controller.start_measurement(
                config, discovered_devices=self._setup_view.get_discovered_devices()
            )
        except Exception as exc:  # MeasurementConfigError, AcquisitionError, RuntimeError
            logger.exception("Messung konnte nicht gestartet werden")
            self._setup_view.show_error(f"{t('cannot_start_measurement')}:\n{exc}")
            return

        logger.info(
            "_on_start_measurement: %d aktive Kanaele vom Controller nach Start: %s",
            len(self._controller.active_channels),
            [c.hardware_channel for c in self._controller.active_channels],
        )

        # Actual tick rate of the ring buffer (= fastest rate group, see
        # data/models.py::resolve_rate_groups) instead of the raw target
        # rate - only differs if e.g. an NI9210 forced its own group.
        # StorageWriter/LiveView must know the REAL tick rate, otherwise
        # the stored time_s column would be scaled incorrectly.
        rate_groups = resolve_rate_groups(config.active_channels(), config.sample_rate_hz)
        effective_tick_rate_hz = max(
            (g.resolved_sample_rate_hz for g in rate_groups), default=config.sample_rate_hz
        )

        self._configuration_manager.update_last_measurement_parameters(
            measurement_name=requested_measurement_name,
            sample_rate_hz=config.sample_rate_hz,
            storage_format=config.storage_format.value,
            recording_unlimited=config.recording_unlimited,
            recording_stop_value=config.recording_stop_value,
            recording_stop_unit=config.recording_stop_unit.value,
        )
        self._configuration_manager.update_last_trigger_settings(config.trigger)

        self._setup_view.set_start_enabled(False, "measurement_running")
        self._live_view.set_start_enabled(False)

        if config.trigger.start.kind == TriggerKind.NONE:
            # Manual start: StorageWriter (if desired) is created and
            # started IMMEDIATELY (previous behavior unchanged).
            if config.save_to_disk:
                ring_buffer = self._controller.get_ring_buffer()
                extension = ".parquet" if config.storage_format == StorageFormat.PARQUET else ".csv"
                data_path = self._storage_path / f"{config.name}{extension}"
                self._storage_writer = StorageWriter(
                    ring_buffer=ring_buffer,
                    channels=self._controller.active_channels,
                    output_path=data_path,
                    storage_format=config.storage_format,
                    sample_rate_hz=effective_tick_rate_hz,
                )
                self._storage_writer.start()
            else:
                self._storage_writer = None
            self._recording_started = True
            self._live_view.start_display(
                self._controller.active_channels,
                effective_tick_rate_hz,
                storage_writer=self._storage_writer,
                trigger_config=config.trigger,
                rate_groups=rate_groups,
            )
            # Zero point for a possibly configured recording limit AND
            # reset of the stop-trigger edge detector (see
            # `gui/live_view.py::mark_recording_started`) - MUST be called
            # here too, not only for a start trigger.
            self._live_view.mark_recording_started(0)
            self._maybe_start_stop_listener(config)
            self._set_nav_index(_VIEW_LIVE)
            self._set_measurement_status(config)
            return

        # Threshold/serial START trigger: hardware acquisition + display
        # start immediately (pre-roll buffering, see
        # `data/models.py::TriggerConfig`), the StorageWriter is only
        # created in `_on_trigger_fired`.
        self._recording_started = False
        self._storage_writer = None
        self._live_view.start_display(
            self._controller.active_channels,
            effective_tick_rate_hz,
            storage_writer=None,
            trigger_config=config.trigger,
            rate_groups=rate_groups,
        )
        self._live_view.enter_armed_state()

        if config.trigger.start.kind == TriggerKind.SERIAL:
            listener = SerialTriggerListener(
                config.trigger.start.serial_port,
                config.trigger.start.serial_baud_rate,
                config.trigger.start.serial_expected_message.encode("utf-8"),
            )
            listener.message_matched.connect(self._on_trigger_fired)
            listener.connection_failed.connect(self._on_trigger_connection_failed)
            self._start_serial_listener = listener
            listener.start()

        self._set_nav_index(_VIEW_LIVE)
        self._status_label.setText(t("measurement_armed_status", name=config.name))

    def _maybe_start_stop_listener(self, config: MeasurementConfig) -> None:
        """Starts the serial STOP-trigger listener, if configured (see
        `TriggerConfig.stop`) - called right AFTER recording has actually
        started (`mark_recording_started`), regardless of whether the
        start was manual or triggered (see the manual branch above and
        `_on_trigger_fired`). A threshold stop trigger doesn't need its
        own listener - it is already monitored independently via
        `gui/live_view.py::_check_stop_threshold_trigger`."""
        if config.trigger.stop.kind != TriggerKind.SERIAL:
            return
        listener = SerialTriggerListener(
            config.trigger.stop.serial_port,
            config.trigger.stop.serial_baud_rate,
            config.trigger.stop.serial_expected_message.encode("utf-8"),
        )
        listener.message_matched.connect(self._on_stop_measurement)
        listener.connection_failed.connect(self._on_stop_trigger_connection_failed)
        self._stop_serial_listener = listener
        listener.start()

    def _on_trigger_fired(self) -> None:
        """An armed START trigger has fired (see
        `gui/live_view.py::LiveView.trigger_fired` and
        `gui/serial_trigger.py::SerialTriggerListener.message_matched`) -
        only now (not before!) creates the StorageWriter, possibly with a
        retroactive pre-roll (threshold trigger, see
        `core/ringbuffer.py::RingBuffer.register_reader`)."""
        session = self._controller.current_session
        if session is None or self._recording_started:
            return
        config = session.config
        # Actual tick rate of the ring buffer (see comment in
        # `_on_start_measurement`) - the pre-roll sample offset AND the
        # stored time_s column must be based on this, not on the raw
        # target rate.
        rate_groups = resolve_rate_groups(config.active_channels(), config.sample_rate_hz)
        effective_tick_rate_hz = max(
            (g.resolved_sample_rate_hz for g in rate_groups), default=config.sample_rate_hz
        )

        back_samples = 0
        if config.trigger.start.kind == TriggerKind.THRESHOLD:
            back_samples = round(config.trigger.pretrigger_seconds * effective_tick_rate_hz)

        total_now = self._controller.total_samples_acquired
        ring_buffer = self._controller.get_ring_buffer()
        capacity = ring_buffer.capacity if ring_buffer is not None else 0
        # Same clamp formula as `RingBuffer.register_reader` - the actual
        # zero point for the recording limit (see
        # `gui/live_view.py::mark_recording_started`) must exactly match
        # the reader later registered by the StorageWriter.
        oldest_valid = max(0, total_now - capacity)
        baseline_samples = max(oldest_valid, total_now - back_samples)

        if config.save_to_disk and self._storage_path is not None:
            ring_buffer_for_writer = self._controller.get_ring_buffer()
            extension = ".parquet" if config.storage_format == StorageFormat.PARQUET else ".csv"
            data_path = self._storage_path / f"{config.name}{extension}"
            self._storage_writer = StorageWriter(
                ring_buffer=ring_buffer_for_writer,
                channels=self._controller.active_channels,
                output_path=data_path,
                storage_format=config.storage_format,
                sample_rate_hz=effective_tick_rate_hz,
                reader_back_samples=back_samples,
            )
            self._storage_writer.start()
        else:
            self._storage_writer = None

        self._live_view.exit_armed_state()
        self._live_view.attach_storage_writer(self._storage_writer)
        self._live_view.mark_recording_started(baseline_samples)
        self._recording_started = True

        # IMPORTANT: `.stop()` BEFORE `.deleteLater()` - guarantees that
        # the COM port is actually closed BEFORE a possibly configured
        # stop trigger (see `_maybe_start_stop_listener`) reopens the same
        # port (otherwise a possible resource conflict if the start and
        # stop trigger use the same port).
        if self._start_serial_listener is not None:
            self._start_serial_listener.stop()
            self._start_serial_listener.deleteLater()
            self._start_serial_listener = None

        self._maybe_start_stop_listener(config)

        self._set_measurement_status(config)

    def _on_trigger_connection_failed(self, message: str) -> None:
        """The serial START trigger could not open the configured COM
        port (see `gui/serial_trigger.py::SerialTriggerListener`) - clean
        disarming instead of a helplessly waiting measurement. Unlike the
        stop trigger (see `_on_stop_trigger_connection_failed`), there is
        NO recording running yet at this point - a full abort is
        therefore unproblematic."""
        logger.error("Serieller Start-Trigger fehlgeschlagen: %s", message)
        self._live_view.exit_armed_state()
        self._live_view.stop_display()
        self._controller.stop_measurement()
        if self._start_serial_listener is not None:
            self._start_serial_listener.stop()
            self._start_serial_listener.deleteLater()
            self._start_serial_listener = None
        self._recording_started = False
        self._setup_view.set_start_enabled(True, "")
        self._live_view.set_start_enabled(True)
        # The arm button must not stay pressed - nothing is retried
        # automatically anymore (a broken port would otherwise stay
        # broken).
        self._setup_view.set_trigger_armed(False)
        self._live_view.set_trigger_armed(False)
        self._trigger_config.auto_rearm = False
        self._clear_measurement_status()
        QMessageBox.warning(self, t("trigger_connection_failed_title"), message)
        self._set_nav_index(_VIEW_SETUP)

    def _on_stop_trigger_connection_failed(self, message: str) -> None:
        """The serial STOP trigger could not open the configured COM port
        - unlike the start trigger (`_on_trigger_connection_failed`), this
        must NOT abort the running recording: real measurement data may
        already be being written at this point. Only the stop listener is
        cleaned up, the measurement continues unchanged (manual
        stop/recording limit keep working independently of this)."""
        logger.error("Serieller Stopp-Trigger fehlgeschlagen: %s", message)
        if self._stop_serial_listener is not None:
            self._stop_serial_listener.stop()
            self._stop_serial_listener.deleteLater()
            self._stop_serial_listener = None
        QMessageBox.warning(
            self,
            t("trigger_stop_connection_failed_title"),
            t("error_stop_trigger_connection_failed", message=message),
        )

    def _on_start_measurement_from_live(self, live_only: bool) -> None:
        config = self._setup_view.build_current_config(live_only=live_only)
        if config is None:
            return
        self._on_start_measurement(config)

    def _on_trigger_arm_toggled(self, checked: bool) -> None:
        """Reacts to the arm button (the setup AND live view each own
        their own but equivalent instance - see
        `gui/setup_view.py`/`gui/live_view.py::_trigger_arm_button`).

        Arming (checked=True) IMMEDIATELY starts the first cycle AND sets
        `TriggerConfig.auto_rearm`, so that `_on_stop_measurement`
        automatically restarts after EVERY stop (manual, via trigger, or
        recording limit) - both buttons stay pressed the whole time,
        regardless of how many times the cycle runs through automatically
        in between. Disarming (checked=False) ends the automatic re-arm
        AND a currently running/armed measurement immediately - this is
        the only way to actually end the cycle (a single manual stop is
        deliberately NOT enough for that, see `_on_stop_measurement`).
        """
        self._trigger_config.auto_rearm = checked
        # Keep both buttons in sync, regardless of which one triggered the
        # click - `set_trigger_armed()` blocks `toggled` here, so no
        # feedback loop is created (see the docs there).
        self._setup_view.set_trigger_armed(checked)
        self._live_view.set_trigger_armed(checked)

        if checked:
            config = self._setup_view.build_current_config()
            if config is None:
                # Invalid configuration (e.g. no active channel) - the
                # button must not stay pressed since nothing happens
                # anyway.
                self._setup_view.set_trigger_armed(False)
                self._live_view.set_trigger_armed(False)
                self._trigger_config.auto_rearm = False
                return
            self._on_start_measurement(config)
            if not self._controller.is_running:
                # Start failed synchronously (see error handling in
                # `_on_start_measurement`) - don't leave the button
                # pressed.
                self._setup_view.set_trigger_armed(False)
                self._live_view.set_trigger_armed(False)
                self._trigger_config.auto_rearm = False
        elif self._controller.is_running:
            self._on_stop_measurement()

    def _resolve_measurement_name(
        self,
        base_name: str,
        storage_format: StorageFormat,
        naming: NamingScheme,
    ) -> str | None:
        """Builds the file/measurement name actually to be used from the
        entered measurement name, according to `naming`.

        Order of the optional components: Name_Date_Time_Number. If no
        number suffix is active and the resolved name already exists, the
        measurement is aborted with an error message - automatic
        overwriting of existing measurement data deliberately does not
        happen.

        Returns:
            The resolved name, or None if the measurement should be
            aborted due to a name conflict without automatic resolution.
        """
        if self._storage_path is None:
            return base_name

        now = datetime.now()
        parts = [base_name]
        if naming.include_date:
            parts.append(now.strftime("%Y%m%d"))
        if naming.include_time:
            parts.append(now.strftime("%H%M%S"))

        extension = ".parquet" if storage_format == StorageFormat.PARQUET else ".csv"

        def data_path(name: str) -> Path:
            return self._storage_path / f"{name}{extension}"

        def metadata_path(name: str) -> Path:
            return self._storage_path / f"{name}_info.json"

        def name_conflicts(name: str) -> bool:
            return data_path(name).exists() or metadata_path(name).exists()

        if naming.use_number_suffix:
            digits = max(1, naming.number_suffix_digits)
            for index in range(1, 10**digits):
                candidate = "_".join(parts + [f"{index:0{digits}d}"])
                if not name_conflicts(candidate):
                    return candidate
            raise RuntimeError(
                "Konnte keinen eindeutigen Messnamen finden "
                f"(Basisname '{base_name}')."
            )

        candidate = "_".join(parts)
        if not name_conflicts(candidate):
            return candidate

        self._setup_view.show_error(t("error_name_conflict", name=candidate))
        return None

    def _on_stop_measurement(self) -> None:
        # Still-running serial trigger listeners (start AND stop) must be
        # stopped BEFORE the actual stop (e.g. abort during the armed
        # phase, or when the stop trigger itself triggered this call) -
        # otherwise background threads would be left orphaned.
        if self._start_serial_listener is not None:
            self._start_serial_listener.stop()
            self._start_serial_listener.deleteLater()
            self._start_serial_listener = None
        if self._stop_serial_listener is not None:
            self._stop_serial_listener.stop()
            self._stop_serial_listener.deleteLater()
            self._stop_serial_listener = None

        # IMPORTANT: read active_device_infos BEFORE stop_measurement() -
        # the controller clears its internal device list when stopping,
        # if read afterwards the list would always be empty and the
        # metadata file would never contain real hardware information.
        device_infos = self._controller.active_device_infos
        session = self._controller.stop_measurement()
        self._live_view.stop_display()

        if self._storage_writer is not None:
            self._storage_writer.stop()
            self._storage_writer = None

        # Only write metadata if the measurement was actually stored AND
        # actually recorded - on an abort during the armed phase (trigger
        # never fired, see `_recording_started`) no StorageWriter/data
        # file exists at all, a metadata file for it would be misleading.
        if (
            session is not None
            and self._storage_path is not None
            and session.config.save_to_disk
            and self._recording_started
        ):
            self._finalize_measurement(session, device_infos)

        # ALWAYS update status, not only when actually stored (see
        # `_finalize_measurement`) - otherwise, with "live view only" (no
        # storage), the status bar would permanently stay on "measurement
        # running" even though the measurement has long since stopped.
        if session is not None:
            self._status_label.setText(
                t(
                    "measurement_completed_named",
                    name=session.config.name,
                    duration=f"{session.duration_seconds:.1f}",
                )
            )
        else:
            self._clear_measurement_status()

        self._recording_started = False
        self._setup_view.set_start_enabled(True, "")
        self._live_view.set_start_enabled(True)

        # Automatic re-arm (see `TriggerConfig.auto_rearm`): WITHOUT this
        # a start/stop trigger would not be a real trigger, just a
        # one-time condition - re-arm immediately after EVERY stop
        # (whether manual, via trigger, or recording limit; this code
        # path is the common endpoint of all three, see call sites)
        # instead of waiting for another manual click on "start
        # measurement". Only relevant if a trigger is configured at all
        # (otherwise "re-arm" would be meaningless) - see also
        # `gui/trigger_settings_dialog.py::_update_auto_rearm_visibility`.
        if not self._closing and self._trigger_config.auto_rearm and (
            self._trigger_config.start.kind != TriggerKind.NONE
            or self._trigger_config.stop.kind != TriggerKind.NONE
        ):
            next_config = self._setup_view.build_current_config()
            if next_config is not None:
                self._on_start_measurement(next_config)

    def _finalize_measurement(
        self, session: MeasurementSession, device_infos: list[DeviceInfo]
    ) -> None:
        """Writes metadata as a JSON file in the chosen storage folder."""
        assert self._storage_path is not None
        try:
            metadata = build_measurement_metadata(session, device_infos)
            metadata_path = self._storage_path / f"{session.config.name}_info.json"
            save_measurement_metadata(metadata_path, metadata)
        except Exception:
            logger.exception("Metadaten konnten nicht gespeichert werden")

    def _on_acquisition_error_gui(self, exc: Exception) -> None:
        """Slot (GUI thread) for errors from the DAQ thread."""
        self._live_view.stop_display()
        if self._storage_writer is not None:
            self._storage_writer.stop()
            self._storage_writer = None
        # The controller has already cleaned up the hardware; here only
        # the state needs a final sync (idempotent).
        self._controller.stop_measurement()
        self._setup_view.set_start_enabled(True, "")
        self._live_view.set_start_enabled(True)
        # The arm button must not stay pressed after a hardware error -
        # otherwise the (broken) measurement would immediately be
        # automatically retried.
        self._setup_view.set_trigger_armed(False)
        self._live_view.set_trigger_armed(False)
        self._trigger_config.auto_rearm = False
        self._clear_measurement_status()
        QMessageBox.critical(
            self,
            t("measurement_error"),
            t("measurement_hardware_error_body", error=exc),
        )
        self._set_nav_index(_VIEW_SETUP)

    # ------------------------------------------------------------------ #
    # Miscellaneous
    # ------------------------------------------------------------------ #

    def _on_about(self) -> None:
        QMessageBox.about(self, t("about_title"), t("about_body", version=APP_VERSION))

    def _restore_window_geometry(self) -> None:
        geom = self._configuration_manager.settings.window
        self.resize(geom.width, geom.height)
        # Only apply if the position is still on a CURRENTLY connected
        # screen (see `gui/theme.py::is_position_on_screen`) - otherwise
        # e.g. unreachable after unplugging a second monitor the window
        # was last on. Without `.move()`, Qt/the window manager uses its
        # own default placement on the remaining (primary) screen.
        center_x = geom.pos_x + geom.width // 2
        center_y = geom.pos_y + geom.height // 2
        if is_position_on_screen(center_x, center_y):
            self.move(geom.pos_x, geom.pos_y)
        if geom.maximized:
            # Only note down the state; `main.py` then shows the fully
            # constructed window exactly once.
            self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

    def _sync_popout_geometry_to_setup(self) -> None:
        """Applies the position/size of all currently open channel popout
        windows to the setup channel table (see
        `LiveView.get_open_popout_geometries()`/
        `SetupView.update_channel_display_setting`), so they are preserved
        the next time the channel configuration is saved.

        Normally, `core/controller.py`'s automatic
        `save_channel_configuration()` on EVERY measurement start is
        enough for this - but a popout position moved during a running
        (or just-finished) measurement would be lost without this
        additional step if the app is closed directly or the
        configuration is explicitly saved without restarting in between.
        """
        for key, (x, y, width, height) in self._live_view.get_open_popout_geometries().items():
            self._setup_view.update_channel_display_setting(
                key,
                {
                    "plot_popout_x": x,
                    "plot_popout_y": y,
                    "plot_popout_width": width,
                    "plot_popout_height": height,
                },
            )

    def closeEvent(self, event) -> None:
        """Saves window geometry and stops a measurement that may still
        be running."""
        # MUST be set before `_on_stop_measurement()`: otherwise an active
        # automatic re-arm (`TriggerConfig.auto_rearm`) would immediately
        # start a new measurement on close, while the window is currently
        # being torn down (see the check there).
        self._closing = True
        self._sync_popout_geometry_to_setup()
        if self._controller.is_running:
            self._on_stop_measurement()

        (
            measurement_name,
            sample_rate_hz,
            storage_format,
            recording_unlimited,
            recording_stop_value,
            recording_stop_unit,
        ) = self._setup_view.get_current_measurement_parameters()
        self._configuration_manager.update_last_measurement_parameters(
            measurement_name=measurement_name,
            sample_rate_hz=sample_rate_hz,
            storage_format=storage_format,
            recording_unlimited=recording_unlimited,
            recording_stop_value=recording_stop_value,
            recording_stop_unit=recording_stop_unit,
        )
        self._configuration_manager.update_last_trigger_settings(self._trigger_config)

        self._configuration_manager.update_window_geometry(
            width=self.width(),
            height=self.height(),
            pos_x=self.x(),
            pos_y=self.y(),
            maximized=self.isMaximized(),
        )
        # Writes, among other things, the just-synced popout geometry to
        # disk (`last_channel_configuration.json`, see
        # `_sync_popout_geometry_to_setup` above) - otherwise it would
        # only stay in memory and be lost on close, since
        # `save_channel_configuration()` is otherwise only called on every
        # measurement start (see `core/controller.py::start_measurement`).
        self._configuration_manager.save_channel_configuration(
            self._setup_view.get_configured_channels()
        )
        super().closeEvent(event)
