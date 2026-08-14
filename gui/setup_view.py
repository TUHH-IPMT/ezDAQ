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
from dataclasses import dataclass

from PyQt6.QtCore import QLocale, QSize, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon
from PyQt6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QCheckBox,
    QScrollArea,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config.configuration_manager import ConfigurationManager
from config.sensor_database import SensorDatabaseManager
from data.models import Channel, DeviceInfo, MeasurementConfig, RecordingStopUnit, StorageFormat
from gui.i18n import connect_language_changed, t
from gui.theme import connect_theme_changed, draw_play_icon
from gui.widgets.channel_table import ChannelTableWidget

logger = logging.getLogger(__name__)

# Übersetzte Anzeige-Labels für die Speicherformat-Combobox. Der
# eigentliche Wert (StorageFormat.value, z. B. "parquet") bleibt
# unabhängig von der UI-Sprache stabil (Persistenz) und wird als
# `userData` je Eintrag hinterlegt, siehe `_populate_storage_format_combo`.
_STORAGE_FORMAT_LABEL_KEYS: dict[StorageFormat, str] = {
    StorageFormat.PARQUET: "storage_format_parquet",
    StorageFormat.CSV: "storage_format_csv",
}

# Übersetzte Anzeige-Labels für die Einheiten-Combobox des Aufnahme-Limits
# (siehe `_populate_recording_stop_unit_combo`) - analog zu
# `_STORAGE_FORMAT_LABEL_KEYS`.
_RECORDING_STOP_UNIT_LABEL_KEYS: dict[RecordingStopUnit, str] = {
    RecordingStopUnit.SAMPLES: "recording_stop_unit_samples",
    RecordingStopUnit.SECONDS: "recording_stop_unit_seconds",
    RecordingStopUnit.MINUTES: "recording_stop_unit_minutes",
    RecordingStopUnit.HOURS: "recording_stop_unit_hours",
}


@dataclass
class NamingScheme:
    """Steuert, wie `gui/main_window.py` aus dem eingegebenen Messnamen
    den tatsächlich verwendeten Datei-/Messnamen aufbaut.

    Attributes:
        use_number_suffix: Ob ein Nummernsuffix (z. B. "_001") angehängt
            wird, um Namenskonflikte automatisch aufzulösen.
        number_suffix_digits: Stellenzahl des Nummernsuffix.
        include_date: Ob das aktuelle Datum (YYYYMMDD) angehängt wird.
        include_time: Ob die aktuelle Uhrzeit (HHMMSS) angehängt wird.
    """

    use_number_suffix: bool
    number_suffix_digits: int
    include_date: bool
    include_time: bool


