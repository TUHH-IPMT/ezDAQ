"""
gui/i18n.py

Einfaches Internationalisierungs-System für Deutsch und Englisch.

Verwendung:
    from gui.i18n import t, connect_language_changed, set_language

    set_language("de")  # oder "en"
    label.setText(t("start_measurement"))
    label.setText(t("devices_found", count=3))  # Interpolation via .format()

Live-Umschaltung:
    Jede Ansicht registriert ihre eigene `retranslate_ui`-Methode über
    `connect_language_changed(self.retranslate_ui)` (typischerweise am Ende
    von `__init__`). `set_language()` benachrichtigt daraufhin alle
    registrierten Ansichten, sodass ein Sprachwechsel sofort in der
    laufenden App sichtbar wird - ohne Neustart.
"""

from __future__ import annotations

from typing import Callable, Optional

_current_language = "de"

# Übersetzungs-Dictionaries
_translations = {
    "de": {
        # Menu
        "menu_file": "Datei",
        "menu_settings": "Optionen",
        "menu_quit": "Beenden",
        "menu_help": "Hilfe",
        "menu_about": "Über...",
        "menu_save_config": "Konfiguration speichern",
        "menu_load_config": "Konfiguration laden",
        "file_filter_json": "Konfigurationsdateien (*.json)",

        # Navigation
        "nav_setup": "Konfiguration",
        "nav_live_view": "Live-Ansicht",

        # Setup View
        "connected_devices": "Angeschlossene Geräte",
        "search_devices": "Geräte suchen",
        "channel_configuration": "Kanalkonfiguration",
        "measurement_settings": "Messeinstellungen",
        "storage_settings": "Speichereinstellungen",
        "measurement_name": "Dateiname",
        "sample_rate_hz": "Abtastrate [Hz]",
        "storage_format": "Speicherformat",
        "storage_format_parquet": "Parquet (empfohlen)",
        "storage_format_csv": "CSV",
        "live_only": "Nur Live-Ansicht (kein Speichern)",
        "storage_location": "Speicherort",
        "choose_storage_location": "Speicherort wählen",
        "start_measurement": "Messung starten",
        "no_storage_location": "Kein Speicherort gewählt",
        "error_no_active_channels": "Bitte mindestens einen aktiven Kanal konfigurieren.",
        "error_channel_missing_hw_channel": (
            "Folgende(r) aktive Kanal/Kanäle hat/haben noch keinen Hardwarekanal "
            "zugewiesen: {names}. Bitte über \"Kanal zuweisen...\" einen echten "
            "Kanal auswählen (ggf. zuerst \"Geräte suchen\" ausführen)."
        ),
        "error_no_name": "Bitte einen Namen für die Messung angeben.",
        "device_channel_count": "{count} Kanäle",
        "naming_scheme": "Dateisuffix",
        "naming_number_suffix": "Nummer",
        "naming_digits": "Stellen",
        "naming_include_date": "Datum",
        "naming_include_time": "Uhrzeit",
        "error_name_conflict": (
            "Eine Messung mit dem Namen '{name}' existiert bereits. Bitte "
            "einen anderen Namen wählen oder das Nummernsuffix aktivieren."
        ),

        # Live View
        "duration": "Dauer",
        "sample_rate": "Abtastrate",
        "stop_measurement": "Messung stoppen",
        "min": "Min",
        "max": "Max",
        "storage_buffer_group": "Speicherpuffer (Schreib-Rückstand)",
        "duration_value": "Dauer: {value}",
        "sample_rate_value": "Abtastrate: {value}",
        "menu_channel_display": "Kanal-Darstellung festlegen",
        "channel_display_dialog_title": "Kanal-Darstellung",
        "channel_display_no_channels": "Keine Kanäle konfiguriert.",
        "plot_color": "Kurvenfarbe",
        "plot_background": "Hintergrund",
        "autoscale_checkbox": "Autoskalierung",
        "autoscale_checkbox_tooltip": (
            "Nutzt den festgelegten Bereich, solange die Messwerte darin "
            "liegen - überschreiten sie ihn, schaltet die Skalierung "
            "automatisch auf den tatsächlichen Wertebereich um."
        ),
        "axis_time": "Zeit",
        "storage_detail": "Datei: {file_size} — Rückstand: {pending} / {capacity} Samples ({percent} %)",

        # Analysis View
        "analysis": "Analyse",
        "drag_drop_files": "Messdateien (.parquet/.csv) hierher ziehen oder über den Button auswählen",
        "load_measurement": "Messung laden",
        "browse_file_button": "Datei auswählen...",
        "loaded_files_channels": "Geladene Dateien / Kanäle",
        "tree_header_name": "Name",
        "remove_file_action": "Datei aus Analyse löschen",
        "unsupported_format_title": "Nicht unterstütztes Format",
        "unsupported_format_body": "Das Format '{suffix}' wird nicht unterstützt (erwartet: .parquet oder .csv).",
        "load_error_title": "Fehler beim Laden",
        "already_loaded_title": "Bereits geladen",
        "already_loaded_body": "Datei {filename} ist bereits geladen.",
        "files_loaded_count": "Geladen: {count} Datei(en)",
        "load_measurement_dialog_title": "Messdatei auswählen",
        "measurement_files_filter": "Messdaten (*.parquet *.csv)",
        "analysis_layout": "Layout:",
        "analysis_layout_single": "1x1",
        "analysis_layout_split": "2x1",
        "analysis_layout_three": "3x1",
        "analysis_layout_four": "4x1",
        "analysis_layout_four_square": "2x2",
        "analysis_category_layout": "Layout",
        "analysis_category_files": "Dateien und Kanäle",

        # Channel Table
        "col_number": "Nr.",
        "col_active": "Aktiv",
        "col_hw_channel": "Hardwarekanal",
        "col_display_name": "Anzeigename",
        "col_unit": "Einheit",
        "col_signal_type": "Signaltyp",
        "col_scale": "Skalierungsfaktor",
        "col_offset": "Offsetwert",
        "col_sensitivity": "Sensitivität\n(mV/g, IEPE)",
        "signal_type_voltage": "Spannung",
        "signal_type_iepe": "IEPE-Beschleunigung",
        "add_channel_button": "Kanal hinzufügen",
        "remove_channel_button": "Ausgewählten Kanal entfernen",
        "default_channel_name": "Kanal {index}",
        "scale_disabled_tooltip": (
            "Bei IEPE-Sensoren übernimmt die Sensitivität die physikalische "
            "Skalierung; die Skalierung bleibt bei 1."
        ),
        "sensitivity_disabled_tooltip": "Nur für IEPE-Sensoren relevant.",
        "choose_hw_channel_button": "Kanal zuweisen...",
        "choose_hw_channel_title": "Hardwarekanal wählen",
        "hw_channel_picker_no_devices": (
            "Noch keine Geräte erkannt. Bitte zuerst im Bereich "
            "\"Angeschlossene Geräte\" auf \"Geräte suchen\" klicken."
        ),
        "hw_channel_already_used": "{channel} (bereits belegt)",
        "no_hw_channel_available": "Kein freier Hardwarekanal mehr verfügbar.",
        "all_channels_assigned_title": "Alle Kanäle belegt",
        "all_channels_assigned_body": (
            "Es sind bereits alle erkannten Hardwarekanäle einer Zeile "
            "zugeordnet - es kann kein weiterer Kanal hinzugefügt werden."
        ),
        "choose_signal_type_button": "Signaltyp wählen...",
        "choose_signal_type_title": "Signaltyp wählen",

        # Settings
        "settings": "Einstellungen",
        "language": "Sprache",
        "german": "Deutsch",
        "english": "English",
        "menu_theme": "Design",
        "theme_light": "Hell",
        "theme_dark": "Dunkel",
        "ok": "OK",
        "cancel": "Abbrechen",

        # Status
        "ready": "Bereit",
        "measurement_running": "Messung läuft ...",
        "measurement_running_named": "Messung '{name}' läuft ...",
        "measurement_completed": "Messung abgeschlossen",
        "measurement_completed_named": "Messung '{name}' abgeschlossen ({duration} s)",
        "no_devices_found": "Keine Geräte gefunden (Treiber installiert? Hardware angeschlossen? Hardware im Treiber für diesen PC reserviert?)",
        "devices_found": "Gerät(e) erkannt",

        # Konfiguration speichern/laden (Datei-Menü)
        "status_config_saved": "Konfiguration gespeichert: {filename}",
        "status_config_loaded": "Konfiguration geladen: {filename}",
        "error_config_save_failed": "Konfiguration konnte nicht gespeichert werden:\n{path}",
        "error_config_load_failed": "Konfiguration konnte nicht geladen werden:\n{path}",

        # Errors
        "error": "Fehler",
        "measurement_error": "Messfehler",
        "cannot_start_measurement": "Messung konnte nicht gestartet werden",
        "no_storage_selected": "Bitte zuerst einen Ordner wählen",
        "error_no_storage_title": "Kein Speicherort",
        "error_no_storage_body": (
            "Bitte zuerst über 'Datei -> Speicherort wählen...' einen Ordner "
            "auswählen, in dem die Messdaten gespeichert werden sollen."
        ),
        "measurement_hardware_error_body": (
            "Die Messung wurde aufgrund eines Hardwarefehlers beendet:\n{error}"
        ),

        # Über
        "window_title": "DAQSoftware - Messdatenerfassung und Analyse",
        "about_title": "Über DAQSoftware",
        "about_body": (
            "DAQSoftware\n"
            "Version 1.0\n\n"
            "Anwendung zur Messdatenerfassung, Visualisierung und Analyse "
            "mit NI cDAQ (NI 9215 / NI 9234).\n\n"
            "© 2026 IPMT\n\n"
            "Verwendete Open-Source-Komponenten:\n"
            "Python, PyQt6, PyQtGraph, nidaqmx, NumPy, Pandas, PyArrow, psutil."
        ),
    },
    "en": {
        # Menu
        "menu_file": "File",
        "menu_settings": "Options",
        "menu_quit": "Quit",
        "menu_help": "Help",
        "menu_about": "About...",
        "menu_save_config": "Save Configuration",
        "menu_load_config": "Load Configuration",
        "file_filter_json": "Configuration Files (*.json)",

        # Navigation
        "nav_setup": "Configuration",
        "nav_live_view": "Live View",

        # Setup View
        "connected_devices": "Connected Devices",
        "search_devices": "Search Devices",
        "channel_configuration": "Channel Configuration",
        "measurement_settings": "Measurement Settings",
        "storage_settings": "Storage Settings",
        "measurement_name": "File Name",
        "sample_rate_hz": "Sample Rate [Hz]",
        "storage_format": "Storage Format",
        "storage_format_parquet": "Parquet (recommended)",
        "storage_format_csv": "CSV",
        "live_only": "Live View Only (do not save)",
        "storage_location": "Storage Location",
        "choose_storage_location": "Choose Storage Location",
        "start_measurement": "Start Measurement",
        "no_storage_location": "No Storage Location Selected",
        "error_no_active_channels": "Please configure at least one active channel.",
        "error_channel_missing_hw_channel": (
            "The following active channel(s) have no hardware channel assigned "
            "yet: {names}. Please use \"Assign channel...\" to pick a real "
            "channel (run \"Search Devices\" first if needed)."
        ),
        "error_no_name": "Please enter a measurement name.",
        "device_channel_count": "{count} Channels",
        "naming_scheme": "File Suffix",
        "naming_number_suffix": "Number",
        "naming_digits": "Digits",
        "naming_include_date": "Date",
        "naming_include_time": "Time",
        "error_name_conflict": (
            "A measurement named '{name}' already exists. Please choose a "
            "different name or enable the number suffix."
        ),

        # Live View
        "duration": "Duration",
        "sample_rate": "Sample Rate",
        "stop_measurement": "Stop Measurement",
        "min": "Min",
        "max": "Max",
        "storage_buffer_group": "Storage Buffer (Write Backlog)",
        "duration_value": "Duration: {value}",
        "sample_rate_value": "Sample Rate: {value}",
        "menu_channel_display": "Set Channel Display",
        "channel_display_dialog_title": "Channel Display",
        "channel_display_no_channels": "No channels configured.",
        "plot_color": "Curve Color",
        "plot_background": "Background",
        "autoscale_checkbox": "Autoscale",
        "autoscale_checkbox_tooltip": (
            "Uses the configured range as long as the measured values stay "
            "within it - if they exceed it, scaling automatically switches "
            "to the actual value range."
        ),
        "axis_time": "Time",
        "storage_detail": "File: {file_size} — Backlog: {pending} / {capacity} Samples ({percent} %)",

        # Analysis View
        "analysis": "Analysis",
        "drag_drop_files": "Drag measurement files (.parquet/.csv) here or use the button",
        "load_measurement": "Load Measurement",
        "browse_file_button": "Select File...",
        "loaded_files_channels": "Loaded Files / Channels",
        "tree_header_name": "Name",
        "remove_file_action": "Remove File from Analysis",
        "unsupported_format_title": "Unsupported Format",
        "unsupported_format_body": "The format '{suffix}' is not supported (expected: .parquet or .csv).",
        "load_error_title": "Error Loading File",
        "already_loaded_title": "Already Loaded",
        "already_loaded_body": "File {filename} is already loaded.",
        "files_loaded_count": "Loaded: {count} File(s)",
        "load_measurement_dialog_title": "Select Measurement File",
        "measurement_files_filter": "Measurement Data (*.parquet *.csv)",
        "analysis_layout": "Layout:",
        "analysis_layout_single": "1x1",
        "analysis_layout_split": "2x1",
        "analysis_layout_three": "3x1",
        "analysis_layout_four": "4x1",
        "analysis_layout_four_square": "2x2",
        "analysis_category_layout": "Layout",
        "analysis_category_files": "Files and Channels",

        # Channel Table
        "col_number": "No.",
        "col_active": "Active",
        "col_hw_channel": "Hardware Channel",
        "col_display_name": "Display Name",
        "col_unit": "Unit",
        "col_signal_type": "Signal Type",
        "col_scale": "Scale Factor",
        "col_offset": "Offset Value",
        "col_sensitivity": "Sensitivity\n(mV/g, IEPE)",
        "signal_type_voltage": "Voltage",
        "signal_type_iepe": "IEPE Acceleration",
        "add_channel_button": "Add Channel",
        "remove_channel_button": "Remove Selected Channel",
        "default_channel_name": "Channel {index}",
        "scale_disabled_tooltip": (
            "For IEPE sensors, sensitivity handles the physical scaling; "
            "scale stays at 1."
        ),
        "sensitivity_disabled_tooltip": "Only relevant for IEPE sensors.",
        "choose_hw_channel_button": "Assign channel...",
        "choose_hw_channel_title": "Choose Hardware Channel",
        "hw_channel_picker_no_devices": (
            "No devices detected yet. Please click \"Search Devices\" in "
            "the \"Connected Devices\" section first."
        ),
        "hw_channel_already_used": "{channel} (already assigned)",
        "no_hw_channel_available": "No free hardware channel available.",
        "all_channels_assigned_title": "All Channels Assigned",
        "all_channels_assigned_body": (
            "All detected hardware channels are already assigned to a row - "
            "no further channel can be added."
        ),
        "choose_signal_type_button": "Choose signal type...",
        "choose_signal_type_title": "Choose Signal Type",

        # Settings
        "settings": "Settings",
        "language": "Language",
        "german": "Deutsch",
        "english": "English",
        "menu_theme": "Theme",
        "theme_light": "Light",
        "theme_dark": "Dark",
        "ok": "OK",
        "cancel": "Cancel",

        # Status
        "ready": "Ready",
        "measurement_running": "Measurement running ...",
        "measurement_running_named": "Measurement '{name}' running ...",
        "measurement_completed": "Measurement completed",
        "measurement_completed_named": "Measurement '{name}' completed ({duration} s)",
        "no_devices_found": "No devices found (Drivers installed? Hardware connected? Hardware reserved for this PC in the driver?)",
        "devices_found": "Device(s) found",

        # Konfiguration speichern/laden (Datei-Menü)
        "status_config_saved": "Configuration saved: {filename}",
        "status_config_loaded": "Configuration loaded: {filename}",
        "error_config_save_failed": "Configuration could not be saved:\n{path}",
        "error_config_load_failed": "Configuration could not be loaded:\n{path}",

        # Errors
        "error": "Error",
        "measurement_error": "Measurement Error",
        "cannot_start_measurement": "Could not start measurement",
        "no_storage_selected": "Please select a storage location first",
        "error_no_storage_title": "No Storage Location",
        "error_no_storage_body": (
            "Please first select a folder via 'File -> Choose Storage "
            "Location...' where the measurement data should be saved."
        ),
        "measurement_hardware_error_body": (
            "The measurement was stopped due to a hardware error:\n{error}"
        ),

        # About
        "window_title": "DAQSoftware - Data Acquisition and Analysis",
        "about_title": "About DAQSoftware",
        "about_body": (
            "DAQSoftware\n"
            "Version 1.0\n\n"
            "Application for data acquisition, visualization, and analysis "
            "with NI cDAQ (NI 9215 / NI 9234).\n\n"
            "© 2026 IPMT\n\n"
            "Open-source components used:\n"
            "Python, PyQt6, PyQtGraph, nidaqmx, NumPy, Pandas, PyArrow, psutil."
        ),
    },
}


