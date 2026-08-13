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
from PyQt6.QtGui import QActionGroup, QIcon
from PyQt6.QtWidgets import (
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
from data.models import DeviceInfo, MeasurementConfig, MeasurementSession, StorageFormat
from gui.analysis_view import AnalysisView
from gui.i18n import connect_language_changed, get_language, set_language, t
from gui.live_view import LiveView
from gui.setup_view import NamingScheme, SetupView
from gui.workers import BackgroundWorker
from gui.theme import (
    connect_theme_changed,
    draw_magnifier_icon,
    draw_gear_icon,
    draw_play_icon,
    get_theme,
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
        self._build_status_bar()

        if self._storage_path is not None:
            self._setup_view.set_storage_path(str(self._storage_path))

        # Signalverbindungen der Ansichten
        self._setup_view.discover_hardware_requested.connect(self._on_discover_hardware)
        self._setup_view.start_measurement_requested.connect(self._on_start_measurement)
        self._setup_view.storage_path_requested.connect(self._on_choose_storage_path)
        self._live_view.start_requested.connect(self._on_start_measurement_from_live)
        self._live_view.stop_requested.connect(self._on_stop_measurement)

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
            "   border: 2px outset palette(mid);"
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
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            widget.update()

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()

        self._file_menu = menu_bar.addMenu(f"&{t('menu_file')}")
        self._save_config_action = self._file_menu.addAction(f"{t('menu_save_config')}...")
        self._save_config_action.triggered.connect(self._on_save_config)
        self._load_config_action = self._file_menu.addAction(f"{t('menu_load_config')}...")
        self._load_config_action.triggered.connect(self._on_load_config)
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

        self._help_menu = menu_bar.addMenu(f"&{t('menu_help')}")
        self._about_action = self._help_menu.addAction(t("menu_about"))
        self._about_action.triggered.connect(self._on_about)

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
        einstellbar sein.
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
        self._quit_action.setText(t("menu_quit"))

        self._settings_menu.setTitle(f"&{t('menu_settings')}")
        self._language_menu.setTitle(t("language"))
        self._language_de_action.setText(t("german"))
        self._language_en_action.setText(t("english"))
        self._theme_menu.setTitle(t("menu_theme"))
        self._theme_light_action.setText(t("theme_light"))
        self._theme_dark_action.setText(t("theme_dark"))
        self._channel_display_action.setText(f"{t('menu_channel_display')}...")

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
        config = self._setup_view.build_current_config()
        if config is None:
            return
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
        self._status_label.setText(t("status_config_loaded", filename=file_path.name))

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #

    def _on_nav_changed(self, row: int) -> None:
        self._workspace.setCurrentIndex(min(row, _VIEW_ANALYSIS))
        # Live View schon beim Wechsel dorthin mit den aktuell im Setup
        # konfigurierten Kanälen "vorbelegen" (Plot-Fenster stehen dann
        # schon bereit, statt erst nach dem Messstart) - nur ohne laufende
        # Messung, siehe `LiveView.preview_channels`.
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
        # Nur Geräte MIT Kanälen zählen (dieselbe Filterung wie
        # `SetupView.set_discovered_devices`) - `System.local().devices`
        # liefert sonst auch reine Chassis-Einträge ohne eigene Kanäle
        # (z. B. "cDAQ9185-0217ED5E" zusätzlich zu dessen Modulen
        # "...Mod1"/"...Mod2") mit, was die Anzahl gegenüber der tatsächlich
        # nutzbaren Hardware künstlich aufbläht.
        usable_devices = [d for d in devices if d.num_channels > 0]
        self._status_label.setText(f"{len(usable_devices)} {t('devices_found')}")

    def _on_discover_hardware_failed(self, message: str) -> None:
        self._discovery_worker = None
        self._setup_view.set_discovery_in_progress(False)
        logger.error("Geräteerkennung fehlgeschlagen: %s", message)
        self._status_label.setText(t("device_discovery_failed"))

    def _forget_background_worker(self, worker: BackgroundWorker) -> None:
        """Entfernt eine abgeschlossene `BackgroundWorker`-Referenz, damit
        `_background_workers` bei langer Programmlaufzeit nicht unbegrenzt
        wächst."""
        if worker in self._background_workers:
            self._background_workers.remove(worker)
        worker.deleteLater()

    def _on_start_measurement(self, config: MeasurementConfig) -> None:
        requested_measurement_name = config.name

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
            session = self._controller.start_measurement(config)
        except Exception as exc:  # MeasurementConfigError, AcquisitionError, RuntimeError
            logger.exception("Messung konnte nicht gestartet werden")
            self._setup_view.show_error(f"{t('cannot_start_measurement')}:\n{exc}")
            return

        self._configuration_manager.update_last_measurement_parameters(
            measurement_name=requested_measurement_name,
            sample_rate_hz=config.sample_rate_hz,
            storage_format=config.storage_format.value,
            live_only=not config.save_to_disk,
            recording_unlimited=config.recording_unlimited,
            recording_stop_value=config.recording_stop_value,
            recording_stop_unit=config.recording_stop_unit.value,
        )

        # Storage Writer nur anlegen, wenn Speicherung gewünscht
        if config.save_to_disk:
            ring_buffer = self._controller.get_ring_buffer()
            extension = ".parquet" if config.storage_format == StorageFormat.PARQUET else ".csv"
            data_path = self._storage_path / f"{config.name}{extension}"
            self._storage_writer = StorageWriter(
                ring_buffer=ring_buffer,
                channels=self._controller.active_channels,
                output_path=data_path,
                storage_format=config.storage_format,
                sample_rate_hz=config.sample_rate_hz,
            )
            self._storage_writer.start()
        else:
            self._storage_writer = None

        self._setup_view.set_start_enabled(False, "measurement_running")
        self._live_view.set_start_enabled(False)
        self._live_view.start_display(
            self._controller.active_channels,
            config.sample_rate_hz,
            storage_writer=self._storage_writer,
        )
        self._set_nav_index(_VIEW_LIVE)
        self._status_label.setText(t("measurement_running_named", name=config.name))

    def _on_start_measurement_from_live(self) -> None:
        config = self._setup_view.build_current_config()
        if config is None:
            return
        self._on_start_measurement(config)

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

        # Schreibe Metadaten nur, wenn die Messung tatsächlich gespeichert wurde
        if (
            session is not None
            and self._storage_path is not None
            and session.config.save_to_disk
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

        self._setup_view.set_start_enabled(True, "")
        self._live_view.set_start_enabled(True)

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
        self.move(geom.pos_x, geom.pos_y)
        if geom.maximized:
            self.showMaximized()

    def closeEvent(self, event) -> None:
        """Speichert Fenstergeometrie und stoppt eine ggf. laufende Messung."""
        if self._controller.is_running:
            self._on_stop_measurement()

        (
            measurement_name,
            sample_rate_hz,
            storage_format,
            live_only,
            recording_unlimited,
            recording_stop_value,
            recording_stop_unit,
        ) = self._setup_view.get_current_measurement_parameters()
        self._configuration_manager.update_last_measurement_parameters(
            measurement_name=measurement_name,
            sample_rate_hz=sample_rate_hz,
            storage_format=storage_format,
            live_only=live_only,
            recording_unlimited=recording_unlimited,
            recording_stop_value=recording_stop_value,
            recording_stop_unit=recording_stop_unit,
        )

        self._configuration_manager.update_window_geometry(
            width=self.width(),
            height=self.height(),
            pos_x=self.x(),
            pos_y=self.y(),
            maximized=self.isMaximized(),
        )
        super().closeEvent(event)