class SetupView(QWidget):
    """Ansicht zur Konfiguration von Hardware, Kanälen und Messparametern.

    Signals:
        discover_hardware_requested: Nutzer möchte angeschlossene Geräte
            erkennen lassen. `gui/main_window.py` ruft daraufhin
            `controller.discover_hardware()` auf und liefert das Ergebnis
            über `set_discovered_devices()` zurück.
        open_ni_max_requested: Nutzer möchte NI-MAX (Measurement &
            Automation Explorer) als separates Programm öffnen - z. B. um
            ein Gerät umzubenennen, ohne diese Anwendung zu verlassen.
        start_measurement_requested: Nutzer möchte die Messung mit der
            übergebenen `MeasurementConfig` starten.
    """

    discover_hardware_requested = pyqtSignal()
    open_ni_max_requested = pyqtSignal()
    start_measurement_requested = pyqtSignal(object)  # MeasurementConfig
    storage_path_requested = pyqtSignal()

    def __init__(
        self,
        configuration_manager: ConfigurationManager,
        sensor_database: SensorDatabaseManager | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._configuration_manager = configuration_manager
        self._discovered_devices: list[DeviceInfo] = []
        self._storage_path_is_set = False
        self._status_reason_key = ""
        self._discovery_in_progress = False

        # Die gesamte Ansicht steckt in einem QScrollArea: bei vielen
        # Abschnitten (Geräte, Kanäle, Messeinstellungen, Speicher) reicht
        # die Fensterhöhe oft nicht für alle Abschnitte gleichzeitig - ohne
        # Scroll-Bereich würde Qt stattdessen ALLE Abschnitte gleichmäßig
        # zusammenquetschen (insbesondere die Kanaltabelle bis auf eine
        # einzelne, kaum nutzbare Zeile). Mit Scroll-Bereich behalten alle
        # Abschnitte ihre bevorzugte/Mindestgröße, überschüssiger Inhalt
        # wird gescrollt statt gequetscht.
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        outer_layout.addWidget(scroll_area)

        content = QWidget()
        scroll_area.setWidget(content)
        layout = QVBoxLayout(content)

        # --- Geräteerkennung ---
        self._device_header = QLabel(t("connected_devices"))
        layout.addWidget(self._device_header)
        self._device_group = QGroupBox()
        device_layout = QVBoxLayout(self._device_group)
        discover_row = QHBoxLayout()
        self._discover_button = QPushButton(t("search_devices"))
        self._discover_button.clicked.connect(self.discover_hardware_requested.emit)
        # Schnellzugriff auf NI-MAX (Measurement & Automation Explorer) -
        # z. B. um ein Gerät umzubenennen/zu konfigurieren, ohne diese
        # Anwendung zu verlassen (siehe `open_ni_max_requested`).
        self._open_ni_max_button = QPushButton(t("open_ni_max_button"))
        self._open_ni_max_button.clicked.connect(self.open_ni_max_requested.emit)
        discover_row.addWidget(self._discover_button)
        discover_row.addWidget(self._open_ni_max_button)
        # Baumansicht Gerät -> Kanäle (dieselbe Gruppierung wie im
        # Kanal-Zuweisungsdialog, siehe
        # `gui/widgets/channel_table.py::HardwareChannelPickerDialog`).
        self._device_list = QTreeWidget()
        self._device_list.setHeaderHidden(True)
        # Mindesthöhe für ein paar sichtbare Zeilen, ohne dass eine leere
        # Liste (vor der ersten Geräteerkennung) unnötig viel Platz belegt.
        self._device_list.setMinimumHeight(120)
        device_layout.addLayout(discover_row)
        device_layout.addWidget(self._device_list)
        layout.addWidget(self._device_group)

        # --- Kanalkonfiguration ---
        self._channel_header = QLabel(t("channel_configuration"))
        layout.addWidget(self._channel_header)
        self._channel_group = QGroupBox()
        channel_layout = QVBoxLayout(self._channel_group)
        self._channel_table = ChannelTableWidget(sensor_database)
        channel_layout.addWidget(self._channel_table)
        layout.addWidget(self._channel_group, stretch=1)

        # --- Messeinstellungen ---
        self._measurement_header = QLabel(t("measurement_settings"))
        layout.addWidget(self._measurement_header)
        self._measurement_group = QGroupBox()
        self._measurement_layout = QFormLayout(self._measurement_group)

        self._sample_rate_spin = QDoubleSpinBox()
        self._sample_rate_spin.setRange(1.0, 100_000.0)
        self._sample_rate_spin.setDecimals(1)
        self._sample_rate_spin.setSingleStep(100.0)
        # Tausenderpunkt in der Anzeige (z. B. "100.000,0"), unabhängig vom
        # Locale des Betriebssystems.
        self._sample_rate_spin.setLocale(QLocale(QLocale.Language.German, QLocale.Country.Germany))
        self._sample_rate_spin.setGroupSeparatorShown(True)
        self._sample_rate_spin.setValue(
            configuration_manager.settings.default_sample_rate_hz
        )
        self._measurement_layout.addRow(f"{t('sample_rate_hz')}:", self._sample_rate_spin)

        measurement_row = QHBoxLayout()
        measurement_row.addWidget(self._measurement_group, stretch=1)
        measurement_row.addStretch(1)
        layout.addLayout(measurement_row)

        # Interne Performance-Parameter werden automatisch festgelegt,
        # damit der Nutzer hier nicht mit technischen Details belastet wird.
        # Ziel: kleinere Read-Bloecke fuer fluessigere Live-Updates. NICHT
        # weiter als 25ms verkleinern: ein Test mit 10ms fuehrte bei hoher
        # Abtastrate zu "The application is not able to keep up with the
        # hardware acquisition" (Pufferueberlauf/Datenverlust) - der reine
        # Python/ctypes-Aufrufoverhead pro `device.read()` dominiert dann
        # gegenueber dem eigentlichen Datentransfer und der DAQ-Thread
        # selbst kommt nicht mehr hinterher.
        self._target_read_block_ms = 25.0
        self._min_samples_per_read = 50
        self._max_samples_per_read = 2000
        self._default_ring_buffer_seconds = 30
        # Obergrenze der BLOCKDAUER in Sekunden (zusaetzlich zur
        # Sample-Untergrenze oben): `AcquisitionThread.stop()` (siehe
        # core/acquisition.py) wartet beim Stoppen auf den GERADE
        # laufenden, blockierenden `device.read()`-Aufruf - bei niedriger
        # Abtastrate wuerde `_min_samples_per_read` sonst einen einzelnen
        # Block mehrere Sekunden dauern lassen (z. B. 50 Samples bei 15 Hz
        # = 3,3 s) und sowohl den manuellen Stopp-Button als auch ein
        # konfiguriertes Aufnahme-Limit (siehe
        # `data/models.py::MeasurementConfig.recording_unlimited`)
        # entsprechend verzoegern. Bei den ueblichen Abtastraten
        # (>= 100 Hz) greift diese Grenze nicht (50 Samples sind dann
        # laengst unter 0.5s), aendert dort also nichts.
        self._max_read_block_seconds = 0.5

        # --- Speichereinstellungen ---
        self._storage_header = QLabel(t("storage_settings"))
        layout.addWidget(self._storage_header)
        self._storage_group = QGroupBox()
        self._storage_layout = QFormLayout(self._storage_group)

        self._live_only_checkbox = QCheckBox(t("live_only"))
        self._live_only_checkbox.setChecked(configuration_manager.settings.last_live_only)
        self._storage_layout.addRow("", self._live_only_checkbox)

        self._name_edit = QLineEdit(
            configuration_manager.settings.last_measurement_name or "Messung"
        )
        self._storage_layout.addRow(f"{t('measurement_name')}:", self._name_edit)

        naming_row = QHBoxLayout()
        settings = configuration_manager.settings
        self._naming_number_checkbox = QCheckBox(t("naming_number_suffix"))
        self._naming_number_checkbox.setChecked(settings.name_use_number_suffix)
        self._naming_digits_label = QLabel(f"{t('naming_digits')}:")
        self._naming_digits_spin = QSpinBox()
        self._naming_digits_spin.setRange(1, 6)
        self._naming_digits_spin.setValue(settings.name_number_suffix_digits)
        self._naming_digits_spin.setEnabled(settings.name_use_number_suffix)
        self._naming_date_checkbox = QCheckBox(t("naming_include_date"))
        self._naming_date_checkbox.setChecked(settings.name_include_date)
        self._naming_time_checkbox = QCheckBox(t("naming_include_time"))
        self._naming_time_checkbox.setChecked(settings.name_include_time)

        naming_row.addWidget(self._naming_number_checkbox)
        naming_row.addWidget(self._naming_digits_label)
        naming_row.addWidget(self._naming_digits_spin)
        naming_row.addWidget(self._naming_date_checkbox)
        naming_row.addWidget(self._naming_time_checkbox)
        naming_row.addStretch(1)
        self._naming_row_label = QLabel(f"{t('naming_scheme')}:")
        self._storage_layout.addRow(self._naming_row_label, naming_row)

        self._naming_number_checkbox.toggled.connect(self._on_naming_scheme_changed)
        self._naming_digits_spin.valueChanged.connect(self._on_naming_scheme_changed)
        self._naming_date_checkbox.toggled.connect(self._on_naming_scheme_changed)
        self._naming_time_checkbox.toggled.connect(self._on_naming_scheme_changed)

        self._storage_format_combo = QComboBox()
        self._populate_storage_format_combo(configuration_manager.settings.default_storage_format)
        self._storage_layout.addRow(f"{t('storage_format')}:", self._storage_format_combo)

        # Aufnahme-Limit ("Messzyklus"): standardmäßig unbegrenzt (heutiges
        # Verhalten - laufen, bis manuell gestoppt oder die Festplatte voll
        # ist). Deaktiviert man den Haken, kann ein Grenzwert (Messwerte
        # oder Zeit) eingegeben werden, bei dessen Erreichen die Messung
        # automatisch stoppt (siehe `gui/live_view.py::_on_timer_tick`).
        self._recording_unlimited_checkbox = QCheckBox(t("recording_unlimited"))
        self._recording_unlimited_checkbox.setChecked(
            configuration_manager.settings.last_recording_unlimited
        )
        self._recording_unlimited_checkbox.toggled.connect(self._on_recording_unlimited_toggled)
        self._storage_layout.addRow("", self._recording_unlimited_checkbox)

        recording_limit_row = QHBoxLayout()
        self._recording_stop_spin = QSpinBox()
        self._recording_stop_spin.setRange(1, 1_000_000_000)
        self._recording_stop_spin.setValue(
            max(1, int(configuration_manager.settings.last_recording_stop_value))
        )
        self._recording_stop_unit_combo = QComboBox()
        self._populate_recording_stop_unit_combo(
            configuration_manager.settings.last_recording_stop_unit
        )
        recording_limit_row.addWidget(self._recording_stop_spin)
        recording_limit_row.addWidget(self._recording_stop_unit_combo)
        recording_limit_row.addStretch(1)
        self._recording_limit_row_label = QLabel(f"{t('recording_limit_label')}:")
        self._storage_layout.addRow(self._recording_limit_row_label, recording_limit_row)
        self._on_recording_unlimited_toggled(self._recording_unlimited_checkbox.isChecked())

        self._storage_path_label = QLabel(t("no_storage_location"))
        self._storage_button = QPushButton(t("choose_storage_location"))
        self._storage_button.clicked.connect(self.storage_path_requested.emit)
        self._storage_layout.addRow(f"{t('storage_location')}:", self._storage_path_label)
        self._storage_layout.addRow("", self._storage_button)

        storage_row = QHBoxLayout()
        storage_row.addWidget(self._storage_group, stretch=1)
        storage_row.addStretch(1)
        layout.addLayout(storage_row)

        # --- Start ---
        start_row = QHBoxLayout()
        self._status_label = QLabel("")
        self._start_button = QPushButton()
        self._set_start_button_text()
        self._start_button.setIconSize(QSize(18, 18))
        self._retheme_start_button_icon()
        self._start_button.setStyleSheet(
            "QPushButton { background-color: #1f7a36; color: #f9fafb; border: none; padding: 6px 16px; border-radius: 4px; font-weight: 700; font-size: 11pt; }"
            "QPushButton:hover { background-color: #1a662e; }"
            "QPushButton:pressed { background-color: #145125; }"
        )
        self._start_button.clicked.connect(self._on_start_clicked)
        start_row.addWidget(self._start_button)
        start_row.addWidget(self._status_label, stretch=1)
        layout.addLayout(start_row)

        self._apply_section_header_emphasis()

        # Zuletzt verwendete Kanalkonfiguration automatisch vorschlagen.
        last_channels = configuration_manager.load_channel_configuration()
        if last_channels:
            self._channel_table.set_channels(last_channels)

        connect_language_changed(self.retranslate_ui)
        connect_theme_changed(self._retheme_start_button_icon)

    # ------------------------------------------------------------------ #
    # Öffentliche API (von main_window.py aufgerufen)
    # ------------------------------------------------------------------ #

    def retranslate_ui(self) -> None:
        """Aktualisiert alle statischen Texte nach einem Sprachwechsel."""
        self._device_header.setText(t("connected_devices"))
        self._discover_button.setText(
            t("searching_devices") if self._discovery_in_progress else t("search_devices")
        )
        self._open_ni_max_button.setText(t("open_ni_max_button"))
        self._channel_header.setText(t("channel_configuration"))
        self._measurement_header.setText(t("measurement_settings"))
        self._storage_header.setText(t("storage_settings"))

        for layout_, key, widget in (
            (self._measurement_layout, "sample_rate_hz", self._sample_rate_spin),
            (self._storage_layout, "measurement_name", self._name_edit),
            (self._storage_layout, "storage_format", self._storage_format_combo),
            (self._storage_layout, "storage_location", self._storage_path_label),
        ):
            label = layout_.labelForField(widget)
            if label is not None:
                label.setText(f"{t(key)}:")

        self._live_only_checkbox.setText(t("live_only"))
        self._storage_button.setText(t("choose_storage_location"))
        if not self._storage_path_is_set:
            self._storage_path_label.setText(t("no_storage_location"))
        self._set_start_button_text()
        self._status_label.setText(t(self._status_reason_key))
        self._populate_storage_format_combo(self._storage_format_combo.currentData())

        self._recording_unlimited_checkbox.setText(t("recording_unlimited"))
        self._recording_limit_row_label.setText(f"{t('recording_limit_label')}:")
        self._populate_recording_stop_unit_combo(self._recording_stop_unit_combo.currentData())

        self._naming_row_label.setText(f"{t('naming_scheme')}:")
        self._naming_number_checkbox.setText(t("naming_number_suffix"))
        self._naming_digits_label.setText(f"{t('naming_digits')}:")
        self._naming_date_checkbox.setText(t("naming_include_date"))
        self._naming_time_checkbox.setText(t("naming_include_time"))

        self._channel_table.retranslate_ui()

    def get_naming_scheme(self) -> NamingScheme:
        """Gibt das aktuell in der UI eingestellte Namensschema zurück.

        Wird von `gui/main_window.py` beim Messstart verwendet, um aus dem
        Messnamen den tatsächlichen Datei-/Messnamen aufzubauen.
        """
        return NamingScheme(
            use_number_suffix=self._naming_number_checkbox.isChecked(),
            number_suffix_digits=self._naming_digits_spin.value(),
            include_date=self._naming_date_checkbox.isChecked(),
            include_time=self._naming_time_checkbox.isChecked(),
        )

    def _populate_storage_format_combo(self, selected_value: str) -> None:
        """Befüllt die Speicherformat-Combobox mit übersetzten Labels.

        Der technische Wert (z. B. "parquet") wird als `userData` je
        Eintrag hinterlegt und bleibt damit unabhängig von der
        UI-Sprache abrufbar (siehe `build_current_config`/
        `get_current_measurement_parameters`).
        """
        self._storage_format_combo.blockSignals(True)
        self._storage_format_combo.clear()
        for storage_format in StorageFormat:
            self._storage_format_combo.addItem(
                t(_STORAGE_FORMAT_LABEL_KEYS[storage_format]), storage_format.value
            )
        index = self._storage_format_combo.findData(selected_value)
        self._storage_format_combo.setCurrentIndex(index if index >= 0 else 0)
        self._storage_format_combo.blockSignals(False)

    def _populate_recording_stop_unit_combo(self, selected_value: str) -> None:
        """Befüllt die Einheiten-Combobox des Aufnahme-Limits mit übersetzten
        Labels - analog zu `_populate_storage_format_combo`."""
        self._recording_stop_unit_combo.blockSignals(True)
        self._recording_stop_unit_combo.clear()
        for unit in RecordingStopUnit:
            self._recording_stop_unit_combo.addItem(
                t(_RECORDING_STOP_UNIT_LABEL_KEYS[unit]), unit.value
            )
        index = self._recording_stop_unit_combo.findData(selected_value)
        self._recording_stop_unit_combo.setCurrentIndex(index if index >= 0 else 0)
        self._recording_stop_unit_combo.blockSignals(False)

    def set_discovery_in_progress(self, in_progress: bool) -> None:
        """Sperrt/entsperrt den "Geräte suchen"-Button während eine
        Geräteerkennung im Hintergrund läuft (siehe
        `gui/main_window.py::_on_discover_hardware`).

        Läuft in einem `BackgroundWorker` (siehe `gui/workers.py`), damit
        `nidaqmx.system.System.local()` - bei mehreren Chassis/Modulen
        oder Treiber-Timeouts spürbar langsam - nicht mehr den GUI-Thread
        blockiert. Diese Methode gibt dem Nutzer währenddessen sichtbares
        Feedback und verhindert eine doppelt gestartete Anfrage.
        """
        self._discovery_in_progress = in_progress
        self._discover_button.setEnabled(not in_progress)
        self._discover_button.setText(
            t("searching_devices") if in_progress else t("search_devices")
        )

    def get_discovered_devices(self) -> list[DeviceInfo]:
        """Gibt die zuletzt erkannten Geräte zurück (siehe
        `set_discovered_devices`).

        Wird von `gui/main_window.py` beim Messstart an
        `MeasurementController.start_measurement()` durchgereicht, damit
        dort NICHT erneut `discover_hardware()` aufgerufen werden muss -
        bei mehreren Chassis/Modulen laut `_on_discover_hardware`
        spürbar langsam und würde sonst bei JEDEM Messstart erneut den
        GUI-Thread blockieren, obwohl das Ergebnis (Kanalzuordnung kommt
        ohnehin aus der Kanalkonfiguration selbst, siehe
        `core/measurement.py::create_devices`) hier nur für kosmetische
        Metadaten (`DeviceInfo.product_type`) gebraucht wird.
        """
        return self._discovered_devices

    def set_discovered_devices(self, devices: list[DeviceInfo]) -> None:
        """Zeigt das Ergebnis einer Geräteerkennung an.

        Reicht ALLE erkannten Geräte (nicht nur ein in der Liste
        ausgewähltes) an die Kanaltabelle weiter - welche Hardwarekanäle
        wählbar sind und welches Modul zu welchem Kanal gehört, ist durch
        die tatsächlich angeschlossene Hardware vorgegeben (siehe
        `gui/widgets/channel_table.py::set_available_devices`).
        """
        self._device_list.clear()
        devices_with_channels = [d for d in devices if d.num_channels > 0]
        self._discovered_devices = devices_with_channels
        self._channel_table.set_available_devices(devices_with_channels)
        if not devices_with_channels:
            self._device_list.addTopLevelItem(QTreeWidgetItem([t("no_devices_found")]))
            return
        for device in devices_with_channels:
            module_info = f" [{device.module_type.value}]" if device.module_type else ""
            device_item = QTreeWidgetItem(
                [
                    f"{device.device_name} - {device.product_type}{module_info} "
                    f"({t('device_channel_count', count=device.num_channels)})"
                ]
            )
            channels = device.physical_channels or [
                f"{device.device_name}/ai{i}" for i in range(device.num_channels)
            ]
            for channel in channels:
                device_item.addChild(QTreeWidgetItem([channel]))
            self._device_list.addTopLevelItem(device_item)
        # Standardmäßig eingeklappt (nur Gerätenamen sichtbar) - spart bei
        # mehreren Modulen mit jeweils vielen Kanälen deutlich Platz. Der
        # Nutzer klappt ein Gerät bei Bedarf einzeln auf.
        self._device_list.collapseAll()

    def show_discovery_error(self, message: str) -> None:
        """Zeigt einen fehlgeschlagenen Geräteerkennungsversuch (z. B.
        NI-DAQmx-Treiber nicht installiert) direkt im Gerätebrowser an,
        statt nur im Log - der Nutzer sieht die Ursache damit genau dort,
        wo er als nächstes hinschaut (siehe
        `gui/main_window.py::_on_discover_hardware_failed`, ruft dies
        anstelle von `set_discovered_devices` auf).
        """
        self._device_list.clear()
        self._discovered_devices = []
        self._channel_table.set_available_devices([])
        self._device_list.addTopLevelItem(
            QTreeWidgetItem([f"{t('device_discovery_failed')}: {message}"])
        )

    def set_start_enabled(self, enabled: bool, reason: str = "") -> None:
        """Aktiviert/deaktiviert den Start-Button (z. B. während eine Messung läuft).

        `reason` ist ein i18n-Key (kein fertiger Text), damit der Grund
        einen Sprachwechsel übersteht (siehe `retranslate_ui`).
        """
        self._start_button.setEnabled(enabled)
        self._status_reason_key = reason
        self._status_label.setText(t(reason))

    def set_storage_path(self, path: str | None) -> None:
        self._storage_path_is_set = bool(path)
        self._storage_path_label.setText(path or t("no_storage_location"))

    def show_error(self, message: str) -> None:
        """Zeigt eine Fehlermeldung an (z. B. ungültige Konfiguration)."""
        QMessageBox.warning(self, t("error"), message)

    def get_current_measurement_parameters(
        self,
    ) -> tuple[str, float, str, bool, bool, float, str]:
        """Gibt die aktuell im UI eingestellten Messparameter zurück.

        Returns:
            (measurement_name, sample_rate_hz, storage_format, live_only,
            recording_unlimited, recording_stop_value, recording_stop_unit)
        """
        return (
            self._name_edit.text().strip() or "Messung",
            self._sample_rate_spin.value(),
            self._storage_format_combo.currentData(),
            self._live_only_checkbox.isChecked(),
            self._recording_unlimited_checkbox.isChecked(),
            float(self._recording_stop_spin.value()),
            self._recording_stop_unit_combo.currentData(),
        )

    def get_configured_channels(self) -> list[Channel]:
        """Gibt die aktuell in der Kanaltabelle konfigurierten Kanäle zurück.

        Anders als `build_current_config()`: keine Validierung, keine
        Fehlermeldung, funktioniert auch OHNE laufende Messung. Wird z. B.
        von `gui/main_window.py` für den Kanal-Darstellung-Dialog genutzt,
        der schon vor dem Messstart nutzbar sein soll (die Live View kennt
        ihre Kanäle sonst erst, sobald eine Messung tatsächlich läuft).
        """
        return self._channel_table.get_channels()

    def apply_channel_display_settings(self, settings: dict[str, dict]) -> None:
        """Übernimmt vom "Kanal-Darstellung"-Dialog (siehe
        `gui/live_view.py::ChannelDisplayDialog`) gesetzte Werte in die
        Kanaltabelle, damit sie beim Speichern der Konfiguration erhalten
        bleiben (siehe `ChannelTableWidget.apply_display_settings`)."""
        self._channel_table.apply_display_settings(settings)

    def build_current_config(self) -> MeasurementConfig | None:
        """Baut eine MeasurementConfig aus den aktuellen UI-Eingaben.

        Wird sowohl von `_on_start_clicked` als auch von
        `gui/main_window.py` für "Konfiguration speichern" verwendet.
        Zeigt bei unvollständigen Eingaben eine Fehlermeldung und gibt
        None zurück (kein aktiver Kanal, kein Name, aktiver Kanal ohne
        zugewiesenen Hardwarekanal).
        """
        channels = self._channel_table.get_channels()
        if not any(ch.enabled for ch in channels):
            self.show_error(t("error_no_active_channels"))
            return None

        # Ohne diese Prüfung würde ein aktivierter, aber noch nicht
        # zugewiesener Kanal (hardware_channel == "") erst tief in der
        # Hardware-Schicht als kryptischer "ungültiger Kanalname"-Fehler
        # auftauchen (nidaqmx lehnt eine leere Kanalzeichenkette ab) -
        # hier lässt sich die eigentliche Ursache klar benennen.
        unassigned = [ch for ch in channels if ch.enabled and not ch.hardware_channel.strip()]
        if unassigned:
            names = ", ".join(ch.display_name for ch in unassigned)
            self.show_error(t("error_channel_missing_hw_channel", names=names))
            return None

        name = self._name_edit.text().strip()
        if not name:
            self.show_error(t("error_no_name"))
            return None

        sample_rate = self._sample_rate_spin.value()
        ring_buffer_size = self._calculate_dynamic_buffer_size(
            sample_rate, len([ch for ch in channels if ch.enabled])
        )
        samples_per_read = self._calculate_samples_per_read(sample_rate)

        return MeasurementConfig(
            name=name,
            sample_rate_hz=sample_rate,
            channels=channels,
            storage_format=StorageFormat(self._storage_format_combo.currentData()),
            samples_per_read=samples_per_read,
            ring_buffer_size=ring_buffer_size,
            save_to_disk=not self._live_only_checkbox.isChecked(),
            recording_unlimited=self._recording_unlimited_checkbox.isChecked(),
            recording_stop_value=float(self._recording_stop_spin.value()),
            recording_stop_unit=RecordingStopUnit(self._recording_stop_unit_combo.currentData()),
        )

    def apply_config(self, config: MeasurementConfig) -> None:
        """Überträgt eine geladene Konfiguration in die UI-Felder.

        Wird von `gui/main_window.py` nach "Konfiguration laden" aufgerufen.
        `samples_per_read`/`ring_buffer_size` werden bewusst NICHT
        übernommen: Sie sind keine editierbaren UI-Felder und werden beim
        Start immer frisch aus Abtastrate/Kanalanzahl/verfügbarem RAM neu
        berechnet (siehe `build_current_config`/`_calculate_dynamic_buffer_size`) -
        identisch zum Verhalten bei manuell eingegebener Konfiguration.
        """
        self._name_edit.setText(config.name)
        self._sample_rate_spin.setValue(config.sample_rate_hz)
        self._populate_storage_format_combo(config.storage_format.value)
        self._live_only_checkbox.setChecked(not config.save_to_disk)
        self._recording_unlimited_checkbox.setChecked(config.recording_unlimited)
        self._recording_stop_spin.setValue(max(1, int(config.recording_stop_value)))
        self._populate_recording_stop_unit_combo(config.recording_stop_unit.value)
        self._on_recording_unlimited_toggled(self._recording_unlimited_checkbox.isChecked())
        self._channel_table.set_channels(config.channels)

    # ------------------------------------------------------------------ #
    # Interna
    # ------------------------------------------------------------------ #

    def _on_naming_scheme_changed(self) -> None:
        """Persistiert das Namensschema sofort bei jeder Änderung."""
        self._naming_digits_spin.setEnabled(self._naming_number_checkbox.isChecked())
        scheme = self.get_naming_scheme()
        self._configuration_manager.update_naming_scheme(
            use_number_suffix=scheme.use_number_suffix,
            number_suffix_digits=scheme.number_suffix_digits,
            include_date=scheme.include_date,
            include_time=scheme.include_time,
        )

    def _on_recording_unlimited_toggled(self, checked: bool) -> None:
        """Graut Wert/Einheit des Aufnahme-Limits aus, solange "Unbegrenzt"
        aktiv ist - reines UI-Feedback, keine sofortige Persistierung (wie
        beim "Nur Live anzeigen"-Haken wird der Wert erst beim Start/
        Schließen über `update_last_measurement_parameters` gespeichert,
        siehe `get_current_measurement_parameters`)."""
        enabled = not checked
        self._recording_stop_spin.setEnabled(enabled)
        self._recording_stop_unit_combo.setEnabled(enabled)

    def _on_start_clicked(self) -> None:
        config = self.build_current_config()
        if config is None:
            return
        self.start_measurement_requested.emit(config)

    def _retheme_start_button_icon(self) -> None:
        # Fester Hintergrund (Dunkelgruen, siehe Stylesheet oben) unabhaengig
        # vom Theme - das Icon braucht daher IMMER Weiss statt der sonst
        # theme-abhaengigen nav_icon_color() (waere im Hell-Modus schwarz
        # und auf dem dunkelgruenen Grund kaum zu erkennen).
        self._start_button.setIcon(
            QIcon(draw_play_icon(20, y_offset=0.6, color=QColor(255, 255, 255)))
        )

    def _set_start_button_text(self) -> None:
        # Fuehrende Leerzeichen vergroessern den Abstand zwischen Icon und Text.
        self._start_button.setText(f"  {t('start_measurement')}")

    def _apply_section_header_emphasis(self) -> None:
        """Hebt nur Abschnitts-Labels hervor und bleibt vollständig theme-sicher."""
        header_font = QFont(self.font())
        if header_font.pointSize() > 0:
            header_font.setPointSize(header_font.pointSize() + 2)
        header_font.setBold(True)

        for header in (
            self._device_header,
            self._channel_header,
            self._measurement_header,
            self._storage_header,
        ):
            header.setFont(header_font)
            margins = header.contentsMargins()
            header.setContentsMargins(
                margins.left(),
                8,
                margins.right(),
                4,
            )

    def _calculate_samples_per_read(self, sample_rate_hz: float) -> int:
        """Berechnet eine adaptive Blockgroesse pro DAQ-Read.

        Kleinere Bloecke reduzieren die wahrgenommene Hakelei der Live View,
        weil neue Daten haeufiger im Ring Buffer landen. Zusaetzlich per
        `_max_read_block_seconds` nach oben in der BLOCKDAUER begrenzt -
        siehe dessen Kommentar in `__init__` fuer den Grund (Stopp-Latenz
        bei niedrigen Abtastraten).
        """
        target = int(sample_rate_hz * (self._target_read_block_ms / 1000.0))
        samples = max(self._min_samples_per_read, min(self._max_samples_per_read, target))
        max_by_duration = max(1, int(sample_rate_hz * self._max_read_block_seconds))
        return min(samples, max_by_duration)

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

