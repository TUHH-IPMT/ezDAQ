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
from data.models import (
    Channel,
    DeviceInfo,
    MeasurementConfig,
    ModuleType,
    RecordingStopUnit,
    StorageFormat,
    is_valid_grid_sample_rate,
    is_valid_ni9234_sample_rate,
    max_ni9213_sample_rate_hz,
    next_grid_sample_rate_at_or_above,
    next_ni9234_sample_rate_at_or_above,
    ni9213_device_groups,
    resolve_rate_groups,
)
from gui.i18n import connect_language_changed, t
from gui.theme import (
    PLAY_ICON_COLOR,
    RECORD_ICON_COLOR,
    action_button_style,
    connect_theme_changed,
    draw_play_icon,
    draw_record_icon,
    draw_stop_icon,
    draw_trigger_icon,
    fix_toggle_button_width,
    repolish,
    trigger_arm_button_style,
)
from gui.widgets.channel_table import ChannelTableWidget
from gui.widgets.spinbox import (
    GroupedDoubleSpinBox,
    NoWheelSpinBox,
    PrecisionDoubleSpinBox,
)

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

# Übersetzte Anzeige-Labels je ADC-Timing-Modus für die Fehlermeldung bei
# zu hoher Abtastrate (siehe `build_current_config`) - eigene, bewusst
# kleine Kopie von `gui/widgets/channel_table.py::_ADC_TIMING_MODE_LABEL_KEYS`
# statt eines Imports des dortigen privaten (`_`-präfigierten) Dicts.
_ADC_TIMING_MODE_LABEL_KEYS: dict[str, str] = {
    "HIGH_RESOLUTION": "adc_timing_mode_high_resolution",
    "HIGH_SPEED": "adc_timing_mode_high_speed",
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
            übergebenen `MeasurementConfig` starten (Play- ODER
            Aufnahme-Button - `MeasurementConfig.save_to_disk` ist dabei
            schon passend gesetzt, siehe `_on_play_clicked`/
            `_on_record_clicked`).
        stop_requested: Nutzer möchte die laufende Messung stoppen (nur
            klickbar, waehrend tatsaechlich eine läuft, siehe
            `set_start_enabled`).
    """

    discover_hardware_requested = pyqtSignal()
    open_ni_max_requested = pyqtSignal()
    start_measurement_requested = pyqtSignal(object)  # MeasurementConfig
    stop_requested = pyqtSignal()
    storage_path_requested = pyqtSignal()
    # Nutzer hat den Scharf-Button geklickt (siehe `_trigger_arm_button`) -
    # bool = neuer Zustand (True = scharf schalten, False = entschärfen).
    trigger_arm_toggled = pyqtSignal(bool)

    def __init__(
        self,
        configuration_manager: ConfigurationManager,
        sensor_database: SensorDatabaseManager | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._configuration_manager = configuration_manager
        # None bedeutet: seit dem letzten Reset gab es noch keine
        # erfolgreiche Geräteerkennung. Eine leere Liste dagegen ist ein
        # gültiges Ergebnis einer erfolgreichen Suche ohne nutzbare Module.
        self._discovered_devices: list[DeviceInfo] | None = None
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
        outer_layout.addWidget(scroll_area, stretch=1)

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

        self._sample_rate_spin = GroupedDoubleSpinBox()
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

        # Nicht-blockierende Vorschau der tatsaechlich pro Ratengruppe
        # verwendeten Abtastrate (siehe `_update_resolved_rate_preview`) -
        # bleibt leer/unsichtbar im Regelfall (genau eine Gruppe), zeigt
        # sich erst, wenn z. B. ein NI9210 eine eigene Gruppe erzwingt.
        # Anders als bei DIAdem/NI-MAX wird die abweichende Ist-Rate hier
        # also sichtbar zurueckgemeldet statt still zu kappen.
        self._resolved_rate_preview_label = QLabel()
        self._resolved_rate_preview_label.setWordWrap(True)
        self._measurement_layout.addRow("", self._resolved_rate_preview_label)
        self._sample_rate_spin.valueChanged.connect(self._update_resolved_rate_preview)

        measurement_row = QHBoxLayout()
        measurement_row.addWidget(self._measurement_group, stretch=1)
        measurement_row.addStretch(1)
        layout.addLayout(measurement_row)

        # Interne Performance-Parameter werden automatisch festgelegt,
        # damit der Nutzer hier nicht mit technischen Details belastet wird.
        # Ziel: Read-Bloecke, die rein von der BLOCKDAUER (nicht von einer
        # festen Sample-Anzahl) abgeleitet werden, damit sich die Groesse
        # dynamisch mit der Abtastrate skaliert - siehe
        # `_calculate_samples_per_read`. NICHT weiter als 25ms verkleinern:
        # ein Test mit 10ms fuehrte bei hoher Abtastrate zu "The application
        # is not able to keep up with the hardware acquisition"
        # (Pufferueberlauf/Datenverlust) - der reine Python/ctypes-
        # Aufrufoverhead pro `device.read()` dominiert dann gegenueber dem
        # eigentlichen Datentransfer und der DAQ-Thread selbst kommt nicht
        # mehr hinterher. Eine feste Sample-Untergrenze (frueherer Ansatz)
        # wuerde bei niedriger Abtastrate dieselbe Zielblockdauer verfehlen
        # und lange, stossweise Bloecke erzeugen (z. B. 50 Samples bei
        # 14 S/s = 3,6s statt der gewollten 25ms) - deshalb bewusst NUR
        # eine Ober-, keine Untergrenze fuer die Sample-Anzahl.
        self._target_read_block_ms = 25.0
        self._max_samples_per_read = 2000
        self._default_ring_buffer_seconds = 30

        # --- Speichereinstellungen ---
        self._storage_header = QLabel(t("storage_settings"))
        layout.addWidget(self._storage_header)
        self._storage_group = QGroupBox()
        self._storage_layout = QFormLayout(self._storage_group)

        self._name_edit = QLineEdit(
            configuration_manager.settings.last_measurement_name or "Messung"
        )
        self._storage_layout.addRow(f"{t('measurement_name')}:", self._name_edit)

        naming_row = QHBoxLayout()
        settings = configuration_manager.settings
        self._naming_number_checkbox = QCheckBox(t("naming_number_suffix"))
        self._naming_number_checkbox.setChecked(settings.name_use_number_suffix)
        self._naming_digits_label = QLabel(f"{t('naming_digits')}:")
        self._naming_digits_spin = NoWheelSpinBox()
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
        self._recording_stop_spin = NoWheelSpinBox()
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
        # Drei Buttons mit Icon UND Text statt frueher einem einzelnen
        # "Start"-Button + "Nur Live-Ansicht"-Haken (siehe `_storage_layout`
        # oben): Play (gruenes Icon) startet NUR die Live-Anzeige ohne
        # Speicherung, Aufnahme (rotes Kreis-Icon) startet MIT Speicherung -
        # welcher Button geklickt wird, legt `MeasurementConfig.save_to_disk`
        # direkt fest (siehe `_on_play_clicked`/`_on_record_clicked`/
        # `build_current_config`). Stop bleibt deaktiviert, solange keine
        # der beiden Varianten laeuft (siehe `set_start_enabled`).
        # `ACTION_BUTTON_STYLE` setzt bewusst KEINEN `background-color` im
        # Normalzustand - die Buttons folgen normal der QPalette/dem
        # aktuellen Theme, nur die Play-/Aufnahme-Icon-Farbe ist fest
        # (siehe `_retheme_start_button_icons`); Hover/Press bekommen einen
        # dezenten Palette-basierten Effekt.
        start_row = QHBoxLayout()
        self._status_label = QLabel("")

        self._play_button = QPushButton()
        self._play_button.setIconSize(QSize(24, 24))
        self._play_button.setStyleSheet(action_button_style())
        self._play_button.clicked.connect(self._on_play_clicked)
        start_row.addWidget(self._play_button)

        self._record_button = QPushButton()
        self._record_button.setIconSize(QSize(24, 24))
        self._record_button.setStyleSheet(action_button_style())
        self._record_button.clicked.connect(self._on_record_clicked)
        start_row.addWidget(self._record_button)

        self._stop_button = QPushButton()
        self._stop_button.setIconSize(QSize(24, 24))
        self._stop_button.setStyleSheet(action_button_style())
        self._stop_button.setEnabled(False)
        self._stop_button.clicked.connect(self.stop_requested.emit)
        start_row.addWidget(self._stop_button)

        self._retheme_start_button_icons()
        self._update_start_button_labels()

        # Scharf-Button: aktiviert den unbeaufsichtigten Trigger-Zyklus
        # (scharf schalten -> warten -> aufzeichnen -> Stopp -> automatisch
        # wieder scharf schalten, siehe `TriggerConfig.auto_rearm` und
        # `gui/main_window.py::_on_trigger_arm_toggled`) - bleibt gedrückt,
        # bis er erneut geklickt wird ("entschärfen"), unabhängig davon wie
        # oft der Zyklus zwischenzeitlich automatisch durchläuft. Nur
        # sichtbar, wenn tatsächlich ein Trigger konfiguriert ist (siehe
        # `set_trigger_arm_available`).
        self._trigger_arm_button = QPushButton()
        self._trigger_arm_button.setCheckable(True)
        self._trigger_arm_button.setIconSize(QSize(24, 24))
        self._retheme_trigger_arm_button_icon()
        self._trigger_arm_button.setStyleSheet(trigger_arm_button_style())
        # ERST NACH Icon/Stylesheet setzen: `_set_trigger_arm_button_text()`
        # fixiert ueber `fix_toggle_button_width()` die Buttonbreite anhand
        # von `sizeHint()`, der Icon UND Stylesheet-Padding braucht, um
        # korrekt zu messen.
        self._set_trigger_arm_button_text()
        self._trigger_arm_button.setVisible(False)
        self._trigger_arm_button.toggled.connect(self._on_trigger_arm_button_toggled)
        start_row.addWidget(self._trigger_arm_button)

        start_row.addWidget(self._status_label, stretch=1)
        # BEWUSST nicht Teil von `layout` (das scrollt mit dem restlichen
        # Inhalt) - direkt in `outer_layout`, damit die Buttons IMMER
        # sichtbar UND an einer festen Position bleiben, unabhaengig vom
        # Scroll-Zustand/der Menge an Kanaelen/Einstellungen darueber. Als
        # Nebeneffekt fluchtet die Unterkante dadurch zuverlaessig mit der
        # Unterkante des Navigationsbereichs links (siehe
        # `gui/main_window.py::_build_navigation_and_workspace` - beide
        # sind Geschwister im selben `root_layout` mit gemeinsamem
        # Rand) - deckungsgleich mit `LiveView`, deren Button-Zeile aus
        # demselben Grund ganz oben (statt in einem scrollenden Bereich)
        # sitzt und so mit der Oberkante des Navigationsbereichs fluchtet.
        # Seitliche/obere Polsterung passend zu `content`s eigenem
        # Standardrand, aber KEIN unterer Rand - der wuerde die
        # Flucht-Garantie sonst wieder zunichtemachen.
        start_row.setContentsMargins(9, 8, 9, 0)
        outer_layout.addLayout(start_row)

        self._apply_section_header_emphasis()

        # Zuletzt verwendete Kanalkonfiguration automatisch vorschlagen.
        last_channels = configuration_manager.load_channel_configuration()
        if last_channels:
            self._channel_table.set_channels(last_channels)
        self._update_resolved_rate_preview()

        connect_language_changed(self.retranslate_ui)
        connect_theme_changed(self._retheme_start_button_icons)
        connect_theme_changed(self._retheme_trigger_arm_button_icon)

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

        self._storage_button.setText(t("choose_storage_location"))
        if not self._storage_path_is_set:
            self._storage_path_label.setText(t("no_storage_location"))
        self._update_start_button_labels()
        self._set_trigger_arm_button_text()
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
        self._update_resolved_rate_preview()

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

    def get_discovered_devices(self) -> list[DeviceInfo] | None:
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

        Meldet zusätzlich per Dialog, falls unter den erkannten Geräten
        welche mit einem NICHT unterstützten Modultyp sind (`DeviceInfo.
        module_type is None`, siehe `hardware/nidaq_device.py::
        _map_product_type`) - bei JEDER Geräteaktualisierung neu geprüft,
        damit ein neu angeschlossenes, (noch) nicht unterstütztes Modul
        nicht unbemerkt bleibt. Deren Kanäle sind in der Kanaltabelle
        bereits nicht auswählbar (siehe
        `gui/widgets/channel_table.py::HardwareChannelPickerDialog`) - die
        Meldung hier macht zusätzlich sichtbar, WARUM/WELCHE.
        """
        self._device_list.clear()
        devices_with_channels = [d for d in devices if d.num_channels > 0]
        self._discovered_devices = devices_with_channels
        self._channel_table.set_available_devices(devices_with_channels)
        if not devices_with_channels:
            self._device_list.addTopLevelItem(QTreeWidgetItem([t("no_devices_found")]))
            return
        unsupported_devices: list[DeviceInfo] = []
        for device in devices_with_channels:
            if device.module_type is None:
                unsupported_devices.append(device)
                module_info = f" [{t('device_module_unsupported')}]"
            else:
                module_info = f" [{device.module_type.value}]"
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

        if unsupported_devices:
            module_list = "\n".join(
                f"- {d.device_name} ({d.product_type})" for d in unsupported_devices
            )
            QMessageBox.warning(
                self,
                t("unsupported_modules_title"),
                t("unsupported_modules_body", modules=module_list),
            )

    def show_discovery_error(self, message: str) -> None:
        """Zeigt einen fehlgeschlagenen Geräteerkennungsversuch (z. B.
        NI-DAQmx-Treiber nicht installiert) direkt im Gerätebrowser an,
        statt nur im Log - der Nutzer sieht die Ursache damit genau dort,
        wo er als nächstes hinschaut (siehe
        `gui/main_window.py::_on_discover_hardware_failed`, ruft dies
        anstelle von `set_discovered_devices` auf).
        """
        self._device_list.clear()
        self._discovered_devices = None
        self._channel_table.set_available_devices([])
        self._device_list.addTopLevelItem(
            QTreeWidgetItem([f"{t('device_discovery_failed')}: {message}"])
        )

    def set_start_enabled(self, enabled: bool, reason: str = "") -> None:
        """Aktiviert/deaktiviert Play/Aufnahme (z. B. während eine Messung
        läuft) - der Stop-Button folgt dabei IMMER dem genau umgekehrten
        Zustand: klickbar nur, waehrend tatsaechlich etwas laeuft (Live-
        Anzeige oder Aufzeichnung), sonst ausgegraut.

        `reason` ist ein i18n-Key (kein fertiger Text), damit der Grund
        einen Sprachwechsel übersteht (siehe `retranslate_ui`).
        """
        self._play_button.setEnabled(enabled)
        self._record_button.setEnabled(enabled)
        self._stop_button.setEnabled(not enabled)
        # Der Scharf-Button folgt demselben Enabled-Zustand wie Play/
        # Aufnahme - AUSSER er ist selbst gerade aktiv (durchlaeuft einen
        # automatischen Zyklus, siehe `trigger_arm_toggled`): dann muss er
        # immer klickbar bleiben, damit "entschärfen" jederzeit
        # funktioniert, auch während die Messung läuft.
        if not self._trigger_arm_button.isChecked():
            self._trigger_arm_button.setEnabled(enabled)
        self._status_reason_key = reason
        self._status_label.setText(t(reason))

    def set_trigger_arm_available(self, available: bool) -> None:
        """Blendet den Scharf-Button ein/aus (siehe `main_window.py`, nach
        jeder Änderung der Trigger-Einstellungen aufgerufen) - ohne
        konfigurierten Start- oder Stopp-Trigger gäbe es nichts zum
        Scharfschalten. Wird der Button dabei ausgeblendet, während er
        noch gedrückt war, wird er zusätzlich sauber zurückgesetzt (ohne
        das `trigger_arm_toggled`-Signal erneut auszulösen)."""
        self._trigger_arm_button.setVisible(available)
        if not available and self._trigger_arm_button.isChecked():
            self.set_trigger_armed(False)

    def set_trigger_armed(self, armed: bool) -> None:
        """Setzt den Scharf-Button-Zustand PROGRAMMATISCH (z. B. wenn
        `main_window.py` wegen eines Fehlers entschärft) - blockt dabei
        `toggled`, damit das nicht fälschlich als erneuter Nutzerklick
        (`trigger_arm_toggled`) interpretiert wird."""
        self._trigger_arm_button.blockSignals(True)
        self._trigger_arm_button.setChecked(armed)
        self._trigger_arm_button.blockSignals(False)
        self._set_trigger_arm_button_text()

    def is_trigger_armed(self) -> bool:
        return self._trigger_arm_button.isChecked()

    def set_storage_path(self, path: str | None) -> None:
        self._storage_path_is_set = bool(path)
        self._storage_path_label.setText(path or t("no_storage_location"))

    def show_error(self, message: str) -> None:
        """Zeigt eine Fehlermeldung an (z. B. ungültige Konfiguration)."""
        QMessageBox.warning(self, t("error"), message)

    def get_current_measurement_parameters(
        self,
    ) -> tuple[str, float, str, bool, float, str]:
        """Gibt die aktuell im UI eingestellten Messparameter zurück.

        Returns:
            (measurement_name, sample_rate_hz, storage_format,
            recording_unlimited, recording_stop_value, recording_stop_unit)
        """
        return (
            self._name_edit.text().strip() or "Messung",
            self._sample_rate_spin.value(),
            self._storage_format_combo.currentData(),
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

    def update_channel_display_setting(self, key: tuple[str, str], values: dict) -> None:
        """Aktualisiert NUR die uebergebenen Felder fuer einen Kanal (siehe
        `ChannelTableWidget.update_display_settings`) - fuer
        `gui/main_window.py`, um die aktuelle Popout-Fensterposition beim
        Schliessen/expliziten Speichern der App zu uebernehmen, ohne die
        vom Kanal-Darstellung-Dialog gesetzten Werte zu ueberschreiben."""
        self._channel_table.update_display_settings(key, values)

    def build_current_config(self, live_only: bool = False) -> MeasurementConfig | None:
        """Baut eine MeasurementConfig aus den aktuellen UI-Eingaben.

        Wird von `_on_play_clicked`/`_on_record_clicked` (jeweils mit
        explizitem `live_only`), von `gui/main_window.py` für
        "Konfiguration speichern" sowie für den Scharf-/Auto-Rearm-Zyklus
        verwendet (dort ohne Angabe - Default `live_only=False`, also MIT
        Speicherung, entspricht dem frueheren Verhalten bei nicht
        gesetztem "Nur Live-Ansicht"-Haken). Zeigt bei unvollständigen
        Eingaben eine Fehlermeldung und gibt None zurück (kein aktiver
        Kanal, kein Name, aktiver Kanal ohne zugewiesenen Hardwarekanal).
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
        # Kein NI9210-Hard-Block mehr: Ein NI9210 zusammen mit einem
        # schnelleren Modul ist seit `resolve_rate_groups()`
        # (data/models.py) kein Fehlerfall mehr, sondern führt zu zwei
        # getrennten, per RateMerger zusammengeführten Abtast-Gruppen
        # (siehe core/controller.py::start_measurement). Die verbleibenden
        # NI9234-/NI9213-Prüfungen unten bleiben unverändert - das sind
        # intrinsische Ratenverstöße, unabhängig vom NI9210.
        if any(
            ch.enabled and ch.module_type == ModuleType.NI9234 for ch in channels
        ) and not is_valid_ni9234_sample_rate(sample_rate):
            self.show_error(
                t(
                    "error_ni9234_invalid_sample_rate",
                    suggestion=f"{next_ni9234_sample_rate_at_or_above(sample_rate):.1f}",
                )
            )
            return None

        if any(
            ch.enabled and ch.module_type == ModuleType.NI9235 for ch in channels
        ) and not is_valid_grid_sample_rate(ModuleType.NI9235, sample_rate):
            self.show_error(
                t(
                    "error_ni9235_invalid_sample_rate",
                    suggestion=f"{next_grid_sample_rate_at_or_above(ModuleType.NI9235, sample_rate):.1f}",
                )
            )
            return None

        for device_name, group_channels in ni9213_device_groups(channels).items():
            max_rate = max_ni9213_sample_rate_hz(group_channels)
            if sample_rate > max_rate + 0.05:
                mode_key = _ADC_TIMING_MODE_LABEL_KEYS.get(
                    group_channels[0].adc_timing_mode, "adc_timing_mode_high_resolution"
                )
                self.show_error(
                    t(
                        "error_ni9213_rate_too_high",
                        device=device_name,
                        count=len(group_channels),
                        mode=t(mode_key),
                        max_rate=f"{max_rate:.1f}",
                    )
                )
                return None

        active_channels = [ch for ch in channels if ch.enabled]
        # Ring-Buffer-Groesse/Blockgroesse muessen sich an der
        # TATSAECHLICHEN Tick-Rate orientieren (= schnellste Ratengruppe),
        # nicht an der rohen Zielrate: bei einem alleinstehenden NI9210
        # z. B. ist die Zielrate irrelevant (immer 14 S/s) - mit der
        # rohen Zielrate berechnete Blockgroessen waeren dort viel zu
        # gross und liessen den ersten Lesezyklus in ein Timeout laufen.
        try:
            rate_groups = resolve_rate_groups(active_channels, sample_rate)
            effective_tick_rate = max(
                (g.resolved_sample_rate_hz for g in rate_groups), default=sample_rate
            )
        except ValueError:
            effective_tick_rate = sample_rate

        ring_buffer_size = self._calculate_dynamic_buffer_size(
            effective_tick_rate, len(active_channels)
        )
        samples_per_read = self._calculate_samples_per_read(effective_tick_rate)

        return MeasurementConfig(
            name=name,
            sample_rate_hz=sample_rate,
            channels=channels,
            storage_format=StorageFormat(self._storage_format_combo.currentData()),
            samples_per_read=samples_per_read,
            ring_buffer_size=ring_buffer_size,
            save_to_disk=not live_only,
            recording_unlimited=self._recording_unlimited_checkbox.isChecked(),
            recording_stop_value=float(self._recording_stop_spin.value()),
            recording_stop_unit=RecordingStopUnit(self._recording_stop_unit_combo.currentData()),
            # trigger bewusst NICHT gesetzt (Default-`TriggerConfig()`,
            # also kein Trigger) - die tatsächlich aktive Trigger-
            # Konfiguration lebt seit der Verallgemeinerung auf Start UND
            # Stopp in `gui/main_window.py` (siehe
            # `gui/trigger_settings_dialog.py::TriggerSettingsDialog`),
            # nicht mehr in der Setup-Ansicht - `MainWindow` speist sie
            # direkt in `config.trigger` ein (siehe `_on_start_measurement`,
            # `_on_save_config`).
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
        self._recording_unlimited_checkbox.setChecked(config.recording_unlimited)
        self._recording_stop_spin.setValue(max(1, int(config.recording_stop_value)))
        self._populate_recording_stop_unit_combo(config.recording_stop_unit.value)
        self._on_recording_unlimited_toggled(self._recording_unlimited_checkbox.isChecked())
        self._channel_table.set_channels(config.channels)
        self._update_resolved_rate_preview()
        # config.trigger wird NICHT hier übernommen - `gui/main_window.py`
        # liest es direkt aus dem geladenen `MeasurementConfig` und setzt
        # es als seine eigene `_trigger_config` (siehe `_on_load_config`).

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

    def _on_play_clicked(self) -> None:
        config = self.build_current_config(live_only=True)
        if config is None:
            return
        self.start_measurement_requested.emit(config)

    def _on_record_clicked(self) -> None:
        config = self.build_current_config(live_only=False)
        if config is None:
            return
        self.start_measurement_requested.emit(config)

    def _on_trigger_arm_button_toggled(self, checked: bool) -> None:
        self._set_trigger_arm_button_text()
        self.trigger_arm_toggled.emit(checked)

    def _set_trigger_arm_button_text(self) -> None:
        key = "trigger_disarm_button" if self._trigger_arm_button.isChecked() else "trigger_arm_button"
        self._trigger_arm_button.setText(f"  {t(key)}")
        fix_toggle_button_width(
            self._trigger_arm_button,
            f"  {t('trigger_arm_button')}",
            f"  {t('trigger_disarm_button')}",
        )

    def _retheme_start_button_icons(self) -> None:
        # Play/Aufnahme haben feste, theme-unabhaengige Symbolfarben (siehe
        # `gui/theme.py::PLAY_ICON_COLOR`/`RECORD_ICON_COLOR`). Stop hat
        # KEINEN fest codierten Hintergrund (siehe `ACTION_BUTTON_STYLE`)
        # und bleibt daher bei der normalen theme-abhaengigen
        # `nav_icon_color()` (kein `color=` uebergeben).
        self._play_button.setIcon(QIcon(draw_play_icon(24, y_offset=0.6, color=PLAY_ICON_COLOR)))
        self._record_button.setIcon(
            QIcon(draw_record_icon(24, y_offset=0.6, color=RECORD_ICON_COLOR))
        )
        self._stop_button.setIcon(QIcon(draw_stop_icon(24, y_offset=0.6)))
        # `ACTION_BUTTON_STYLE` referenziert `palette(...)` - ohne manuelles
        # unpolish()/polish() bleiben Rahmen/Hintergrund nach einem
        # Live-Theme-Wechsel optisch im alten Theme haengen (gleicher
        # Befund wie bei den Navigationskacheln, siehe
        # `gui/main_window.py::_retheme_nav_icons`).
        for button in (self._play_button, self._record_button, self._stop_button):
            repolish(button)

    def _retheme_trigger_arm_button_icon(self) -> None:
        # Kein fest codierter Hintergrund mehr (siehe
        # `TRIGGER_ARM_BUTTON_STYLE`) - Icon bleibt daher bei der normalen
        # theme-abhaengigen `nav_icon_color()` (kein `color=` uebergeben).
        # Das Icon selbst wird NICHT extra fuer den gecheckten (scharfen)
        # Zustand umgefaerbt (nur der Text via `palette(highlighted-text)`
        # im Stylesheet) - Schwarz/Weiss bleibt auf dem Akzentton
        # (`palette(highlight)`) in beiden Themes ausreichend lesbar.
        self._trigger_arm_button.setIcon(QIcon(draw_trigger_icon(24, y_offset=0.6)))
        repolish(self._trigger_arm_button)

    def _update_start_button_labels(self) -> None:
        # Kurzer Button-Text (siehe `play_button_label`/`record_button_label`/
        # `stop_button_label`) UND ausfuehrlicherer Tooltip (bestehende
        # `live_only`/`start_measurement`/`stop_measurement`-Keys).
        self._play_button.setText(f"  {t('play_button_label')}")
        self._play_button.setToolTip(t("live_only"))
        self._record_button.setText(f"  {t('record_button_label')}")
        self._record_button.setToolTip(t("start_measurement"))
        self._stop_button.setText(f"  {t('stop_button_label')}")
        self._stop_button.setToolTip(t("stop_measurement"))

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

    def _update_resolved_rate_preview(self) -> None:
        """Aktualisiert die nicht-blockierende Ratengruppen-Vorschau.

        Bewusst tolerant: ein `ValueError` (z. B. gerade eine ungueltige
        NI9234-Rate waehrend der Eingabe) wird hier verschluckt und die
        Vorschau einfach geleert - `build_current_config()` bleibt die
        einzige verbindliche Validierung (beim Start/Aufnehmen), diese
        Vorschau ist reine Zusatzinformation.
        """
        active_channels = [ch for ch in self._channel_table.get_channels() if ch.enabled]
        sample_rate = self._sample_rate_spin.value()
        try:
            rate_groups = resolve_rate_groups(active_channels, sample_rate)
        except ValueError:
            self._resolved_rate_preview_label.setText("")
            return

        if len(rate_groups) <= 1:
            # Regelfall: genau eine Gruppe, Zielrate == tatsaechliche
            # Rate - keine Zusatzinfo noetig, Label bleibt leer/unsichtbar.
            self._resolved_rate_preview_label.setText("")
            return

        parts = []
        for group in rate_groups:
            if group.reason == "Zielrate":
                parts.append(t("resolved_rate_preview_target", rate=f"{group.resolved_sample_rate_hz:.1f}"))
            else:
                module_names = sorted({ch.module_type.value for ch in group.channels})
                parts.append(
                    t(
                        "resolved_rate_preview_fixed",
                        modules="/".join(module_names),
                        rate=f"{group.resolved_sample_rate_hz:.1f}",
                    )
                )
        self._resolved_rate_preview_label.setText(" · ".join(parts))

    def _calculate_samples_per_read(self, sample_rate_hz: float) -> int:
        """Berechnet eine adaptive Blockgroesse pro DAQ-Read.

        Rein aus der Ziel-BLOCKDAUER (`_target_read_block_ms`) abgeleitet,
        nicht aus einer festen Sample-Anzahl - skaliert dadurch dynamisch
        mit der Abtastrate: bei hoher Rate viele Samples pro Block (haelt
        die Aufrufhaeufigkeit von `device.read()` konstant niedrig, siehe
        `__init__`), bei niedriger Rate (z. B. NI9210 mit 14 S/s) entsprechend
        wenige - so bleibt die Live View auch dort fluessig, statt in
        seltenen, dafuer grossen Schueben zu aktualisieren. Nach oben durch
        `_max_samples_per_read` begrenzt, nach unten auf mindestens 1 Sample.
        """
        target = int(sample_rate_hz * (self._target_read_block_ms / 1000.0))
        return max(1, min(self._max_samples_per_read, target))

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