def set_language(lang: str) -> None:
    """Setzt die aktuelle Sprache auf 'de' oder 'en' und benachrichtigt
    alle über `connect_language_changed` registrierten Ansichten."""
    global _current_language
    if lang not in _translations or lang == _current_language:
        return
    _current_language = lang
    _get_signals().language_changed.emit(lang)


def get_language() -> str:
    """Gibt die aktuelle Sprache zurück."""
    return _current_language


def t(key: str, **kwargs) -> str:
    """Übersetzt einen Schlüssel in die aktuelle Sprache.

    Fallback auf den Schlüssel selbst, falls nicht gefunden. Optionale
    kwargs werden per `str.format()` in das Template eingesetzt, z. B.
    `t("devices_found_count", count=3)`.
    """
    template = _translations.get(_current_language, {}).get(key, key)
    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError):
            return template
    return template


# ---------------------------------------------------------------------- #
# Live-Retranslate-Signal
# ---------------------------------------------------------------------- #
#
# Lazy konstruiert: `gui/i18n.py` wird transitiv importiert, BEVOR
# `QApplication` in main.py erzeugt wird (Importe von `gui.main_window`
# laufen vor `QApplication(sys.argv)`). Ein QObject darf zu diesem
# Zeitpunkt nicht instanziiert werden - daher Konstruktion erst beim
# ersten echten Gebrauch (`set_language()`/`connect_language_changed()`),
# beides passiert in der Praxis erst nach `QApplication`-Erzeugung.

_signals: Optional["_I18nSignals"] = None


def _get_signals() -> "_I18nSignals":
    global _signals
    if _signals is None:
        from PyQt6.QtCore import QObject, pyqtSignal

        class _I18nSignals(QObject):
            language_changed = pyqtSignal(str)

        _signals = _I18nSignals()
    return _signals


def connect_language_changed(slot: Callable[[], None]) -> None:
    """Registriert `slot` (typischerweise `view.retranslate_ui`), der bei
    jedem Sprachwechsel aufgerufen wird."""
    _get_signals().language_changed.connect(slot)
