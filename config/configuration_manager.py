"""
config/configuration_manager.py

Zentrale Zugriffsstelle für persistente Anwendungskonfiguration.

Die GUI- und Core-Schicht greifen ausschließlich über den
`ConfigurationManager` auf gespeicherte Einstellungen zu - niemals direkt
über Dateipfade oder `json`-Aufrufe. Das hält die Persistenzlogik an einer
Stelle testbar, austauschbar (z. B. später gegen eine Datenbank) und
robust gegen fehlerhafte/fehlende Konfigurationsdateien.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from config.settings import (
    AppSettings,
    CHANNEL_CONFIG_FILE_NAME,
    CONFIG_FILE_NAME,
    get_config_directory,
)
from data.models import Channel, MeasurementConfig

logger = logging.getLogger(__name__)


class ConfigurationManager:
    """Lädt, verwaltet und speichert die persistente Anwendungskonfiguration.

    Verantwortlichkeiten:
        * Laden/Speichern der allgemeinen `AppSettings` (Fenstergeometrie,
          zuletzt verwendetes Projekt/Gerät, Defaults für neue Messungen).
        * Laden/Speichern der zuletzt verwendeten Kanalkonfiguration
          (Liste von `Channel`-Objekten), unabhängig von einem konkreten
          Messprojekt - z. B. um dem Nutzer beim nächsten Start dieselbe
          Kanalbelegung vorzuschlagen.

    Fehlerverhalten:
        Fehlt eine Konfigurationsdatei oder ist sie fehlerhaft, wird dies
        geloggt und auf sinnvolle Defaults zurückgefallen - die Anwendung
        startet in jedem Fall, auch bei einer korrupten Konfiguration.
    """

    def __init__(self, config_dir: Optional[Path] = None) -> None:
        """Initialisiert den ConfigurationManager.

        Args:
            config_dir: Optionales, explizites Konfigurationsverzeichnis.
                Wird v. a. für Tests verwendet, um nicht das reale
                Benutzerverzeichnis zu beschreiben. Im Normalbetrieb wird
                `get_config_directory()` verwendet.
        """
        self._config_dir = config_dir or get_config_directory()
        self._config_dir.mkdir(parents=True, exist_ok=True)

        self._settings_path = self._config_dir / CONFIG_FILE_NAME
        self._channel_config_path = self._config_dir / CHANNEL_CONFIG_FILE_NAME

        self._settings: AppSettings = self._load_settings()

    # ------------------------------------------------------------------ #
    # Allgemeine Einstellungen
    # ------------------------------------------------------------------ #

    @property
    def settings(self) -> AppSettings:
        """Gibt die aktuell geladenen Einstellungen zurück."""
        return self._settings

    def _load_settings(self) -> AppSettings:
        if not self._settings_path.exists():
            logger.info(
                "Keine bestehende Konfiguration gefunden, verwende Defaults (%s)",
                self._settings_path,
            )
            return AppSettings()

        try:
            with self._settings_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return AppSettings.from_dict(data)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Konfigurationsdatei konnte nicht gelesen werden (%s), "
                "verwende Defaults: %s",
                self._settings_path,
                exc,
            )
            return AppSettings()

    def save_settings(self) -> None:
        """Speichert die aktuellen Einstellungen auf die Festplatte."""
        try:
            with self._settings_path.open("w", encoding="utf-8") as f:
                json.dump(self._settings.to_dict(), f, indent=2, ensure_ascii=False)
            logger.debug("Einstellungen gespeichert: %s", self._settings_path)
        except OSError as exc:
            logger.error("Einstellungen konnten nicht gespeichert werden: %s", exc)

    def update_window_geometry(
        self,
        width: int,
        height: int,
        pos_x: int,
        pos_y: int,
        maximized: bool = False,
    ) -> None:
        """Aktualisiert und speichert die Fenstergeometrie.

        Wird von `gui/main_window.py` z. B. im `closeEvent` aufgerufen,
        damit die Anwendung beim nächsten Start Größe und Position wiederherstellt.
        """
        self._settings.window.width = width
        self._settings.window.height = height
        self._settings.window.pos_x = pos_x
        self._settings.window.pos_y = pos_y
        self._settings.window.maximized = maximized
        self.save_settings()

    def update_last_project_path(self, path: str) -> None:
        """Merkt sich den Pfad des zuletzt geöffneten Messprojekts."""
        self._settings.last_project_path = path
        self.save_settings()

    def update_last_storage_path(self, path: str) -> None:
        """Merkt sich das zuletzt verwendete Speicherverzeichnis."""
        self._settings.last_storage_path = path
        self.save_settings()

    def update_last_device_name(self, device_name: str) -> None:
        """Merkt sich den Namen des zuletzt verwendeten NI-cDAQ-Geräts."""
        self._settings.last_device_name = device_name
        self.save_settings()

    def update_language(self, language: str) -> None:
        """Merkt sich die zuletzt gewählte UI-Sprache."""
        self._settings.language = language
        self.save_settings()

    def update_theme(self, theme: str) -> None:
        """Merkt sich das zuletzt gewählte Farbschema."""
        self._settings.theme = theme
        self.save_settings()

    def update_naming_scheme(
        self,
        use_number_suffix: bool,
        number_suffix_digits: int,
        include_date: bool,
        include_time: bool,
    ) -> None:
        """Merkt sich das zuletzt gewählte Namensschema für neue Messungen."""
        self._settings.name_use_number_suffix = use_number_suffix
        self._settings.name_number_suffix_digits = number_suffix_digits
        self._settings.name_include_date = include_date
        self._settings.name_include_time = include_time
        self.save_settings()

    def update_last_measurement_parameters(
        self,
        measurement_name: str,
        sample_rate_hz: float,
        storage_format: str,
        live_only: bool,
        recording_unlimited: bool = True,
        recording_stop_value: float = 0.0,
        recording_stop_unit: str = "samples",
    ) -> None:
        """Speichert die zuletzt verwendeten Messparameter.

        Diese Werte werden beim naechsten App-Start in der Setup-Ansicht
        automatisch als Messparameter vorbelegt.
        """
        self._settings.last_measurement_name = measurement_name
        self._settings.default_sample_rate_hz = sample_rate_hz
        self._settings.default_storage_format = storage_format
        self._settings.last_live_only = live_only
        self._settings.last_recording_unlimited = recording_unlimited
        self._settings.last_recording_stop_value = recording_stop_value
        self._settings.last_recording_stop_unit = recording_stop_unit
        self.save_settings()

    # ------------------------------------------------------------------ #
    # Kanalkonfiguration
    # ------------------------------------------------------------------ #

    def save_channel_configuration(self, channels: list[Channel]) -> None:
        """Speichert die aktuelle Kanalkonfiguration als JSON-Datei.

        Args:
            channels: Liste der zu speichernden Kanäle (Setup-Ansicht).
        """
        try:
            data = [ch.to_dict() for ch in channels]
            with self._channel_config_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.debug(
                "Kanalkonfiguration gespeichert (%d Kanäle): %s",
                len(channels),
                self._channel_config_path,
            )
        except OSError as exc:
            logger.error(
                "Kanalkonfiguration konnte nicht gespeichert werden: %s", exc
            )

    def load_channel_configuration(self) -> list[Channel]:
        """Lädt die zuletzt gespeicherte Kanalkonfiguration.

        Returns:
            Liste der gespeicherten Kanäle, oder eine leere Liste, falls
            keine Datei existiert oder diese nicht lesbar ist.
        """
        if not self._channel_config_path.exists():
            logger.info(
                "Keine gespeicherte Kanalkonfiguration gefunden: %s",
                self._channel_config_path,
            )
            return []

        try:
            with self._channel_config_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return [Channel.from_dict(item) for item in data]
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            logger.warning(
                "Kanalkonfiguration konnte nicht geladen werden, "
                "ignoriere Datei: %s",
                exc,
            )
            return []

    # ------------------------------------------------------------------ #
    # Gespeicherte Messkonfigurationen
    # ------------------------------------------------------------------ #

    def save_measurement_config(self, config: MeasurementConfig, file_path: Path) -> None:
        """Speichert eine Messkonfiguration unter einem vom Nutzer gewählten Pfad.

        Der Speicherort wird bewusst vom Aufrufer (GUI, per Dateidialog)
        vorgegeben statt intern verwaltet zu werden - Konfigurationen sind
        damit normale Dateien, die der Nutzer frei ablegen, umbenennen,
        teilen oder löschen kann.

        Raises:
            OSError: falls die Datei nicht geschrieben werden kann. Der
                Fehler wird bewusst NICHT verschluckt, damit ein expliziter
                "Speichern"-Klick in der GUI eine sichtbare Fehlermeldung
                auslösen kann.
        """
        with file_path.open("w", encoding="utf-8") as f:
            json.dump(config.to_dict(), f, indent=2, ensure_ascii=False)
        logger.debug("Messkonfiguration '%s' gespeichert: %s", config.name, file_path)

    def load_measurement_config(self, file_path: Path) -> Optional[MeasurementConfig]:
        """Lädt eine zuvor gespeicherte Messkonfiguration von einem Dateipfad.

        Returns:
            Die geladene Konfiguration, oder None falls die Datei nicht
            existiert oder nicht lesbar ist.
        """
        if not file_path.exists():
            logger.warning("Messkonfigurationsdatei nicht gefunden: %s", file_path)
            return None
        try:
            with file_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return MeasurementConfig.from_dict(data)
        except (json.JSONDecodeError, OSError, KeyError, ValueError) as exc:
            logger.warning(
                "Messkonfiguration konnte nicht geladen werden (%s): %s", file_path, exc
            )
            return None
