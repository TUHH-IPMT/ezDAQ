"""
gui/setup_view.py

Setup-Ansicht: Geräteerkennung, Kanalkonfiguration und Messparameter.

Funktionen (siehe Vorgabe):
    * angeschlossene NI-Geräte erkennen
    * Module anzeigen
    * Kanäle auswählen/aktivieren/deaktivieren, benennen, Einheit/
      Skalierung/Offset einstellen
    * Samplingrate einstellen
    * Speicherformat auswählen

Diese Ansicht kommuniziert ausschließlich über Signale mit
`gui/main_window.py` - sie kennt weder `MeasurementController` noch
Hardware-Details direkt.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QCheckBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from config.configuration_manager import ConfigurationManager
from data.models import DeviceInfo, MeasurementConfig, StorageFormat
from gui.widgets.channel_table import ChannelTableWidget

logger = logging.getLogger(__name__)


class SetupView(QWidget):
    """Ansicht zur Konfiguration von Hardware, Kanälen und Messparametern.

    Signals:
        discover_hardware_requested: Nutzer möchte angeschlossene Geräte
            erkennen lassen. `gui/main_window.py` ruft daraufhin
            `controller.discover_hardware()` auf und liefert das Ergebnis
            über `set_discovered_devices()` zurück.
        start_measurement_requested: Nutzer möchte die Messung mit der
            übergebenen `MeasurementConfig` starten.
    """

    discover_hardware_requested = pyqtSignal()
    start_measurement_requested = pyqtSignal(object)  # MeasurementConfig
    storage_path_requested = pyqtSignal()

    def __init__(
        self, configuration_manager: ConfigurationManager, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._configuration_manager = configuration_manager
        self._discovered_devices: list[DeviceInfo] = []

        layout = QVBoxLayout(self)

        # --- Geräteerkennung ---
        device_group = QGroupBox("Angeschlossene Geräte")
        device_layout = QVBoxLayout(device_group)
        self._discover_button = QPushButton("Geräte suchen")
        self._discover_button.clicked.connect(self.discover_hardware_requested.emit)
        self._device_list = QListWidget()
        self._device_list.currentRowChanged.connect(self._on_device_selected)
        device_layout.addWidget(self._discover_button)
        device_layout.addWidget(self._device_list)
        layout.addWidget(device_group)

        # --- Kanalkonfiguration ---
        channel_group = QGroupBox("Kanalkonfiguration")
        channel_layout = QVBoxLayout(channel_group)
        self._channel_table = ChannelTableWidget()
        channel_layout.addWidget(self._channel_table)
        layout.addWidget(channel_group, stretch=1)

        # --- Messparameter ---
        params_group = QGroupBox("Messparameter")
        params_layout = QFormLayout(params_group)

        self._name_edit = QLineEdit(
            configuration_manager.settings.last_measurement_name or "messung_001"
        )
        params_layout.addRow("Messungsname:", self._name_edit)

        self._sample_rate_spin = QDoubleSpinBox()
        self._sample_rate_spin.setRange(1.0, 100_000.0)
        self._sample_rate_spin.setDecimals(1)
        self._sample_rate_spin.setValue(
            configuration_manager.settings.default_sample_rate_hz
        )
        params_layout.addRow("Abtastrate [Hz]:", self._sample_rate_spin)

        # Interne Performance-Parameter werden automatisch festgelegt,
        # damit der Nutzer hier nicht mit technischen Details belastet wird.
        # Ziel: kleinere Read-Bloecke fuer fluessigere Live-Updates.
        self._target_read_block_ms = 25.0
        self._min_samples_per_read = 50
        self._max_samples_per_read = 2000
        self._default_ring_buffer_seconds = 30

        self._storage_format_combo = QComboBox()
        self._storage_format_combo.addItems([f.value for f in StorageFormat])
        self._storage_format_combo.setCurrentText(
            configuration_manager.settings.default_storage_format
        )
        params_layout.addRow("Speicherformat:", self._storage_format_combo)

        self._live_only_checkbox = QCheckBox("Nur Live anzeigen (nicht speichern)")
        self._live_only_checkbox.setChecked(configuration_manager.settings.last_live_only)
        params_layout.addRow("", self._live_only_checkbox)

        self._storage_path_label = QLabel("Kein Speicherort gewählt")
        self._storage_button = QPushButton("Speicherort wählen")
        self._storage_button.clicked.connect(self.storage_path_requested.emit)
        params_layout.addRow("Speicherort:", self._storage_path_label)
        params_layout.addRow("", self._storage_button)

        layout.addWidget(params_group)

        # --- Start ---
        start_row = QHBoxLayout()
        self._status_label = QLabel("")
        self._start_button = QPushButton("Messung starten")
        self._start_button.setStyleSheet(
            "QPushButton { background-color: #28a745; color: white; border: none; padding: 6px 16px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #218838; }"
            "QPushButton:pressed { background-color: #1e7e34; }"
        )
        self._start_button.clicked.connect(self._on_start_clicked)
        start_row.addWidget(self._status_label, stretch=1)
        start_row.addWidget(self._start_button)
        layout.addLayout(start_row)

        # Zuletzt verwendete Kanalkonfiguration automatisch vorschlagen.
        last_channels = configuration_manager.load_channel_configuration()
        if last_channels:
            self._channel_table.set_channels(last_channels)

    # ------------------------------------------------------------------ #
    # Öffentliche API (von main_window.py aufgerufen)
    # ------------------------------------------------------------------ #

    def set_discovered_devices(self, devices: list[DeviceInfo]) -> None:
        """Zeigt das Ergebnis einer Geräteerkennung an."""
        self._device_list.clear()
        devices_with_channels = [d for d in devices if d.num_channels > 0]
        self._discovered_devices = devices_with_channels
        if not devices_with_channels:
            self._device_list.addItem(
                "Keine Geräte gefunden (Treiber installiert? Hardware angeschlossen?)"
            )
            return
        for device in devices_with_channels:
            module_info = f" [{device.module_type.value}]" if device.module_type else ""
            self._device_list.addItem(
                f"{device.device_name} - {device.product_type}{module_info} "
                f"({device.num_channels} Kanäle)"
            )
        # Wähle erstes Gerät und trigger die Kanal-Liste
        self._device_list.setCurrentRow(0)
        self._on_device_selected(0)

    def set_start_enabled(self, enabled: bool, reason: str = "") -> None:
        """Aktiviert/deaktiviert den Start-Button (z. B. während eine Messung läuft)."""
        self._start_button.setEnabled(enabled)
        self._status_label.setText(reason)

    def set_storage_path(self, path: str | None) -> None:
        self._storage_path_label.setText(path or "Kein Speicherort gewählt")

    def show_error(self, message: str) -> None:
        """Zeigt eine Fehlermeldung an (z. B. ungültige Konfiguration)."""
        QMessageBox.warning(self, "Fehler", message)

    def get_current_measurement_parameters(self) -> tuple[str, float, str, bool]:
        """Gibt die aktuell im UI eingestellten Messparameter zurück.

        Returns:
            (measurement_name, sample_rate_hz, storage_format, live_only)
        """
        return (
            self._name_edit.text().strip() or "messung_001",
            self._sample_rate_spin.value(),
            self._storage_format_combo.currentText(),
            self._live_only_checkbox.isChecked(),
        )

    # ------------------------------------------------------------------ #
    # Interna
    # ------------------------------------------------------------------ #

    def _on_start_clicked(self) -> None:
        channels = self._channel_table.get_channels()
        if not any(ch.enabled for ch in channels):
            self.show_error("Bitte mindestens einen aktiven Kanal konfigurieren.")
            return

        name = self._name_edit.text().strip()
        if not name:
            self.show_error("Bitte einen Namen für die Messung angeben.")
            return

        sample_rate = self._sample_rate_spin.value()
        ring_buffer_size = self._calculate_dynamic_buffer_size(
            sample_rate, len([ch for ch in channels if ch.enabled])
        )
        samples_per_read = self._calculate_samples_per_read(sample_rate)

        config = MeasurementConfig(
            name=name,
            sample_rate_hz=sample_rate,
            channels=channels,
            storage_format=StorageFormat(self._storage_format_combo.currentText()),
            samples_per_read=samples_per_read,
            ring_buffer_size=ring_buffer_size,
            save_to_disk=not self._live_only_checkbox.isChecked(),
        )
        self.start_measurement_requested.emit(config)

    def _calculate_samples_per_read(self, sample_rate_hz: float) -> int:
        """Berechnet eine adaptive Blockgroesse pro DAQ-Read.

        Kleinere Bloecke reduzieren die wahrgenommene Hakelei der Live View,
        weil neue Daten haeufiger im Ring Buffer landen.
        """
        target = int(sample_rate_hz * (self._target_read_block_ms / 1000.0))
        return max(self._min_samples_per_read, min(self._max_samples_per_read, target))

    def _calculate_dynamic_buffer_size(self, sample_rate_hz: float, num_active_channels: int) -> int:
        """Berechnet die Puffergröße dynamisch basierend auf verfügbarem RAM.

        Nutzt ~10% des verfügbaren RAM für den Ring Buffer, gedeckelt auf
        120s. Bei sehr wenig freiem RAM wird die sonst übliche
        10s-Mindestgröße bewusst unterschritten (mit Warnung), statt die
        RAM-Grenze zu überschreiten - ein fester Mindest-Puffer würde sonst
        bei knappem Speicher zu einem MemoryError beim Messstart führen.
        """
        try:
            import psutil
            available_ram_bytes = psutil.virtual_memory().available
            bytes_per_sample = 8.0 * num_active_channels  # float64 pro Kanal
            max_duration_from_ram = (available_ram_bytes * 0.1) / bytes_per_sample / sample_rate_hz

            duration_seconds = min(120.0, max_duration_from_ram)
            if duration_seconds < 10.0:
                logger.warning(
                    "Wenig freier Arbeitsspeicher (%.0f MB verfügbar): Ring Buffer "
                    "wird auf %.1f s begrenzt statt der üblichen Mindestgröße von 10 s.",
                    available_ram_bytes / (1024 ** 2),
                    duration_seconds,
                )
            return max(1, int(sample_rate_hz * duration_seconds))
        except Exception:
            # Fallback auf statische Größe bei Fehler
            logger.debug("Fehler bei dynamischer RAM-Berechnung, nutze Fallback")
            return int(sample_rate_hz * self._default_ring_buffer_seconds)

    def _on_device_selected(self, row: int) -> None:
        """Handler, der beim Wechsel der Geräteauswahl die verfügbaren
        Hardware-Kanäle an die ChannelTableWidget übergibt.
        """
        if row < 0 or row >= len(self._discovered_devices):
            self._channel_table.set_available_hw_channels([])
            return
        device = self._discovered_devices[row]
        if getattr(device, "physical_channels", None):
            hw_list = device.physical_channels
        else:
            # Fallback: generiere kanalnamen anhand der Anzahl
            hw_list = [f"{device.device_name}/ai{i}" for i in range(device.num_channels)]
        self._channel_table.set_available_hw_channels(hw_list)
