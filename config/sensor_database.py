"""
config/sensor_database.py

Persistence for the sensor catalog (see data/sensor_models.py) -
analogous to config/configuration_manager.py, but deliberately stored
in its OWN file, completely separate from measurement/channel
configurations (see data/sensor_models.py module doc).

The write protection against accidental changes (fixed password)
deliberately does NOT live here, but directly in
`gui/sensor_database_dialog.py` - this manager is pure persistence,
with no knowledge of access protection.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from config.settings import SENSOR_DATABASE_FILE_NAME, get_config_directory
from config.json_helpers import load_json_list
from data.sensor_models import SensorEntry

logger = logging.getLogger(__name__)


class SensorDatabaseManager:
    """Loads, manages, and saves the sensor catalog.

    Unlike `ConfigurationManager`'s channel configuration (which is
    written explicitly via "Save"), every mutating method here saves
    IMMEDIATELY to disk - the catalog therefore behaves more like a
    small, permanently maintained database than a form with OK/Cancel
    (see gui/sensor_database_dialog.py).

    Error handling: as with `ConfigurationManager`, a missing or
    corrupted file is logged and treated as an empty catalog - the
    application always starts.
    """

    def __init__(self, config_dir: Optional[Path] = None) -> None:
        self._config_dir = config_dir or get_config_directory()
        self._config_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._config_dir / SENSOR_DATABASE_FILE_NAME
        self._sensors: list[SensorEntry] = self._load()

    def _load(self) -> list[SensorEntry]:
        if not self._path.exists():
            logger.info("Keine bestehende Sensor-Datenbank gefunden: %s", self._path)
            return []
        return load_json_list(
            self._path,
            SensorEntry.from_dict,
            logger,
            list_key="sensors",
        )

    def _save(self) -> None:
        try:
            data = [sensor.to_dict() for sensor in self._sensors]
            with self._path.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.debug(
                "Sensor-Datenbank gespeichert (%d Sensoren): %s", len(self._sensors), self._path
            )
        except OSError as exc:
            logger.error("Sensor-Datenbank konnte nicht gespeichert werden: %s", exc)

    def list_sensors(self) -> list[SensorEntry]:
        """Returns all sensors, sorted by name."""
        return sorted(self._sensors, key=lambda s: s.name.lower())

    def list_categories(self) -> list[str]:
        """Returns all currently used category names (sorted, without
        duplicates/empty strings) - for autocompletion in
        `gui/sensor_database_dialog.py`, to avoid typo duplicates
        (e.g. "Force" vs. "Force measurement")."""
        return sorted({s.category for s in self._sensors if s.category}, key=str.lower)

    def get_sensor(self, sensor_id: str) -> Optional[SensorEntry]:
        return next((s for s in self._sensors if s.id == sensor_id), None)

    def add_sensor(self, sensor: SensorEntry) -> None:
        self._sensors.append(sensor)
        self._save()

    def update_sensor(self, sensor: SensorEntry) -> None:
        """Replaces the existing sensor with the same `id` with `sensor`.

        An unknown `id` (should not normally occur) is treated as a new
        entry, instead of discarding the call.
        """
        for index, existing in enumerate(self._sensors):
            if existing.id == sensor.id:
                self._sensors[index] = sensor
                self._save()
                return
        self.add_sensor(sensor)

    def delete_sensor(self, sensor_id: str) -> None:
        self._sensors = [s for s in self._sensors if s.id != sensor_id]
        self._save()
