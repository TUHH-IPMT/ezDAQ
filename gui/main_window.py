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
from pathlib import Path

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QFileDialog,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QWidget,
)

from config.configuration_manager import ConfigurationManager
from core.controller import MeasurementController
from data.exporter import StorageWriter
from data.metadata import build_measurement_metadata, save_measurement_metadata
from data.models import MeasurementConfig, MeasurementSession, StorageFormat
from gui.analysis_view import AnalysisView
from gui.live_view import LiveView
from gui.setup_view import SetupView

logger = logging.getLogger(__name__)

_VIEW_SETUP = 0
_VIEW_LIVE = 1
_VIEW_ANALYSIS = 2


class MainWindow(QMainWindow):
    """Zentrales Anwendungsfenster mit Navigation und Ansichten."""

    # Signal zum Marshallen von DAQ-Thread-Fehlern in den GUI-Thread.
    _acquisition_error_signal = pyqtSignal(object)

    def __init__(
        self,
        controller: MeasurementController,
        configuration_manager: ConfigurationManager,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._configuration_manager = configuration_manager

        self._storage_writer: StorageWriter | None = None
        last_storage = self._configuration_manager.settings.last_storage_path
        self._storage_path: Path | None = Path(last_storage) if last_storage else None

        self.setWindowTitle("DAQSoftware - Messdatenerfassung und Analyse")
        self._restore_window_geometry()

        self._setup_view = SetupView(configuration_manager)
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
        self._live_view.stop_requested.connect(self._on_stop_measurement)

        # DAQ-Thread-Fehler thread-sicher in den GUI-Thread bringen
        self._acquisition_error_signal.connect(self._on_acquisition_error_gui)
        self._controller.add_error_listener(self._acquisition_error_signal.emit)

        # Geräte automatisch einmalig beim Start durchsuchen
        self._setup_view.discover_hardware_requested.emit()

    # ------------------------------------------------------------------ #
    # Aufbau
    # ------------------------------------------------------------------ #

    def _build_navigation_and_workspace(self) -> None:
        from PyQt6.QtWidgets import QHBoxLayout

        central = QWidget()
        root_layout = QHBoxLayout(central)

        self._nav_list = QListWidget()
        self._nav_list.addItems(["Setup", "Live View", "Analyse"])
        self._nav_list.setMaximumWidth(180)
        self._nav_list.currentRowChanged.connect(self._on_nav_changed)
        root_layout.addWidget(self._nav_list)

        self._workspace = QStackedWidget()
        self._workspace.addWidget(self._setup_view)      # index 0
        self._workspace.addWidget(self._live_view)       # index 1
        self._workspace.addWidget(self._analysis_view)   # index 2
        # "Datenverwaltung" teilt sich vorerst die Analyse-Ansicht (Laden
        # gespeicherter Messungen); eine dedizierte Verwaltung folgt später.
        root_layout.addWidget(self._workspace, stretch=1)

        self.setCentralWidget(central)
        self._nav_list.setCurrentRow(0)

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("&Datei")
        settings_action = file_menu.addAction("Einstellungen")
        settings_action.triggered.connect(self._on_settings)
        file_menu.addSeparator()
        quit_action = file_menu.addAction("Beenden")
        quit_action.triggered.connect(self.close)

        help_menu = menu_bar.addMenu("&Hilfe")
        about_action = help_menu.addAction("Über...")
        about_action.triggered.connect(self._on_about)

    def _build_status_bar(self) -> None:
        self._status_label = QLabel("Bereit")
        self.statusBar().addWidget(self._status_label)

    def _on_settings(self) -> None:
        """Öffnet den Settings-Dialog."""
        from gui.settings_dialog import SettingsDialog
        dialog = SettingsDialog(self)
        dialog.exec()

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #

    def _on_nav_changed(self, row: int) -> None:
        self._workspace.setCurrentIndex(min(row, _VIEW_ANALYSIS))

    # ------------------------------------------------------------------ #
    # Speicherort
    # ------------------------------------------------------------------ #

    def _on_choose_storage_path(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Speicherort wählen")
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
        devices = self._controller.discover_hardware()
        self._setup_view.set_discovered_devices(devices)
        self._status_label.setText(f"{len(devices)} Gerät(e) erkannt")

    def _on_start_measurement(self, config: MeasurementConfig) -> None:
        requested_measurement_name = config.name

        if config.save_to_disk:
            if self._storage_path is None:
                QMessageBox.warning(
                    self,
                    "Kein Speicherort",
                    "Bitte zuerst über 'Datei -> Speicherort wählen...' einen Ordner auswählen, "
                    "in dem die Messdaten gespeichert werden sollen.",
                )
                return

            resolved_name = self._resolve_unique_measurement_name(
                base_name=config.name,
                storage_format=config.storage_format,
            )
            if resolved_name != config.name:
                logger.info(
                    "Messname '%s' existiert bereits, verwende '%s'.",
                    config.name,
                    resolved_name,
                )
                config.name = resolved_name

        try:
            session = self._controller.start_measurement(config)
        except Exception as exc:  # MeasurementConfigError, AcquisitionError, RuntimeError
            logger.exception("Messung konnte nicht gestartet werden")
            self._setup_view.show_error(f"Messung konnte nicht gestartet werden:\n{exc}")
            return

        self._configuration_manager.update_last_measurement_parameters(
            measurement_name=requested_measurement_name,
            sample_rate_hz=config.sample_rate_hz,
            storage_format=config.storage_format.value,
            live_only=not config.save_to_disk,
        )

        # Storage Writer nur anlegen, wenn Speicherung gewünscht
        if config.save_to_disk:
            ring_buffer = self._controller.get_ring_buffer()
            extension = ".parquet" if config.storage_format == StorageFormat.PARQUET else ".csv"
            data_path = self._storage_path / f"{config.name}{extension}"
            self._storage_writer = StorageWriter(
                ring_buffer=ring_buffer,
                channels=config.active_channels(),
                output_path=data_path,
                storage_format=config.storage_format,
                sample_rate_hz=config.sample_rate_hz,
            )
            self._storage_writer.start()
        else:
            self._storage_writer = None

        self._setup_view.set_start_enabled(False, "Messung läuft ...")
        self._live_view.start_display(config.active_channels(), config.sample_rate_hz)
        self._nav_list.setCurrentRow(1)  # zur Live View wechseln
        self._status_label.setText(f"Messung '{config.name}' läuft ...")

    def _resolve_unique_measurement_name(
        self,
        base_name: str,
        storage_format: StorageFormat,
    ) -> str:
        """Ermittelt einen eindeutigen Messnamen fuer den Speicherordner.

        Es wird immer ein laufender Suffix angehaengt, beginnend mit
        `_001`, danach `_002`, ...
        """
        if self._storage_path is None:
            return base_name

        extension = ".parquet" if storage_format == StorageFormat.PARQUET else ".csv"

        def name_conflicts(name: str) -> bool:
            data_path = self._storage_path / f"{name}{extension}"
            metadata_path = self._storage_path / f"{name}_info.json"
            return data_path.exists() or metadata_path.exists()

        for index in range(1, 10_000):
            candidate = f"{base_name}_{index:03d}"
            if not name_conflicts(candidate):
                return candidate

        raise RuntimeError(
            "Konnte keinen eindeutigen Messnamen finden "
            f"(Basisname '{base_name}')."
        )

    def _on_stop_measurement(self) -> None:
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
            self._finalize_measurement(session)

        self._setup_view.set_start_enabled(True, "")
        self._nav_list.setCurrentRow(0)

    def _finalize_measurement(self, session: MeasurementSession) -> None:
        """Schreibt Metadaten als JSON-Datei im gewählten Speicherordner."""
        assert self._storage_path is not None
        try:
            metadata = build_measurement_metadata(session, self._controller.active_device_infos)
            metadata_path = self._storage_path / f"{session.config.name}_info.json"
            save_measurement_metadata(metadata_path, metadata)
            self._status_label.setText(
                f"Messung '{session.config.name}' abgeschlossen "
                f"({session.duration_seconds:.1f} s)"
            )
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
        QMessageBox.critical(
            self,
            "Messfehler",
            f"Die Messung wurde aufgrund eines Hardwarefehlers beendet:\n{exc}",
        )
        self._nav_list.setCurrentRow(0)

    # ------------------------------------------------------------------ #
    # Sonstiges
    # ------------------------------------------------------------------ #

    def _on_about(self) -> None:
        QMessageBox.about(
            self,
            "Über DAQSoftware",
            "DAQSoftware\n\nSchlanke Alternative zur Messdatenerfassung und "
            "-analyse mit NI cDAQ (NI 9215 / NI 9234).",
        )

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
        ) = self._setup_view.get_current_measurement_parameters()
        self._configuration_manager.update_last_measurement_parameters(
            measurement_name=measurement_name,
            sample_rate_hz=sample_rate_hz,
            storage_format=storage_format,
            live_only=live_only,
        )

        self._configuration_manager.update_window_geometry(
            width=self.width(),
            height=self.height(),
            pos_x=self.x(),
            pos_y=self.y(),
            maximized=self.isMaximized(),
        )
        super().closeEvent(event)
