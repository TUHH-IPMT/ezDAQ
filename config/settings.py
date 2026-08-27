"""
config/settings.py

Defines storage locations and default values for the persistent
application configuration (window geometry, last used hardware,
user settings).

This module deliberately contains no loading/saving logic - that is
handled by `config/configuration_manager.py`. This file contains only
paths and plain data structures.
"""

from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

APP_NAME = "ezDAQ"

# Single source of truth for the version. Shown in the About dialog
# (see `gui/main_window.py::_on_about`) and mirrored by the installer
# (`packaging/ezDAQ.iss`), which cannot import Python - a test keeps
# the two from drifting apart (`tests/test_version.py`).
APP_VERSION = "0.1"

CONFIG_FILE_NAME = "settings.json"
CHANNEL_CONFIG_FILE_NAME = "last_channel_configuration.json"
# Sensor catalog (see config/sensor_database.py) - deliberately a
# separate file, completely independent of measurement/channel
# configurations (see data/sensor_models.py module doc).
SENSOR_DATABASE_FILE_NAME = "sensor_database.json"


def get_resource_path(*parts: str) -> Path:
    """Returns the path to a bundled resource file (e.g. an icon).

    Works both in development (``python main.py``, in which case the
    base is the project directory) and in a PyInstaller-packaged
    application: in "onefile" mode, files bundled with ``--add-data``
    live in the temporary extraction directory (``sys._MEIPASS``), in
    "onedir" mode (see README) next to the ``.exe`` (``sys.executable``).
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).parent)
    else:
        base = Path(__file__).resolve().parent.parent
    return base.joinpath("resources", *parts)


def get_config_directory() -> Path:
    """Returns the platform-specific directory for configuration files.

    On Windows, ``%APPDATA%/ezDAQ`` is used, since this is the standard
    location for user-specific application data. On other platforms
    (development/test environment), ``~/.config/ezDAQ`` is used as a
    fallback, so main.py remains runnable there as well.
    """
    appdata = os.environ.get("APPDATA")
    if appdata:
        base = Path(appdata)
    else:
        base = Path.home() / ".config"
    return base / APP_NAME


@dataclass
class WindowGeometry:
    """Persisted window geometry of the main window."""

    width: int = 1400
    height: int = 900
    pos_x: int = 100
    pos_y: int = 100
    maximized: bool = False


@dataclass
class AppSettings:
    """Persistent user settings of the application.

    Attributes:
        window: Last used window geometry.
        last_project_path: Path of the last opened measurement project.
        last_device_name: Name of the last used NI cDAQ device.
        default_sample_rate_hz: Default sample rate for new measurements.
        default_storage_format: Default storage format
            (value of ``data.models.StorageFormat``, e.g. "parquet").
        last_measurement_name: Last used measurement name.
        language: Last selected UI language ("de"/"en").
        theme: Last selected color theme ("light"/"dark").
        name_use_number_suffix: Whether a number suffix is automatically
            appended to the measurement name when a measurement starts.
        name_number_suffix_digits: Number of digits of the number suffix (e.g. 3 -> "_001").
        name_include_date: Whether the current date is included in the measurement name.
        name_include_time: Whether the current time is included in the measurement name.
        last_recording_unlimited: Last selection for "Unlimited (until
            storage is full)" - see
            `data.models.MeasurementConfig.recording_unlimited`.
        last_recording_stop_value: Last entered limit value (see
            `data.models.MeasurementConfig.recording_stop_value`).
        last_recording_stop_unit: Last selected unit (value of
            `data.models.RecordingStopUnit`, e.g. "samples").
        last_trigger_config: Last used trigger configuration for both
            start AND stop, as raw data from
            `data.models.TriggerConfig.to_dict()` (analogous to `window`
            above - stored directly as a dict instead of its own flat
            fields, since `TriggerConfig` itself is already nested).
    """

    window: WindowGeometry = field(default_factory=WindowGeometry)
    last_project_path: Optional[str] = None
    last_storage_path: Optional[str] = None
    last_device_name: Optional[str] = None
    default_sample_rate_hz: float = 1000.0
    default_storage_format: str = "parquet"
    last_measurement_name: str = "Messung"
    language: str = "de"
    theme: str = "light"
    name_use_number_suffix: bool = True
    name_number_suffix_digits: int = 3
    name_include_date: bool = False
    name_include_time: bool = False
    last_recording_unlimited: bool = True
    last_recording_stop_value: float = 0.0
    last_recording_stop_unit: str = "samples"
    last_trigger_config: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serializes the settings into a JSON-compatible dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppSettings":
        """Creates AppSettings from a dictionary, robust against missing fields.

        Missing or unknown fields fall back to the default values, so
        that older or incomplete configuration files do not cause a
        crash.
        """
        window_data = data.get("window", {}) or {}
        window = WindowGeometry(
            width=window_data.get("width", 1400),
            height=window_data.get("height", 900),
            pos_x=window_data.get("pos_x", 100),
            pos_y=window_data.get("pos_y", 100),
            maximized=window_data.get("maximized", False),
        )
        return cls(
            window=window,
            last_project_path=data.get("last_project_path"),
            last_storage_path=data.get("last_storage_path"),
            last_device_name=data.get("last_device_name"),
            default_sample_rate_hz=data.get("default_sample_rate_hz", 1000.0),
            default_storage_format=data.get("default_storage_format", "parquet"),
            last_measurement_name=data.get("last_measurement_name", "Messung"),
            language=data.get("language", "de"),
            theme=data.get("theme", "light"),
            name_use_number_suffix=data.get("name_use_number_suffix", True),
            name_number_suffix_digits=data.get("name_number_suffix_digits", 3),
            name_include_date=data.get("name_include_date", False),
            name_include_time=data.get("name_include_time", False),
            last_recording_unlimited=data.get("last_recording_unlimited", True),
            last_recording_stop_value=data.get("last_recording_stop_value", 0.0),
            last_recording_stop_unit=data.get("last_recording_stop_unit", "samples"),
            last_trigger_config=data.get("last_trigger_config", {}) or {},
        )
