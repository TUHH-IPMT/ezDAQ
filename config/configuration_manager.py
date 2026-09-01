"""
config/configuration_manager.py

Central access point for persistent application configuration.

The GUI and core layers access stored settings exclusively through the
`ConfigurationManager` - never directly via file paths or `json` calls.
This keeps the persistence logic testable in one place, replaceable
(e.g. with a database later), and robust against corrupted/missing
configuration files.
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
from config.json_helpers import load_json_list
from data.models import Channel, MeasurementConfig, TriggerConfig

logger = logging.getLogger(__name__)


class ConfigurationManager:
    """Loads, manages, and saves the persistent application configuration.

    Responsibilities:
        * Loading/saving the general `AppSettings` (window geometry,
          last used project/device, defaults for new measurements).
        * Loading/saving the last used channel configuration
          (list of `Channel` objects), independent of a specific
          measurement project - e.g. to suggest the same channel
          assignment to the user on the next start.

    Error handling:
        If a configuration file is missing or corrupted, this is logged
        and reasonable defaults are used instead - the application
        always starts, even with a corrupted configuration.
    """

    def __init__(self, config_dir: Optional[Path] = None) -> None:
        """Initializes the ConfigurationManager.

        Args:
            config_dir: Optional, explicit configuration directory.
                Mainly used for tests, to avoid writing to the real
                user directory. In normal operation,
                `get_config_directory()` is used.
        """
        self._config_dir = config_dir or get_config_directory()
        self._config_dir.mkdir(parents=True, exist_ok=True)

        self._settings_path = self._config_dir / CONFIG_FILE_NAME
        self._channel_config_path = self._config_dir / CHANNEL_CONFIG_FILE_NAME

        self._settings: AppSettings = self._load_settings()

    # ------------------------------------------------------------------ #
    # General settings
    # ------------------------------------------------------------------ #

    @property
    def settings(self) -> AppSettings:
        """Returns the currently loaded settings."""
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
        """Saves the current settings to disk."""
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
        """Updates and saves the window geometry.

        Called by `gui/main_window.py`, e.g. in `closeEvent`, so the
        application restores size and position on the next start.
        """
        self._settings.window.width = width
        self._settings.window.height = height
        self._settings.window.pos_x = pos_x
        self._settings.window.pos_y = pos_y
        self._settings.window.maximized = maximized
        self.save_settings()

    def update_last_project_path(self, path: str) -> None:
        """Remembers the path of the last opened measurement project."""
        self._settings.last_project_path = path
        self.save_settings()

    def update_last_storage_path(self, path: str) -> None:
        """Remembers the last used storage directory."""
        self._settings.last_storage_path = path
        self.save_settings()

    def update_live_view_plot_columns(self, columns: int) -> None:
        """Remembers how many channels the live view places side by
        side (see `gui/live_view.py::LiveView.set_plot_columns`)."""
        self._settings.live_view_plot_columns = max(1, int(columns))
        self.save_settings()

    def update_last_device_name(self, device_name: str) -> None:
        """Remembers the name of the last used NI cDAQ device."""
        self._settings.last_device_name = device_name
        self.save_settings()

    def update_language(self, language: str) -> None:
        """Remembers the last selected UI language."""
        self._settings.language = language
        self.save_settings()

    def update_theme(self, theme: str) -> None:
        """Remembers the last selected color theme."""
        self._settings.theme = theme
        self.save_settings()

    def update_naming_scheme(
        self,
        use_number_suffix: bool,
        number_suffix_digits: int,
        include_date: bool,
        include_time: bool,
    ) -> None:
        """Remembers the last selected naming scheme for new measurements.

        Seeds the setup view's checkboxes when no configuration has been
        loaded. NOT a source for the naming actually applied - that
        comes from `MeasurementConfig.naming` (see `data/models.py`), so
        that a configuration always names its files the same way, no
        matter which machine or user it runs on.
        """
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
        recording_unlimited: bool = True,
        recording_stop_value: float = 0.0,
        recording_stop_unit: str = "samples",
    ) -> None:
        """Saves the last used measurement parameters.

        These values are automatically pre-filled as measurement
        parameters in the setup view on the next app start.
        """
        self._settings.last_measurement_name = measurement_name
        self._settings.default_sample_rate_hz = sample_rate_hz
        self._settings.default_storage_format = storage_format
        self._settings.last_recording_unlimited = recording_unlimited
        self._settings.last_recording_stop_value = recording_stop_value
        self._settings.last_recording_stop_unit = recording_stop_unit
        self.save_settings()

    def update_last_trigger_settings(self, trigger: TriggerConfig) -> None:
        """Remembers the last used measurement trigger configuration for
        both start AND stop (see `data/models.py::TriggerConfig`) - as
        with `update_last_measurement_parameters`, these values are
        automatically pre-filled on the next app start."""
        self._settings.last_trigger_config = trigger.to_dict()
        self.save_settings()

    # ------------------------------------------------------------------ #
    # Channel configuration
    # ------------------------------------------------------------------ #

    def save_channel_configuration(self, channels: list[Channel]) -> None:
        """Saves the current channel configuration as a JSON file.

        Args:
            channels: List of channels to save (setup view).
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
        """Loads the last saved channel configuration.

        Returns:
            List of the saved channels, or an empty list if no file
            exists or it is not readable.
        """
        if not self._channel_config_path.exists():
            logger.info(
                "Keine gespeicherte Kanalkonfiguration gefunden: %s",
                self._channel_config_path,
            )
            return []

        return load_json_list(
            self._channel_config_path,
            Channel.from_dict,
            logger,
        )

    # ------------------------------------------------------------------ #
    # Saved measurement configurations
    # ------------------------------------------------------------------ #

    def save_measurement_config(self, config: MeasurementConfig, file_path: Path) -> None:
        """Saves a measurement configuration to a path chosen by the user.

        The save location is deliberately provided by the caller (GUI,
        via a file dialog) instead of being managed internally -
        configurations are therefore normal files that the user can
        freely place, rename, share, or delete.

        Raises:
            OSError: if the file cannot be written. The error is
                deliberately NOT swallowed, so that an explicit "Save"
                click in the GUI can trigger a visible error message.
        """
        with file_path.open("w", encoding="utf-8") as f:
            json.dump(config.to_dict(), f, indent=2, ensure_ascii=False)
        logger.debug("Messkonfiguration '%s' gespeichert: %s", config.name, file_path)

    def load_measurement_config(self, file_path: Path) -> Optional[MeasurementConfig]:
        """Loads a previously saved measurement configuration from a file path.

        Returns:
            The loaded configuration, or None if the file does not
            exist or is not readable.
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
