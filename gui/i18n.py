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
        "menu_sensor_database": "Sensor-Datenbank...",

        # Navigation
        "nav_setup": "Konfiguration",
        "nav_live_view": "Live-Ansicht",

        # Setup View
        "connected_devices": "Angeschlossene Geräte",
        "search_devices": "Geräte suchen",
        "open_ni_max_button": "NI-MAX öffnen",
        "ni_max_open_failed": "NI-MAX konnte nicht geöffnet werden",
        "channel_configuration": "Kanalkonfiguration",
        "measurement_settings": "Messeinstellungen",
        "storage_settings": "Speichereinstellungen",
        "measurement_name": "Dateiname",
        "sample_rate_hz": "Abtastrate [Hz]",
        "menu_trigger_settings": "Trigger-Einstellungen...",
        "trigger_settings_dialog_title": "Trigger-Einstellungen",
        "trigger_start_section_title": "Start",
        "trigger_stop_section_title": "Stopp",
        "trigger_mode_label": "Modus",
        "trigger_mode_manual": "Manuell",
        "trigger_mode_threshold": "Schwellwert",
        "trigger_mode_serial": "Seriell (USB)",
        "trigger_channel_label": "Kanal",
        "trigger_threshold_value_label": "Schwellwert",
        "trigger_direction_label": "Richtung",
        "trigger_direction_rises_above": "Steigt über Schwelle",
        "trigger_direction_falls_below": "Fällt unter Schwelle",
        "trigger_direction_abs_exceeds": "Betrag überschreitet Schwelle",
        "trigger_pretrigger_seconds_label": "Vorlaufzeit [s]",
        "trigger_serial_port_label": "COM-Port",
        "trigger_serial_refresh_button": "Aktualisieren",
        "trigger_serial_baud_label": "Baudrate",
        "trigger_serial_message_label": "Erwartetes Signal",
        "trigger_test_group_title": "Seriellen Trigger testen",
        "trigger_test_source_label": "Trigger",
        "trigger_test_source_start": "Start-Trigger",
        "trigger_test_source_stop": "Stopp-Trigger",
        "trigger_test_start_button": "Test starten",
        "trigger_test_stop_button": "Test stoppen",
        "trigger_test_matched_log": "Signal erkannt (Treffer)!",
        "error_trigger_channel_not_active": "Bitte einen aktiven Kanal für den Schwellwert-Trigger auswählen.",
        "error_trigger_serial_no_port": "Bitte einen COM-Port für den seriellen Trigger auswählen.",
        "error_trigger_serial_empty_message": "Bitte ein erwartetes Signal für den seriellen Trigger eingeben.",
        "error_trigger_serial_invalid_baud": "Ungültige Baudrate für den seriellen Trigger.",
        "armed_waiting_threshold": "Scharf - wartet auf Schwellwert-Trigger an Kanal {channel} (Schwelle: {threshold})...",
        "armed_waiting_serial": "Scharf - wartet auf serielles Triggersignal an {port}...",
        "measurement_armed_status": "Scharf geschaltet: {name} - wartet auf Trigger",
        "trigger_connection_failed_title": "Serieller Trigger fehlgeschlagen",
        "trigger_stop_connection_failed_title": "Stopp-Trigger nicht verfügbar",
        "error_stop_trigger_connection_failed": "Der Stopp-Trigger konnte nicht verbunden werden ({message}). Die Messung läuft weiter - Stopp bitte manuell oder über das Aufnahme-Limit.",
        "storage_format": "Speicherformat",
        "storage_format_parquet": "Parquet (empfohlen)",
        "storage_format_csv": "CSV",
        "live_only": "Nur Live-Ansicht (kein Speichern)",
        "recording_unlimited": "Unbegrenzt (bis Speicherplatz voll)",
        "recording_limit_label": "Messzyklus",
        "recording_stop_unit_samples": "Messwerte",
        "recording_stop_unit_seconds": "Sekunden",
        "recording_stop_unit_minutes": "Minuten",
        "recording_stop_unit_hours": "Stunden",
        "storage_location": "Speicherort",
        "choose_storage_location": "Speicherort wählen",
        "start_measurement": "Messung starten",
        "no_storage_location": "Kein Speicherort gewählt",
        "error_no_active_channels": "Bitte mindestens einen aktiven Kanal konfigurieren.",
        "error_ni9210_fixed_sample_rate": "Das NI9210 unterstützt ausschließlich {rate} S/s.",
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
        "plot_visible_checkbox": "Aktiv",
        "plot_visible_checkbox_tooltip": (
            "Deaktivierte Kanäle werden nicht mehr geplottet (weder im "
            "Hauptfenster noch in einem eigenen Fenster) - die Aufzeichnung "
            "selbst ist davon nicht betroffen."
        ),
        "popout_button": "Eigenes Fenster",
        "popout_button_tooltip": (
            "Zeigt diesen Kanal statt im Hauptraster in einem eigenen, frei "
            "verschiebbaren Fenster an - verhindert Doppel-Darstellung."
        ),
        "axis_time": "Zeit",
        "storage_detail": "Datei: {file_size} — Rückstand: {pending} / {capacity} Samples ({percent} %)",

        # Analysis View
        "analysis": "Analyse",
        "drag_drop_files": "Messdateien (.parquet/.csv) hierher ziehen oder über Datei → Messung laden auswählen",
        "load_measurement": "Messung laden",
        "loaded_files_channels": "Geladene Dateien / Kanäle",
        "search_files_placeholder": "Dateien/Kanäle durchsuchen...",
        "search_no_results": "Keine Treffer für diese Suche.",
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
        "analysis_category_spectral": "Spektralanalyse",
        "analysis_category_filter": "Filter",
        "analysis_fft_button": "FFT",
        "analysis_fft_tooltip": "Frequenzspektrum eines Kanals berechnen",
        "analysis_lowpass_button": "Tiefpass",
        "analysis_lowpass_tooltip": "Tiefpassfilter auf einen Kanal anwenden",
        "analysis_highpass_button": "Hochpass",
        "analysis_highpass_tooltip": "Hochpassfilter auf einen Kanal anwenden",
        "analysis_smoothing_button": "Glättung",
        "analysis_smoothing_tooltip": "Gleitenden Mittelwert auf einen Kanal anwenden",
        "analysis_function_dialog_title_fft": "FFT - Kanal auswählen",
        "analysis_function_dialog_title_lowpass": "Tiefpassfilter - Kanal auswählen",
        "analysis_function_dialog_title_highpass": "Hochpassfilter - Kanal auswählen",
        "analysis_function_dialog_title_smoothing": "Glättungsfilter - Kanal auswählen",
        "analysis_select_channel": "Kanal:",
        "analysis_cutoff_frequency": "Grenzfrequenz (Hz):",
        "analysis_window_size": "Fenstergröße (Samples):",
        "analysis_run_button": "Ausführen",
        "analysis_no_channels_available": "Es sind keine Kanäle zum Analysieren geladen. Bitte zuerst eine Messdatei laden.",
        "analysis_no_channels_title": "Keine Kanäle geladen",
        "analysis_error_title": "Analysefehler",
        "analysis_error_body": "Die Analyse konnte nicht durchgeführt werden:\n{error}",
        "analysis_no_sample_rate_body": (
            "Für diesen Kanal konnte keine Abtastrate ermittelt werden "
            "(weder aus den Metadaten noch aus dem Zeitverlauf)."
        ),
        "analysis_fft_result_suffix": "FFT",
        "analysis_lowpass_result_suffix": "Tiefpass",
        "analysis_highpass_result_suffix": "Hochpass",
        "analysis_smoothing_result_suffix": "Geglättet",
        "axis_frequency": "Frequenz",
        "save_as_action": "Speichern als...",
        "save_as_csv_action": "Als CSV speichern...",
        "save_as_parquet_action": "Als Parquet speichern...",
        "remove_result_action": "Analyseergebnis entfernen",
        "remove_channel_action": "Kanal aus Analyse löschen",
        "confirm_delete_title": "Löschen bestätigen",
        "delete_action": "Löschen",
        "confirm_remove_file_body": "Soll die Datei '{name}' wirklich aus der Analyse entfernt werden?",
        "confirm_remove_channel_body": "Soll der Kanal '{name}' wirklich entfernt werden?",
        "save_result_csv_filter": "CSV-Datei (*.csv)",
        "save_result_parquet_filter": "Parquet-Datei (*.parquet)",
        "save_result_dialog_title": "Analyseergebnis speichern",
        "save_result_success": "Analyseergebnis gespeichert: {filename}",
        "save_result_error_title": "Fehler beim Speichern",
        "save_result_error_body": "Das Analyseergebnis konnte nicht gespeichert werden:\n{error}",

        # Channel Table
        "col_number": "Nr.",
        "col_active": "Aktiv",
        "col_hw_channel": "Hardwarekanal",
        "col_display_name": "Anzeigename",
        "col_unit": "Einheit",
        "col_signal_type": "Signaltyp",
        "col_parameters": "Einstellungen",
        "signal_type_voltage": "Spannung",
        "signal_type_iepe": "IEPE-Beschleunigung",
        "signal_type_thermocouple": "Thermoelement",
        "add_channel_button": "Kanal hinzufügen",
        "remove_channel_button": "Ausgewählten Kanal entfernen",
        "default_channel_name": "Kanal {index}",
        "choose_parameters_button": "Weitere Einstellungen",
        "parameters_dialog_title": "Kanaleinstellungen",
        "param_scale_label": "Skalierungsfaktor:",
        "param_offset_label": "Offsetwert:",
        "param_sensitivity_label": "Sensitivität (mV/g):",
        "param_thermocouple_type_label": "Thermoelement-Typ:",
        "param_adc_timing_mode_label": "ADC-Timing-Modus:",
        "param_adc_timing_mode_hint": "Gilt für alle Kanäle dieses Moduls (nur NI9213).",
        "adc_timing_mode_automatic": "Automatisch",
        "adc_timing_mode_high_resolution": "Hohe Auflösung",
        "adc_timing_mode_high_speed": "Hohe Geschwindigkeit",
        "adc_timing_mode_50hz": "Beste 50-Hz-Unterdrückung",
        "adc_timing_mode_60hz": "Beste 60-Hz-Unterdrückung",
        "two_point_cal_button": "2-Punkt-Kalibrierung...",
        "two_point_cal_dialog_title": "2-Punkt-Kalibrierung",
        "two_point_cal_hint": (
            "Zwei bekannte Referenzpunkte eingeben (z. B. Eispunkt 0 °C und "
            "Siedepunkt 100 °C) - Skalierung und Offset werden daraus "
            "automatisch berechnet."
        ),
        "cal_point1_measured_label": "Punkt 1 - gemessener Wert:",
        "cal_point1_reference_label": "Punkt 1 - bekannter Sollwert:",
        "cal_point2_measured_label": "Punkt 2 - gemessener Wert:",
        "cal_point2_reference_label": "Punkt 2 - bekannter Sollwert:",
        "cal_identical_points_error": "Die beiden gemessenen Werte dürfen nicht identisch sein.",
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
        "close_button": "Schließen",

        # Sensor-Datenbank
        "sensor_database_dialog_title": "Sensor-Datenbank",
        "add_sensor_button": "Sensor hinzufügen",
        "remove_sensor_button": "Sensor entfernen",
        "sensor_category_label": "Kategorie:",
        "sensor_uncategorized_label": "Unkategorisiert",
        "sensor_name_label": "Name:",
        "sensor_manufacturer_label": "Hersteller:",
        "sensor_serial_label": "Seriennummer:",
        "sensor_notes_label": "Notizen:",
        "new_sensor_default_name": "Neuer Sensor",
        "new_axis_default_label": "Neuer Messkanal",
        "add_axis_button": "Messkanal hinzufügen",
        "remove_axis_button": "Messkanal entfernen",
        "add_range_button": "Messbereich-Variante hinzufügen",
        "remove_range_button": "Variante entfernen",
        "confirm_delete_sensor_body": "Sensor '{name}' wirklich löschen?",
        "range_minimum_one_body": (
            "Ein Messkanal muss mindestens eine Messbereich-Variante haben. "
            "Zum Entfernen stattdessen den ganzen Messkanal löschen."
        ),
        "sensor_col_axis": "Messkanal",
        "sensor_col_signal_type": "Signaltyp",
        "sensor_col_range": "Messbereich",
        "sensor_col_sensitivity": "Sensitivitätswert",
        "sensor_db_edit_button": "Bearbeiten",
        "sensor_db_relock_button": "Sperren",
        "sensor_db_locked_status": "Gesperrt (schreibgeschützt)",
        "sensor_db_unlocked_status": "Entsperrt",
        "sensor_db_enter_password_title": "Datenbank entsperren",
        "sensor_db_enter_password_body": "Passwort eingeben:",
        "sensor_db_wrong_password_body": "Falsches Passwort.",

        # Status
        "ready": "Bereit",
        "measurement_running": "Messung läuft ...",
        "measurement_running_named": "Messung '{name}' läuft ...",
        "measurement_completed": "Messung abgeschlossen",
        "measurement_completed_named": "Messung '{name}' abgeschlossen ({duration} s)",
        "no_devices_found": "Keine Geräte gefunden (Treiber installiert? Hardware angeschlossen? Hardware im Treiber für diesen PC reserviert?)",
        "devices_found": "Gerät(e) erkannt",
        "searching_devices": "Suche läuft...",
        "device_discovery_failed": "Geräteerkennung fehlgeschlagen",

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
        "window_title": "ezDAQ - Easy Data Acquisition",
        "about_title": "Über ezDAQ",
        "about_body": (
            "ezDAQ - Easy Data Acquisition\n"
            "Version 1.0\n\n"
            "Anwendung zur Messdatenerfassung, Visualisierung und Analyse "
            "mit NI cDAQ (NI 9215 / NI 9234 / NI 9210 / NI 9213).\n\n"
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
        "menu_sensor_database": "Sensor Database...",

        # Navigation
        "nav_setup": "Configuration",
        "nav_live_view": "Live View",

        # Setup View
        "connected_devices": "Connected Devices",
        "search_devices": "Search Devices",
        "open_ni_max_button": "Open NI-MAX",
        "ni_max_open_failed": "Could not open NI-MAX",
        "channel_configuration": "Channel Configuration",
        "measurement_settings": "Measurement Settings",
        "storage_settings": "Storage Settings",
        "measurement_name": "File Name",
        "sample_rate_hz": "Sample Rate [Hz]",
        "menu_trigger_settings": "Trigger Settings...",
        "trigger_settings_dialog_title": "Trigger Settings",
        "trigger_start_section_title": "Start",
        "trigger_stop_section_title": "Stop",
        "trigger_mode_label": "Mode",
        "trigger_mode_manual": "Manual",
        "trigger_mode_threshold": "Threshold",
        "trigger_mode_serial": "Serial (USB)",
        "trigger_channel_label": "Channel",
        "trigger_threshold_value_label": "Threshold Value",
        "trigger_direction_label": "Direction",
        "trigger_direction_rises_above": "Rises above threshold",
        "trigger_direction_falls_below": "Falls below threshold",
        "trigger_direction_abs_exceeds": "Magnitude exceeds threshold",
        "trigger_pretrigger_seconds_label": "Pre-Trigger Time [s]",
        "trigger_serial_port_label": "COM Port",
        "trigger_serial_refresh_button": "Refresh",
        "trigger_serial_baud_label": "Baud Rate",
        "trigger_serial_message_label": "Expected Signal",
        "trigger_test_group_title": "Test Serial Trigger",
        "trigger_test_source_label": "Trigger",
        "trigger_test_source_start": "Start Trigger",
        "trigger_test_source_stop": "Stop Trigger",
        "trigger_test_start_button": "Start Test",
        "trigger_test_stop_button": "Stop Test",
        "trigger_test_matched_log": "Signal matched (trigger)!",
        "error_trigger_channel_not_active": "Please select an active channel for the threshold trigger.",
        "error_trigger_serial_no_port": "Please select a COM port for the serial trigger.",
        "error_trigger_serial_empty_message": "Please enter an expected signal for the serial trigger.",
        "error_trigger_serial_invalid_baud": "Invalid baud rate for the serial trigger.",
        "armed_waiting_threshold": "Armed - waiting for threshold trigger on channel {channel} (threshold: {threshold})...",
        "armed_waiting_serial": "Armed - waiting for serial trigger signal on {port}...",
        "measurement_armed_status": "Armed: {name} - waiting for trigger",
        "trigger_connection_failed_title": "Serial Trigger Failed",
        "trigger_stop_connection_failed_title": "Stop Trigger Unavailable",
        "error_stop_trigger_connection_failed": "The stop trigger could not connect ({message}). The measurement keeps running - stop it manually or via the recording limit.",
        "storage_format": "Storage Format",
        "storage_format_parquet": "Parquet (recommended)",
        "storage_format_csv": "CSV",
        "live_only": "Live View Only (do not save)",
        "recording_unlimited": "Unlimited (until disk is full)",
        "recording_limit_label": "Recording Limit",
        "recording_stop_unit_samples": "Samples",
        "recording_stop_unit_seconds": "Seconds",
        "recording_stop_unit_minutes": "Minutes",
        "recording_stop_unit_hours": "Hours",
        "storage_location": "Storage Location",
        "choose_storage_location": "Choose Storage Location",
        "start_measurement": "Start Measurement",
        "no_storage_location": "No Storage Location Selected",
        "error_no_active_channels": "Please configure at least one active channel.",
        "error_ni9210_fixed_sample_rate": "The NI9210 only supports {rate} S/s.",
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
        "plot_visible_checkbox": "Active",
        "plot_visible_checkbox_tooltip": (
            "Disabled channels are no longer plotted (neither in the main "
            "window nor in a popout window) - recording itself is not "
            "affected."
        ),
        "popout_button": "Popout Window",
        "popout_button_tooltip": (
            "Shows this channel in its own, freely movable window instead of "
            "the main grid - prevents duplicate display."
        ),
        "axis_time": "Time",
        "storage_detail": "File: {file_size} — Backlog: {pending} / {capacity} Samples ({percent} %)",

        # Analysis View
        "analysis": "Analysis",
        "drag_drop_files": "Drag measurement files (.parquet/.csv) here or use File → Load Measurement",
        "load_measurement": "Load Measurement",
        "loaded_files_channels": "Loaded Files / Channels",
        "search_files_placeholder": "Search files/channels...",
        "search_no_results": "No matches for this search.",
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
        "analysis_category_spectral": "Spectral Analysis",
        "analysis_category_filter": "Filter",
        "analysis_fft_button": "FFT",
        "analysis_fft_tooltip": "Compute the frequency spectrum of a channel",
        "analysis_lowpass_button": "Lowpass",
        "analysis_lowpass_tooltip": "Apply a lowpass filter to a channel",
        "analysis_highpass_button": "Highpass",
        "analysis_highpass_tooltip": "Apply a highpass filter to a channel",
        "analysis_smoothing_button": "Smoothing",
        "analysis_smoothing_tooltip": "Apply a moving average to a channel",
        "analysis_function_dialog_title_fft": "FFT - Select Channel",
        "analysis_function_dialog_title_lowpass": "Lowpass Filter - Select Channel",
        "analysis_function_dialog_title_highpass": "Highpass Filter - Select Channel",
        "analysis_function_dialog_title_smoothing": "Smoothing Filter - Select Channel",
        "analysis_select_channel": "Channel:",
        "analysis_cutoff_frequency": "Cutoff Frequency (Hz):",
        "analysis_window_size": "Window Size (Samples):",
        "analysis_run_button": "Run",
        "analysis_no_channels_available": "No channels are loaded to analyze. Please load a measurement file first.",
        "analysis_no_channels_title": "No Channels Loaded",
        "analysis_error_title": "Analysis Error",
        "analysis_error_body": "The analysis could not be performed:\n{error}",
        "analysis_no_sample_rate_body": (
            "Could not determine a sample rate for this channel "
            "(neither from the metadata nor from the time series)."
        ),
        "analysis_fft_result_suffix": "FFT",
        "analysis_lowpass_result_suffix": "Lowpass",
        "analysis_highpass_result_suffix": "Highpass",
        "analysis_smoothing_result_suffix": "Smoothed",
        "axis_frequency": "Frequency",
        "save_as_action": "Save as...",
        "save_as_csv_action": "Save as CSV...",
        "save_as_parquet_action": "Save as Parquet...",
        "remove_result_action": "Remove Analysis Result",
        "remove_channel_action": "Remove Channel from Analysis",
        "confirm_delete_title": "Confirm Deletion",
        "delete_action": "Delete",
        "confirm_remove_file_body": "Do you really want to remove the file '{name}' from the analysis?",
        "confirm_remove_channel_body": "Do you really want to remove the channel '{name}'?",
        "save_result_csv_filter": "CSV File (*.csv)",
        "save_result_parquet_filter": "Parquet File (*.parquet)",
        "save_result_dialog_title": "Save Analysis Result",
        "save_result_success": "Analysis result saved: {filename}",
        "save_result_error_title": "Error Saving Result",
        "save_result_error_body": "The analysis result could not be saved:\n{error}",

        # Channel Table
        "col_number": "No.",
        "col_active": "Active",
        "col_hw_channel": "Hardware Channel",
        "col_display_name": "Display Name",
        "col_unit": "Unit",
        "col_signal_type": "Signal Type",
        "col_parameters": "Settings",
        "signal_type_voltage": "Voltage",
        "signal_type_iepe": "IEPE Acceleration",
        "signal_type_thermocouple": "Thermocouple",
        "add_channel_button": "Add Channel",
        "remove_channel_button": "Remove Selected Channel",
        "default_channel_name": "Channel {index}",
        "choose_parameters_button": "More Settings",
        "parameters_dialog_title": "Channel Settings",
        "param_scale_label": "Scale Factor:",
        "param_offset_label": "Offset Value:",
        "param_sensitivity_label": "Sensitivity (mV/g):",
        "param_thermocouple_type_label": "Thermocouple Type:",
        "param_adc_timing_mode_label": "ADC Timing Mode:",
        "param_adc_timing_mode_hint": "Applies to all channels of this module (NI9213 only).",
        "adc_timing_mode_automatic": "Automatic",
        "adc_timing_mode_high_resolution": "High Resolution",
        "adc_timing_mode_high_speed": "High Speed",
        "adc_timing_mode_50hz": "Best 50 Hz Rejection",
        "adc_timing_mode_60hz": "Best 60 Hz Rejection",
        "two_point_cal_button": "2-Point Calibration...",
        "two_point_cal_dialog_title": "2-Point Calibration",
        "two_point_cal_hint": (
            "Enter two known reference points (e.g. ice point 0 °C and "
            "boiling point 100 °C) - scale and offset are calculated "
            "automatically from them."
        ),
        "cal_point1_measured_label": "Point 1 - measured value:",
        "cal_point1_reference_label": "Point 1 - known reference:",
        "cal_point2_measured_label": "Point 2 - measured value:",
        "cal_point2_reference_label": "Point 2 - known reference:",
        "cal_identical_points_error": "The two measured values must not be identical.",
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
        "close_button": "Close",

        # Sensor Database
        "sensor_database_dialog_title": "Sensor Database",
        "add_sensor_button": "Add Sensor",
        "remove_sensor_button": "Remove Sensor",
        "sensor_category_label": "Category:",
        "sensor_uncategorized_label": "Uncategorized",
        "sensor_name_label": "Name:",
        "sensor_manufacturer_label": "Manufacturer:",
        "sensor_serial_label": "Serial Number:",
        "sensor_notes_label": "Notes:",
        "new_sensor_default_name": "New Sensor",
        "new_axis_default_label": "New Channel",
        "add_axis_button": "Add Channel",
        "remove_axis_button": "Remove Channel",
        "add_range_button": "Add Range Variant",
        "remove_range_button": "Remove Variant",
        "confirm_delete_sensor_body": "Really delete sensor '{name}'?",
        "range_minimum_one_body": (
            "A channel must have at least one range variant. Remove the "
            "whole channel instead."
        ),
        "sensor_col_axis": "Channel",
        "sensor_col_signal_type": "Signal Type",
        "sensor_col_range": "Range",
        "sensor_col_sensitivity": "Sensitivity Value",
        "sensor_db_edit_button": "Edit",
        "sensor_db_relock_button": "Lock",
        "sensor_db_locked_status": "Locked (read-only)",
        "sensor_db_unlocked_status": "Unlocked",
        "sensor_db_enter_password_title": "Unlock Database",
        "sensor_db_enter_password_body": "Enter password:",
        "sensor_db_wrong_password_body": "Incorrect password.",

        # Status
        "ready": "Ready",
        "measurement_running": "Measurement running ...",
        "measurement_running_named": "Measurement '{name}' running ...",
        "measurement_completed": "Measurement completed",
        "measurement_completed_named": "Measurement '{name}' completed ({duration} s)",
        "no_devices_found": "No devices found (Drivers installed? Hardware connected? Hardware reserved for this PC in the driver?)",
        "devices_found": "Device(s) found",
        "searching_devices": "Searching...",
        "device_discovery_failed": "Device discovery failed",

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
        "window_title": "ezDAQ - Easy Data Acquisition",
        "about_title": "About ezDAQ",
        "about_body": (
            "ezDAQ - Easy Data Acquisition\n"
            "Version 1.0\n\n"
            "Application for data acquisition, visualization, and analysis "
            "with NI cDAQ (NI 9215 / NI 9234 / NI 9210 / NI 9213).\n\n"
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
