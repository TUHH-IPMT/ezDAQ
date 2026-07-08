"""
gui/i18n.py

Einfaches Internationalisierungs-System für Deutsch und Englisch.

Verwendung:
    from gui.i18n import set_language, t
    
    set_language("de")  # oder "en"
    label.setText(t("start_measurement"))
"""

from __future__ import annotations

_current_language = "de"

# Übersetzungs-Dictionaries
_translations = {
    "de": {
        # Menu
        "menu_file": "Datei",
        "menu_settings": "Einstellungen",
        "menu_quit": "Beenden",
        "menu_help": "Hilfe",
        "menu_about": "Über...",
        
        # Setup View
        "connected_devices": "Angeschlossene Geräte",
        "search_devices": "Geräte suchen",
        "channel_configuration": "Kanalkonfiguration",
        "measurement_parameters": "Messparameter",
        "measurement_name": "Messungsname",
        "sample_rate_hz": "Abtastrate [Hz]",
        "storage_format": "Speicherformat",
        "live_only": "Nur Live anzeigen (nicht speichern)",
        "storage_location": "Speicherort",
        "choose_storage_location": "Speicherort wählen",
        "start_measurement": "Messung starten",
        "no_storage_location": "Kein Speicherort gewählt",
        "error_no_active_channels": "Bitte mindestens einen aktiven Kanal konfigurieren.",
        "error_no_name": "Bitte einen Namen für die Messung angeben.",
        
        # Live View
        "duration": "Dauer",
        "sample_rate": "Abtastrate",
        "stop_measurement": "Messung stoppen",
        "statistics": "Statistiken (aktuelle 10 s)",
        "channel": "Kanal",
        "min": "Min",
        "max": "Max",
        "rms": "RMS",
        
        # Analysis View
        "analysis": "Analyse",
        "drag_drop_files": "Messdateien hier ziehen...",
        "load_measurement": "Messung laden",
        
        # Settings
        "settings": "Einstellungen",
        "language": "Sprache",
        "german": "Deutsch",
        "english": "English",
        "ok": "OK",
        "cancel": "Abbrechen",
        
        # Status
        "ready": "Bereit",
        "measurement_running": "Messung läuft ...",
        "measurement_completed": "Messung abgeschlossen",
        "no_devices_found": "Keine Geräte gefunden (Treiber installiert? Hardware angeschlossen?)",
        "devices_found": "Gerät(e) erkannt",
        
        # Errors
        "error": "Fehler",
        "measurement_error": "Messfehler",
        "cannot_start_measurement": "Messung konnte nicht gestartet werden",
        "no_storage_selected": "Bitte zuerst einen Ordner wählen",
    },
    "en": {
        # Menu
        "menu_file": "File",
        "menu_settings": "Settings",
        "menu_quit": "Quit",
        "menu_help": "Help",
        "menu_about": "About...",
        
        # Setup View
        "connected_devices": "Connected Devices",
        "search_devices": "Search Devices",
        "channel_configuration": "Channel Configuration",
        "measurement_parameters": "Measurement Parameters",
        "measurement_name": "Measurement Name",
        "sample_rate_hz": "Sample Rate [Hz]",
        "storage_format": "Storage Format",
        "live_only": "Live View Only (do not save)",
        "storage_location": "Storage Location",
        "choose_storage_location": "Choose Storage Location",
        "start_measurement": "Start Measurement",
        "no_storage_location": "No Storage Location Selected",
        "error_no_active_channels": "Please configure at least one active channel.",
        "error_no_name": "Please enter a measurement name.",
        
        # Live View
        "duration": "Duration",
        "sample_rate": "Sample Rate",
        "stop_measurement": "Stop Measurement",
        "statistics": "Statistics (last 10 s)",
        "channel": "Channel",
        "min": "Min",
        "max": "Max",
        "rms": "RMS",
        
        # Analysis View
        "analysis": "Analysis",
        "drag_drop_files": "Drag measurement files here...",
        "load_measurement": "Load Measurement",
        
        # Settings
        "settings": "Settings",
        "language": "Language",
        "german": "Deutsch",
        "english": "English",
        "ok": "OK",
        "cancel": "Cancel",
        
        # Status
        "ready": "Ready",
        "measurement_running": "Measurement running ...",
        "measurement_completed": "Measurement completed",
        "no_devices_found": "No devices found (Drivers installed? Hardware connected?)",
        "devices_found": "Device(s) found",
        
        # Errors
        "error": "Error",
        "measurement_error": "Measurement Error",
        "cannot_start_measurement": "Could not start measurement",
        "no_storage_selected": "Please select a storage location first",
    }
}


def set_language(lang: str) -> None:
    """Setzt die aktuelle Sprache auf 'de' oder 'en'."""
    global _current_language
    if lang in _translations:
        _current_language = lang


def get_language() -> str:
    """Gibt die aktuelle Sprache zurück."""
    return _current_language


def t(key: str) -> str:
    """Übersetzt einen Schlüssel in die aktuelle Sprache.
    
    Fallback auf den Schlüssel selbst, falls nicht gefunden.
    """
    lang_dict = _translations.get(_current_language, {})
    return lang_dict.get(key, key)
