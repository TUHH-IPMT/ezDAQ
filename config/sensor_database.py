"""
config/sensor_database.py

Persistenz für den Sensor-Katalog (siehe data/sensor_models.py) - analog
zu config/configuration_manager.py, aber bewusst in einer EIGENEN Datei
gespeichert, komplett getrennt von Mess-/Kanalkonfigurationen (siehe
data/sensor_models.py Moduldoc).

Der Schreibschutz gegen versehentliche Änderungen (festes Passwort) lebt
bewusst NICHT hier, sondern direkt in `gui/sensor_database_dialog.py` -
dieser Manager ist reine Persistenz, ohne Kenntnis von Zugriffsschutz.
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
    """Lädt, verwaltet und speichert den Sensor-Katalog.

    Anders als `ConfigurationManager`s Kanalkonfiguration (die explizit
    per "Speichern" geschrieben wird) speichert jede ändernde Methode
    hier SOFORT auf die Festplatte - der Katalog verhält sich damit eher
    wie eine kleine, dauerhaft gepflegte Datenbank als wie ein Formular
    mit OK/Abbrechen (siehe gui/sensor_database_dialog.py).

    Fehlerverhalten: wie beim `ConfigurationManager` wird eine fehlende
    oder fehlerhafte Datei geloggt und als leerer Katalog behandelt - die
    Anwendung startet in jedem Fall.
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
        """Gibt alle Sensoren zurück, nach Name sortiert."""
        return sorted(self._sensors, key=lambda s: s.name.lower())

    def list_categories(self) -> list[str]:
        """Gibt alle aktuell verwendeten Kategorienamen zurück (sortiert,
        ohne Duplikate/Leerstrings) - für die Autovervollständigung in
        `gui/sensor_database_dialog.py`, damit keine Tippfehler-Duplikate
        entstehen (z. B. "Kraft" vs. "Kraftmessung")."""
        return sorted({s.category for s in self._sensors if s.category}, key=str.lower)

    def get_sensor(self, sensor_id: str) -> Optional[SensorEntry]:
        return next((s for s in self._sensors if s.id == sensor_id), None)

    def add_sensor(self, sensor: SensorEntry) -> None:
        self._sensors.append(sensor)
        self._save()

    def update_sensor(self, sensor: SensorEntry) -> None:
        """Ersetzt den bestehenden Sensor mit derselben `id` durch `sensor`.

        Unbekannte `id` (sollte im Normalfall nicht vorkommen) wird als
        neuer Eintrag behandelt, statt den Aufruf zu verwerfen.
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
