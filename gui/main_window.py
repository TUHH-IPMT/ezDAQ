"""
gui/main_window.py

Hauptfenster der Anwendung.

Aufbau (siehe Vorgabe):
    * Menüleiste
    * Seitenleiste (Navigation: Setup / Live View / Analyse / Datenverwaltung)
    * Arbeitsbereich (QStackedWidget mit den einzelnen Ansichten)
    * Statusleiste

Das Hauptfenster ist die einzige GUI-Komponente, die den
`MeasurementController` direkt kennt. Es übersetzt Signale der Ansichten
(z. B. "Messung starten") in Controller-Aufrufe und verteilt Ergebnisse
zurück an die Ansichten. Dadurch bleiben die einzelnen Views von der
Steuerlogik entkoppelt.

Thread-Sicherheit:
    Fehler aus dem DAQ-Thread erreichen das Hauptfenster über den
    Controller-Error-Listener, der IM DAQ-THREAD läuft. Um Qt-Widgets nur
    aus dem GUI-Thread zu berühren, wird der Fehler über ein Qt-Signal
    (`_acquisition_error_signal`) in den GUI-Thread marshallt - Qt stellt
    eine über Thread-Grenzen emittierte Signal-Slot-Verbindung
    automatisch als `QueuedConnection` zu, sodass der Slot im GUI-Thread
    ausgeführt wird.
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
    QStackedWidget,
    QWidget,
)

from config.configuration_manager import ConfigurationManager
from config.sensor_database import SensorDatabaseManager
from config.settings import get_resource_path
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
    connect_theme_changed,
    draw_magnifier_icon,
    draw_gear_icon,
    draw_play_icon,
    get_theme,
    is_position_on_screen,
    repolish,
    set_theme,
)

logger = logging.getLogger(__name__)

_VIEW_SETUP = 0
_VIEW_LIVE = 1
_VIEW_ANALYSIS = 2

# (Zeilenindex, i18n-Key, Icon-Zeichenfunktion) je Navigationskachel - eine
# Stelle für `_build_navigation_and_workspace()` und `retranslate_ui()`.
# Eigene, theme-faehige Icons statt QStyle.standardIcon() (siehe
# gui/theme.py - Qt-Standardicons sind hier NICHT palettenabhängig).
_NAV_ITEMS = [
    (_VIEW_SETUP, "nav_setup", draw_gear_icon),
    (_VIEW_LIVE, "nav_live_view", draw_play_icon),
    (_VIEW_ANALYSIS, "analysis", draw_magnifier_icon),
]


class MainWindow(QMainWindow):
    """Zentrales Anwendungsfenster mit Navigation und Ansichten."""

    # Signal zum Marshallen von DAQ-Thread-Fehlern in den GUI-Thread.
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

        # Referenzen auf laufende Hintergrund-Worker (siehe gui/workers.py)
        # - müssen bis zum Abschluss am Leben gehalten werden, sonst würde
        # Python das QThread-Objekt vorzeitig einsammeln.
        self._background_workers: list[BackgroundWorker] = []
        self._discovery_worker: BackgroundWorker | None = None

        self._storage_writer: StorageWriter | None = None
        last_storage = self._configuration_manager.settings.last_storage_path
        self._storage_path: Path | None = Path(last_storage) if last_storage else None
        # Verhindert, dass eine automatische Neubewaffnung
        # (`TriggerConfig.auto_rearm`) beim Schließen der App eine neue
        # Messung startet (siehe `closeEvent`/`_on_stop_measurement`).
        self._closing = False

        # Zustand fuer automatische Mess-Trigger (siehe
        # `data/models.py::TriggerConfig`, `_on_start_measurement`). Besitzt
        # die aktuelle Konfiguration selbst (nicht mehr die Setup-Ansicht,
        # siehe `gui/trigger_settings_dialog.py`) - vorbelegt mit der
        # zuletzt verwendeten Konfiguration.
        self._trigger_config: TriggerConfig = TriggerConfig.from_dict(
            configuration_manager.settings.last_trigger_config
        )
        self._start_serial_listener: SerialTriggerListener | None = None
        self._stop_serial_listener: SerialTriggerListener | None = None
        # False waehrend der Scharf-Phase (Start-Trigger noch nicht
        # ausgeloest) - steuert, ob `_on_stop_measurement` Metadaten
        # schreibt (siehe dort).
        self._recording_started: bool = False

        self.setWindowTitle(t("window_title"))
        # .ico statt .png (siehe main.py) - mehrere Aufloesungen fuer
        # Titelleiste/Taskleiste statt einer einzelnen 256px-Groesse.
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

        # Signalverbindungen der Ansichten
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

        # Scharf-Button (Setup UND Live-Ansicht) nur sichtbar, wenn beim
        # Start bereits ein Trigger konfiguriert/geladen ist (siehe
        # `_on_open_trigger_settings_dialog` für Aktualisierung nach einer
        # Änderung).
        self._update_trigger_arm_available()

        # DAQ-Thread-Fehler thread-sicher in den GUI-Thread bringen
        self._acquisition_error_signal.connect(self._on_acquisition_error_gui)
        self._controller.add_error_listener(self._acquisition_error_signal.emit)

        # Geräte automatisch einmalig beim Start durchsuchen
        self._setup_view.discover_hardware_requested.emit()

        connect_language_changed(self.retranslate_ui)
        connect_theme_changed(self._retheme_nav_icons)

    # ------------------------------------------------------------------ #
    # Aufbau
    # ------------------------------------------------------------------ #

    def _build_navigation_and_workspace(self) -> None:
        from PyQt6.QtWidgets import QButtonGroup, QHBoxLayout, QToolButton, QVBoxLayout

        central = QWidget()
        root_layout = QHBoxLayout(central)

        nav_container = QWidget()
        nav_container.setFixedWidth(180)
        nav_container.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        nav_layout = QVBoxLayout(nav_container)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(8)
        # 3D-Bevel-Optik: "outset"/helles Gefaelle im Normalzustand wirkt
        # erhaben, "inset"/dunkles Gefaelle im gecheckten Zustand simuliert
        # das Reindruecken. Padding bewusst in BEIDEN Zuständen identisch,
        # damit Icon+Text immer exakt mittig bleiben (kein Verschieben).
        nav_container.setStyleSheet(
            "QToolButton {"
            # palette(dark) statt palette(mid): "mid" liegt in beiden
            # Themes zu nah am Hintergrund (Kontrastdifferenz ~13-40) und
            # der Rahmen war dadurch kaum sichtbar - "dark" verdoppelt den
            # Kontrast (~33-80) und bleibt in beiden Themes gut erkennbar,
            # ohne die 3D-Bevel-Optik selbst zu verändern.
            "   border: 2px outset palette(dark);"
            "   border-radius: 8px;"
            "   background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            "                                stop:0 palette(light), stop:1 palette(button));"
            "   padding: 8px;"
            "}"
            "QToolButton:hover:!checked {"
            "   background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            "                                stop:0 palette(light), stop:1 palette(midlight));"
            "}"
            "QToolButton:checked {"
            "   border: 2px inset palette(dark);"
            "   background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            "                                stop:0 palette(dark), stop:1 palette(midlight));"
            "}"
            # Sobald IRGENDEIN Vorfahre ein Stylesheet trägt, rendert Qt
            # Kind-QLabels über die CSS-Engine statt rein palettenbasiert -
            # ohne diese Regel bleibt der Text unabhängig vom Theme schwarz.
            # WICHTIG: Der QSS-Rollenname für QPalette.WindowText heißt
            # "foreground", NICHT "window-text" (das wird sonst still
            # ignoriert und die Regel greift gar nicht).
            "QToolButton QLabel { color: palette(foreground); background: transparent; }"
        )

        # Icon+Text werden bewusst NICHT über QToolButton.setIcon()/setText()
        # gesetzt: Qt's eingebautes Label-Layout (CE_ToolButtonLabel) hält
        # bei sehr hohen Buttons (volle Spaltenhöhe/3) keinen festen Abstand
        # zwischen Icon und Text ein - das Icon bleibt oben "kleben", der
        # Text landet separat vertikal mittig. Stattdessen ein eigenes,
        # eng zusammenhängendes Icon+Text-Päckchen bauen und dieses als
        # Ganzes im Button zentrieren.
        self._nav_container = nav_container
        self._nav_button_group = QButtonGroup(self)
        self._nav_button_group.setExclusive(True)
        self._nav_buttons: list[QToolButton] = []
        self._nav_icon_labels: list[QLabel] = []
        self._nav_text_labels: list[QLabel] = []
        for index, key, icon in _NAV_ITEMS:
            button = QToolButton()
            button.setCheckable(True)
            # QToolButton hat standardmäßig eine "Fixed"/"Preferred"
            # Größenrichtlinie in der Vertikalen - ohne "Expanding" würde
            # das stretch=1 unten wirkungslos bleiben und die Buttons
            # blieben auf ihrer Mindesthöhe, statt die Spalte auszufüllen.
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
            # stretch=1 auf allen drei Buttons -> teilen sich die volle
            # Hoehe der Navigationsspalte gleichmaessig auf.
            nav_layout.addWidget(button, stretch=1)

        self._retheme_nav_icons()
        self._nav_button_group.idClicked.connect(self._on_nav_changed)
        root_layout.addWidget(nav_container)

        self._workspace = QStackedWidget()
        self._workspace.addWidget(self._setup_view)      # index 0
        self._workspace.addWidget(self._live_view)       # index 1
        self._workspace.addWidget(self._analysis_view)   # index 2
        # "Datenverwaltung" teilt sich vorerst die Analyse-Ansicht (Laden
        # gespeicherter Messungen); eine dedizierte Verwaltung folgt später.
        root_layout.addWidget(self._workspace, stretch=1)

        self.setCentralWidget(central)
        self._set_nav_index(_VIEW_SETUP)

    def _set_nav_index(self, index: int) -> None:
        """Wählt eine Navigationskachel programmatisch aus.

        `QButtonGroup.setChecked()` löst - anders als
        `QListWidget.setCurrentRow()` zuvor - kein Klick-Signal aus, daher
        wird die Arbeitsbereich-Seite hier explizit mitgesetzt.
        """
        self._nav_buttons[index].setChecked(True)
        self._workspace.setCurrentIndex(min(index, _VIEW_ANALYSIS))

    def _retheme_nav_icons(self) -> None:
        """Zeichnet die Navigations-Icons neu und erzwingt ein Re-Polish
        der Kachel-Stylesheets nach einem Theme-Wechsel.

        Die Icons werden mit der aktuellen `WindowText`-Farbe neu gezeichnet
        (siehe `gui/theme.py::draw_gear_icon` usw.), da sie sonst in der
        Farbe des Themes hängen blieben, mit dem der Button ursprünglich
        erzeugt wurde. Das manuelle unpolish()/polish() stellt zusätzlich
        sicher, dass die `palette(...)`-Referenzen im Kachel-Stylesheet
        (Rahmen/Verlauf) ebenfalls sofort neu gezeichnet werden.
        """
        for (_, _, draw_icon), label in zip(_NAV_ITEMS, self._nav_icon_labels):
            label.setPixmap(draw_icon(36))
        # Nicht nur den Container, sondern JEDEN einzelnen Button UND jedes
        # Kind-Label repolishen - ein QSS-Cache auf Kind-Widget-Ebene wird
        # durch das Repolish nur des Elternteils nicht zuverlässig invalidiert.
        all_widgets = [
            self._nav_container,
            *self._nav_buttons,
            *self._nav_icon_labels,
            *self._nav_text_labels,
        ]
        for widget in all_widgets:
            repolish(widget)

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
        """Globale Tastenkürzel für Messung Start/Stopp - funktionieren
        unabhängig davon, welcher Tab (Konfiguration/Live-Ansicht/Analyse)
        gerade aktiv ist, da über `self.addAction()` am Hauptfenster selbst
        registriert statt an einem einzelnen Button/View.

        Rufen dieselben Handler wie die Start-/Stopp-Buttons auf (siehe
        `_on_start_measurement_from_live`/`_on_stop_measurement`) - beide
        sind bereits dagegen abgesichert, wenn keine Messung läuft
        (Stopp) bzw. bereits eine läuft (Start meldet dann den bestehenden
        `MeasurementController`-Fehler wie ein doppelter Klick auf den
        Start-Button auch).
        """
        self._start_shortcut_action = QAction(self)
        self._start_shortcut_action.setShortcut(QKeySequence("F5"))
        # F5 startet MIT Speicherung (live_only=False), wie der
        # Aufnahme-Button - fuer die reine Live-Anzeige gibt es keinen
        # eigenen Shortcut.
        self._start_shortcut_action.triggered.connect(
            lambda: self._on_start_measurement_from_live(False)
        )
        self.addAction(self._start_shortcut_action)

        self._stop_shortcut_action = QAction(self)
        self._stop_shortcut_action.setShortcut(QKeySequence("F8"))
        self._stop_shortcut_action.triggered.connect(self._on_stop_measurement)
        self.addAction(self._stop_shortcut_action)

    def _on_language_action_triggered(self, action) -> None:
        """Wird ausgelöst, wenn der Nutzer im Menü Einstellungen -> Sprache
        eine Sprache anklickt - wirkt sofort in der laufenden App und wird
        persistiert (siehe `gui/i18n.py::set_language`)."""
        new_language = action.data()
        set_language(new_language)
        self._configuration_manager.update_language(new_language)

    def _on_theme_action_triggered(self, action) -> None:
        """Wird ausgelöst, wenn der Nutzer im Menü Einstellungen -> Design
        Hell/Dunkel anklickt - wirkt sofort in der laufenden App und wird
        persistiert (siehe `gui/theme.py::set_theme`)."""
        new_theme = action.data()
        set_theme(new_theme)
        self._configuration_manager.update_theme(new_theme)

    def _on_open_channel_display_dialog(self) -> None:
        """Öffnet den Kanal-Darstellung-Dialog mit den im Setup konfigurierten
        Kanälen (siehe `SetupView.get_configured_channels()`).

        Bewusst NICHT `self._live_view.open_channel_display_dialog()` ohne
        Argument: die Live View kennt ihre Kanäle erst, sobald eine
        Messung tatsächlich läuft (`start_display()`) - die Darstellung
        soll aber schon vorher, direkt nach dem Konfigurieren im Setup,
        einstellbar sein - AUCH für Kanäle, die noch keinen zugewiesenen
        Hardwarekanal haben (siehe `gui/live_view.py::_channel_display_key`
        für die dafür nötige Schlüssel-Behandlung).
        """
        channels = [ch for ch in self._setup_view.get_configured_channels() if ch.enabled]
        settings = self._live_view.open_channel_display_dialog(channels)
        if settings is not None:
            # Zurück ins Setup schreiben, damit die Werte beim Speichern
            # der Konfiguration erhalten bleiben (siehe
            # `SetupView.apply_channel_display_settings`) - die Live View
            # kennt hier nur die übergebene Kopie, nicht die Kanaltabelle.
            self._setup_view.apply_channel_display_settings(settings)

    def _on_open_sensor_database(self) -> None:
        """Öffnet die Sensor-Datenbank-Verwaltung (siehe
        gui/sensor_database_dialog.py). Änderungen werden dort sofort
        persistiert - dieser Dialog reicht daher nur die (bereits von
        `main.py` erzeugte) `SensorDatabaseManager`-Instanz durch."""
        from gui.sensor_database_dialog import SensorDatabaseDialog

        dialog = SensorDatabaseDialog(self._sensor_database, self)
        dialog.exec()

    def _on_open_trigger_settings_dialog(self) -> None:
        """Öffnet den Trigger-Einstellungen-Dialog (siehe
        `gui/trigger_settings_dialog.py::TriggerSettingsDialog`) mit den im
        Setup konfigurierten AKTIVEN Kanälen als Auswahl für einen
        Schwellwert-Trigger - wie beim Kanal-Darstellung-Dialog schon vor
        dem Messstart nutzbar (siehe `_on_open_channel_display_dialog`)."""
        channels = [ch for ch in self._setup_view.get_configured_channels() if ch.enabled]
        dialog = TriggerSettingsDialog(self._trigger_config, channels, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._trigger_config = dialog.results()
            self._configuration_manager.update_last_trigger_settings(self._trigger_config)
            self._update_trigger_arm_available()

    def _update_trigger_arm_available(self) -> None:
        """Blendet den Scharf-Button in BEIDEN Ansichten (Setup und Live)
        synchron ein/aus - siehe `SetupView.set_trigger_arm_available`/
        `LiveView.set_trigger_arm_available`."""
        available = (
            self._trigger_config.start.kind != TriggerKind.NONE
            or self._trigger_config.stop.kind != TriggerKind.NONE
        )
        self._setup_view.set_trigger_arm_available(available)
        self._live_view.set_trigger_arm_available(available)

    def _build_status_bar(self) -> None:
        self._status_label = QLabel(t("ready"))
        self.statusBar().addWidget(self._status_label)

    def retranslate_ui(self) -> None:
        """Aktualisiert alle statischen Texte des Hauptfensters nach einem
        Sprachwechsel (siehe `gui/i18n.py::connect_language_changed`)."""
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

        # Der Statustext ist meist ein einmaliges Ereignis (Start/Stop/
        # Fehler) und korrigiert sich beim nächsten Ereignis von selbst.
        # Nur der Leerlauf-Zustand würde sonst dauerhaft in der alten
        # Sprache hängen bleiben.
        if not self._controller.is_running:
            self._status_label.setText(t("ready"))

    # ------------------------------------------------------------------ #
    # Gespeicherte Messkonfigurationen (Datei-Menü)
    # ------------------------------------------------------------------ #

    def _on_save_config(self) -> None:
        """Speichert die aktuell in der Setup-Ansicht eingestellte Konfiguration.

        Der Speicherort wird per Dateidialog vom Nutzer gewählt (kein
        interner Namens-Katalog) - Konfigurationsdateien sind damit normale
        Dateien, die frei abgelegt/umbenannt/geteilt werden können.
        """
        # Aktuelle Popout-Fensterposition uebernehmen, BEVOR die Kanäle
        # ausgelesen werden - siehe `_sync_popout_geometry_to_setup`.
        self._sync_popout_geometry_to_setup()
        config = self._setup_view.build_current_config()
        if config is None:
            return
        # Wie bei `_on_start_measurement`: `SetupView.build_current_config()`
        # kennt die Trigger-Konfiguration nicht mehr (siehe
        # `gui/trigger_settings_dialog.py`) - ohne diese Zeile wuerde
        # "Konfiguration speichern" den aktiven Trigger stillschweigend
        # verlieren (Default-`TriggerConfig()` statt der echten Werte).
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
        """Lässt den Nutzer eine Konfigurationsdatei auswählen und lädt sie."""
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
        # Die geladene Konfiguration bringt ihre eigene Trigger-Konfiguration
        # mit (siehe `_on_save_config`) - MainWindow uebernimmt sie hier als
        # neue aktive Konfiguration (siehe `_trigger_config`).
        self._trigger_config = config.trigger
        self._status_label.setText(t("status_config_loaded", filename=file_path.name))

    def _on_load_measurement(self) -> None:
        """Lässt den Nutzer eine abgeschlossene Messung auswählen und
        springt bei Auswahl direkt in den Analyse-Tab (siehe
        `AnalysisView.prompt_and_load_file` - ehemals ein Button direkt in
        der Analyse-Ansicht, jetzt aus jeder Ansicht heraus erreichbar).
        """
        if self._analysis_view.prompt_and_load_file():
            self._set_nav_index(_VIEW_ANALYSIS)

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #

    def _on_nav_changed(self, row: int) -> None:
        self._workspace.setCurrentIndex(min(row, _VIEW_ANALYSIS))
        # Live View schon beim Wechsel dorthin mit den aktuell im Setup
        # konfigurierten Kanälen "vorbelegen" (Plot-Fenster stehen dann
        # schon bereit, statt erst nach dem Messstart) - nur ohne laufende
        # Messung, siehe `LiveView.preview_channels`. Funktioniert
        # ausdrücklich AUCH für Kanäle ohne zugewiesenen Hardwarekanal
        # (noch keine Hardware angeschlossen).
        if row == _VIEW_LIVE and not self._controller.is_running:
            channels = [ch for ch in self._setup_view.get_configured_channels() if ch.enabled]
            self._live_view.preview_channels(channels)

    # ------------------------------------------------------------------ #
    # Speicherort
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
        # Speicherort-Anzeige in Statusleiste entfernt; wird nur noch in Setup-Ansicht angezeigt
        pass

    # ------------------------------------------------------------------ #
    # Hardware / Messung
    # ------------------------------------------------------------------ #

    def _on_discover_hardware(self) -> None:
        """Startet die Geräteerkennung im Hintergrund (siehe
        `gui/workers.py::BackgroundWorker`).

        `nidaqmx.system.System.local()` plus Kanal-Iteration je Gerät
        (`hardware/nidaq_device.py::discover_devices`) kann bei mehreren
        Chassis/Modulen oder Treiber-Timeouts spürbar dauern - lief
        vorher synchron im GUI-Thread und blockierte dabei auch den
        automatischen Erkennungslauf beim Programmstart (siehe `__init__`).
        """
        if self._discovery_worker is not None:  # bereits eine Anfrage aktiv
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
        # Nur Geräte MIT Analogeingangs-Kanälen zählen - `System.local().devices`
        # liefert sonst auch reine Chassis-Einträge ohne eigene Kanäle
        # (z. B. "cDAQ9185-0217ED5E" zusätzlich zu dessen Modulen
        # "...Mod1"/"...Mod2") mit, was die Anzahl gegenüber der tatsächlich
        # nutzbaren Hardware künstlich aufbläht. Bewusst ENGER als der
        # Filter in `SetupView.set_discovered_devices` (der zusätzlich
        # `has_any_channels` zulässt, um auch nicht unterstützte
        # Nicht-AI-Module wie das NI9263 anzuzeigen/zu melden) - hier
        # zählt explizit nur, was diese App tatsächlich als Kanal
        # konfigurieren kann.
        usable_devices = [d for d in devices if d.num_channels > 0]
        self._status_label.setText(f"{len(usable_devices)} {t('devices_found')}")

    def _on_discover_hardware_failed(self, message: str) -> None:
        self._discovery_worker = None
        self._setup_view.set_discovery_in_progress(False)
        logger.error("Geräteerkennung fehlgeschlagen: %s", message)
        self._status_label.setText(t("device_discovery_failed"))
        # Ursache (z. B. "NI-DAQmx-Treiber nicht installiert") sichtbar im
        # Gerätebrowser selbst, nicht nur in Statusleiste/Log - dort schaut
        # der Nutzer als nächstes hin.
        self._setup_view.show_discovery_error(message)

    def _on_open_ni_max(self) -> None:
        """Öffnet NI-MAX (Measurement & Automation Explorer) als separates
        Programm (siehe `hardware/nidaq_device.py::open_ni_max`)."""
        try:
            self._controller.open_ni_max()
        except Exception as exc:
            QMessageBox.warning(self, t("error"), f"{t('ni_max_open_failed')}:\n{exc}")

    def _forget_background_worker(self, worker: BackgroundWorker) -> None:
        """Entfernt eine abgeschlossene `BackgroundWorker`-Referenz, damit
        `_background_workers` bei langer Programmlaufzeit nicht unbegrenzt
        wächst."""
        if worker in self._background_workers:
            self._background_workers.remove(worker)
        worker.deleteLater()

    def _on_start_measurement(self, config: MeasurementConfig) -> None:
        requested_measurement_name = config.name
        # Trigger-Konfiguration gehoert seit der Verallgemeinerung auf
        # Start UND Stopp `MainWindow` selbst (siehe `TriggerSettingsDialog`)
        # - `SetupView.build_current_config()` liefert nur noch eine leere
        # Default-`TriggerConfig()`, hier wird die tatsaechlich aktive
        # Konfiguration eingespeist.
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
            # Die zuletzt erkannte Geräteliste wiederverwenden statt bei
            # jedem Messstart erneut zu erkennen (siehe
            # `SetupView.get_discovered_devices`) - spart die bei mehreren
            # Chassis/Modulen spürbar langsame Rediscovery.
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

        # Tatsaechliche Tick-Rate des Ring Buffers (= schnellste
        # Ratengruppe, siehe data/models.py::resolve_rate_groups) statt
        # der rohen Zielrate - weicht nur ab, wenn z. B. ein NI9210 eine
        # eigene Gruppe erzwingt hat. StorageWriter/LiveView muessen die
        # ECHTE Tick-Rate kennen, sonst waere die gespeicherte
        # time_s-Spalte falsch skaliert.
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
            # Manueller Start: StorageWriter (falls gewuenscht) wird SOFORT
            # angelegt und gestartet (bisheriges Verhalten unveraendert).
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
            # Nullpunkt fuer ein evtl. konfiguriertes Aufnahme-Limit UND
            # Reset des Stopp-Trigger-Flankendetektors (siehe
            # `gui/live_view.py::mark_recording_started`) - MUSS auch hier
            # aufgerufen werden, nicht nur bei einem Start-Trigger.
            self._live_view.mark_recording_started(0)
            self._maybe_start_stop_listener(config)
            self._set_nav_index(_VIEW_LIVE)
            self._status_label.setText(t("measurement_running_named", name=config.name))
            return

        # Schwellwert/Seriell START-Trigger: Hardware-Erfassung + Anzeige
        # starten sofort (Vorlauf-Pufferung, siehe
        # `data/models.py::TriggerConfig`), der StorageWriter wird ERST bei
        # `_on_trigger_fired` angelegt.
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
        """Startet den seriellen STOPP-Trigger-Lauscher, falls konfiguriert
        (siehe `TriggerConfig.stop`) - aufgerufen direkt NACHDEM die
        Aufzeichnung tatsaechlich begonnen hat (`mark_recording_started`),
        unabhaengig davon ob der Start manuell oder getriggert erfolgte
        (siehe manueller Zweig oben bzw. `_on_trigger_fired`). Ein
        Schwellwert-Stopp-Trigger braucht keinen eigenen Lauscher - der
        wird bereits unabhaengig ueber
        `gui/live_view.py::_check_stop_threshold_trigger` ueberwacht."""
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
        """Ein scharf geschalteter START-Trigger hat ausgeloest (siehe
        `gui/live_view.py::LiveView.trigger_fired` bzw.
        `gui/serial_trigger.py::SerialTriggerListener.message_matched`) -
        legt jetzt (erst jetzt!) den StorageWriter an, ggf. mit
        rueckwirkendem Vorlauf (Schwellwert-Trigger, siehe
        `core/ringbuffer.py::RingBuffer.register_reader`)."""
        session = self._controller.current_session
        if session is None or self._recording_started:
            return
        config = session.config
        # Tatsaechliche Tick-Rate des Ring Buffers (siehe Kommentar in
        # `_on_start_measurement`) - der Vorlauf-Sample-Versatz UND die
        # gespeicherte time_s-Spalte muessen sich daran orientieren, nicht
        # an der rohen Zielrate.
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
        # Gleiche Clamp-Formel wie `RingBuffer.register_reader` - der
        # tatsaechliche Nullpunkt fuer das Aufnahme-Limit (siehe
        # `gui/live_view.py::mark_recording_started`) muss exakt dem
        # spaeter vom StorageWriter registrierten Reader entsprechen.
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

        # WICHTIG: `.stop()` VOR `.deleteLater()` - garantiert, dass der
        # COM-Port tatsaechlich geschlossen ist, BEVOR ein evtl.
        # konfigurierter Stopp-Trigger (siehe `_maybe_start_stop_listener`)
        # denselben Port erneut oeffnet (sonst moeglicher Ressourcen-
        # Konflikt, falls Start- und Stopp-Trigger denselben Port nutzen).
        if self._start_serial_listener is not None:
            self._start_serial_listener.stop()
            self._start_serial_listener.deleteLater()
            self._start_serial_listener = None

        self._maybe_start_stop_listener(config)

        self._status_label.setText(t("measurement_running_named", name=config.name))

    def _on_trigger_connection_failed(self, message: str) -> None:
        """Der serielle START-Trigger konnte den konfigurierten COM-Port
        nicht oeffnen (siehe `gui/serial_trigger.py::SerialTriggerListener`)
        - sauberes Disarmieren statt eine hilflos wartende Messung. Anders
        als beim Stopp-Trigger (siehe `_on_stop_trigger_connection_failed`)
        existiert hier noch KEINE laufende Aufzeichnung - ein voller
        Abbruch ist daher unproblematisch."""
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
        # Scharf-Button darf nicht gedrueckt bleiben - es wird nichts mehr
        # automatisch neu versucht (ein kaputter Port bliebe sonst kaputt).
        self._setup_view.set_trigger_armed(False)
        self._live_view.set_trigger_armed(False)
        self._trigger_config.auto_rearm = False
        self._status_label.setText(t("ready"))
        QMessageBox.warning(self, t("trigger_connection_failed_title"), message)
        self._set_nav_index(_VIEW_SETUP)

    def _on_stop_trigger_connection_failed(self, message: str) -> None:
        """Der serielle STOPP-Trigger konnte den konfigurierten COM-Port
        nicht oeffnen - anders als beim Start-Trigger
        (`_on_trigger_connection_failed`) darf das die laufende
        Aufzeichnung NICHT abbrechen: zu diesem Zeitpunkt werden ggf.
        schon echte Messdaten geschrieben. Nur der Stopp-Lauscher wird
        aufgeraeumt, die Messung laeuft unveraendert weiter (manueller
        Stopp/Aufnahme-Limit funktionieren unabhaengig davon weiter)."""
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
        """Reagiert auf den Scharf-Button (Setup- UND Live-Ansicht besitzen
        je ein eigenes, aber gleichbedeutendes Exemplar - siehe
        `gui/setup_view.py`/`gui/live_view.py::_trigger_arm_button`).

        Scharf schalten (checked=True) startet SOFORT den ersten Zyklus UND
        setzt `TriggerConfig.auto_rearm`, sodass `_on_stop_measurement`
        nach JEDEM Stopp (manuell, per Trigger oder Aufnahme-Limit)
        automatisch neu startet - beide Buttons bleiben dabei die ganze
        Zeit gedrückt, unabhängig davon wie oft der Zyklus zwischenzeitlich
        automatisch durchläuft. Entschärfen (checked=False) beendet die
        automatische Neubewaffnung UND eine gerade laufende/scharfe
        Messung sofort - das ist der einzige Weg, den Zyklus wirklich zu
        beenden (ein einzelner manueller Stopp reicht dafür bewusst NICHT,
        siehe `_on_stop_measurement`).
        """
        self._trigger_config.auto_rearm = checked
        # Beide Buttons synchron halten, egal welcher den Klick ausgelöst
        # hat - `set_trigger_armed()` blockt dabei `toggled`, damit kein
        # Rueckkopplungs-Loop entsteht (siehe dortige Doku).
        self._setup_view.set_trigger_armed(checked)
        self._live_view.set_trigger_armed(checked)

        if checked:
            config = self._setup_view.build_current_config()
            if config is None:
                # Ungueltige Konfiguration (z. B. kein aktiver Kanal) -
                # Button darf nicht gedrueckt bleiben, es passiert ja nichts.
                self._setup_view.set_trigger_armed(False)
                self._live_view.set_trigger_armed(False)
                self._trigger_config.auto_rearm = False
                return
            self._on_start_measurement(config)
            if not self._controller.is_running:
                # Start ist synchron fehlgeschlagen (siehe Fehlerbehandlung
                # in `_on_start_measurement`) - Button nicht gedrueckt lassen.
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
        """Baut aus dem eingegebenen Messnamen den tatsächlich zu
        verwendenden Datei-/Messnamen gemäß `naming` auf.

        Reihenfolge der optionalen Bestandteile: Name_Datum_Uhrzeit_Nummer.
        Ist kein Nummernsuffix aktiv und der aufgelöste Name existiert
        bereits, wird die Messung mit einer Fehlermeldung abgebrochen -
        ein automatisches Überschreiben vorhandener Messdaten findet
        bewusst nicht statt.

        Returns:
            Den aufzulösenden Namen, oder None, falls die Messung wegen
            eines Namenskonflikts ohne automatische Auflösung abgebrochen
            werden soll.
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
        # Noch laufende serielle Trigger-Lauscher (Start UND Stopp) muessen
        # VOR dem eigentlichen Stoppen beendet werden (z. B. Abbruch
        # waehrend der Scharf-Phase, oder wenn der Stopp-Trigger selbst
        # diesen Aufruf ausgeloest hat) - sonst blieben Hintergrund-Threads
        # verwaist.
        if self._start_serial_listener is not None:
            self._start_serial_listener.stop()
            self._start_serial_listener.deleteLater()
            self._start_serial_listener = None
        if self._stop_serial_listener is not None:
            self._stop_serial_listener.stop()
            self._stop_serial_listener.deleteLater()
            self._stop_serial_listener = None

        # WICHTIG: active_device_infos VOR stop_measurement() auslesen -
        # der Controller leert seine interne Geräteliste beim Stoppen,
        # danach ausgelesen wäre die Liste immer leer und die
        # Metadaten-Datei würde nie echte Hardwareinformationen enthalten.
        device_infos = self._controller.active_device_infos
        session = self._controller.stop_measurement()
        self._live_view.stop_display()

        if self._storage_writer is not None:
            self._storage_writer.stop()
            self._storage_writer = None

        # Schreibe Metadaten nur, wenn die Messung tatsächlich gespeichert
        # UND tatsächlich aufgezeichnet wurde - bei einem Abbruch waehrend
        # der Scharf-Phase (Trigger nie ausgeloest, siehe
        # `_recording_started`) existiert gar kein StorageWriter/keine
        # Datendatei, eine Metadaten-Datei dafuer waere irrefuehrend.
        if (
            session is not None
            and self._storage_path is not None
            and session.config.save_to_disk
            and self._recording_started
        ):
            self._finalize_measurement(session, device_infos)

        # Status IMMER aktualisieren, nicht nur wenn tatsächlich gespeichert
        # wurde (siehe `_finalize_measurement`) - sonst bliebe die
        # Statusleiste bei "Nur Live anzeigen" (kein Speichern) dauerhaft
        # auf "Messung läuft" stehen, obwohl die Messung längst gestoppt ist.
        if session is not None:
            self._status_label.setText(
                t(
                    "measurement_completed_named",
                    name=session.config.name,
                    duration=f"{session.duration_seconds:.1f}",
                )
            )
        else:
            self._status_label.setText(t("ready"))

        self._recording_started = False
        self._setup_view.set_start_enabled(True, "")
        self._live_view.set_start_enabled(True)

        # Automatische Neubewaffnung (siehe `TriggerConfig.auto_rearm`):
        # OHNE das waere ein Start-/Stopp-Trigger kein echter Trigger,
        # sondern nur eine einmalige Bedingung - nach JEDEM Stopp (egal ob
        # manuell, per Trigger oder Aufnahme-Limit; dieser Codepfad ist der
        # gemeinsame Endpunkt aller drei, siehe Aufrufstellen) sofort wieder
        # scharf schalten, statt auf einen erneuten manuellen Klick auf
        # "Messung starten" zu warten. Nur relevant, wenn ueberhaupt ein
        # Trigger konfiguriert ist (sonst waere "neu scharf schalten"
        # bedeutungslos) - siehe auch
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
        """Schreibt Metadaten als JSON-Datei im gewählten Speicherordner."""
        assert self._storage_path is not None
        try:
            metadata = build_measurement_metadata(session, device_infos)
            metadata_path = self._storage_path / f"{session.config.name}_info.json"
            save_measurement_metadata(metadata_path, metadata)
        except Exception:
            logger.exception("Metadaten konnten nicht gespeichert werden")

    def _on_acquisition_error_gui(self, exc: Exception) -> None:
        """Slot (GUI-Thread) für Fehler aus dem DAQ-Thread."""
        self._live_view.stop_display()
        if self._storage_writer is not None:
            self._storage_writer.stop()
            self._storage_writer = None
        # Controller hat die Hardware bereits aufgeräumt; hier nur noch
        # den Zustand final synchronisieren (idempotent).
        self._controller.stop_measurement()
        self._setup_view.set_start_enabled(True, "")
        self._live_view.set_start_enabled(True)
        # Scharf-Button darf bei einem Hardware-Fehler nicht gedrueckt
        # bleiben - sonst wuerde die (kaputte) Messung sofort wieder
        # automatisch neu versucht.
        self._setup_view.set_trigger_armed(False)
        self._live_view.set_trigger_armed(False)
        self._trigger_config.auto_rearm = False
        self._status_label.setText(t("ready"))
        QMessageBox.critical(
            self,
            t("measurement_error"),
            t("measurement_hardware_error_body", error=exc),
        )
        self._set_nav_index(_VIEW_SETUP)

    # ------------------------------------------------------------------ #
    # Sonstiges
    # ------------------------------------------------------------------ #

    def _on_about(self) -> None:
        QMessageBox.about(self, t("about_title"), t("about_body"))

    def _restore_window_geometry(self) -> None:
        geom = self._configuration_manager.settings.window
        self.resize(geom.width, geom.height)
        # Nur uebernehmen, wenn die Position noch auf einem AKTUELL
        # angeschlossenen Bildschirm liegt (siehe
        # `gui/theme.py::is_position_on_screen`) - sonst z. B. nach dem
        # Abstecken eines zweiten Monitors, auf dem das Fenster zuletzt
        # stand, unerreichbar. Ohne `.move()` verwendet Qt/der Fenster-
        # manager seine eigene Standardplatzierung auf dem verbliebenen
        # (primaeren) Bildschirm.
        center_x = geom.pos_x + geom.width // 2
        center_y = geom.pos_y + geom.height // 2
        if is_position_on_screen(center_x, center_y):
            self.move(geom.pos_x, geom.pos_y)
        if geom.maximized:
            # Nur den Zustand vormerken; `main.py` zeigt das vollständig
            # aufgebaute Fenster anschließend genau einmal an.
            self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

    def _sync_popout_geometry_to_setup(self) -> None:
        """Uebernimmt Position/Groesse aller aktuell offenen Kanal-Popout-
        Fenster in die Setup-Kanaltabelle (siehe
        `LiveView.get_open_popout_geometries()`/
        `SetupView.update_channel_display_setting`), damit sie beim
        naechsten Speichern der Kanalkonfiguration erhalten bleiben.

        Normalerweise reicht dafuer `core/controller.py`s automatisches
        `save_channel_configuration()` bei JEDEM Messstart - eine waehrend
        der laufenden (oder gerade beendeten) Messung verschobene Popout-
        Position wuerde ohne diesen zusaetzlichen Schritt aber verloren
        gehen, wenn die App direkt geschlossen oder die Konfiguration
        explizit gespeichert wird, ohne zwischendurch erneut zu starten.
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
        """Speichert Fenstergeometrie und stoppt eine ggf. laufende Messung."""
        # MUSS vor `_on_stop_measurement()` gesetzt werden: sonst würde eine
        # aktive automatische Neubewaffnung (`TriggerConfig.auto_rearm`)
        # beim Schließen sofort eine neue Messung starten, während das
        # Fenster gerade abgebaut wird (siehe dortige Prüfung).
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
        # Schreibt u. a. die soeben synchronisierte Popout-Geometrie auf
        # die Platte (`last_channel_configuration.json`, siehe
        # `_sync_popout_geometry_to_setup` oben) - sonst bliebe sie nur im
        # Speicher und ginge beim Schließen verloren, da
        # `save_channel_configuration()` sonst nur bei jedem Messstart
        # aufgerufen wird (siehe `core/controller.py::start_measurement`).
        self._configuration_manager.save_channel_configuration(
            self._setup_view.get_configured_channels()
        )
        super().closeEvent(event)
