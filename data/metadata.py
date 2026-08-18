"""
data/metadata.py

Aufbau und Persistierung von Metadaten:
    * Metadaten einer einzelnen Messung (measurement_info.json)
    * Bookkeeping eines gesamten Messprojekts (project.json)

Architektur-Hinweis:
    Die ursprüngliche Modul-Skizze sah `metadata.py` nur für
    Mess-Metadaten vor. Projekt-Bookkeeping (Liste der Messungen,
    Projektname, Erstellungsdatum) ist inhaltlich eng verwandt (auch hier
    geht es nur um reine JSON-Persistierung von Metainformationen) und
    wurde daher hier mit untergebracht, statt eine zusätzliche Datei für
    eine sehr kleine Verantwortlichkeit anzulegen. Falls das Projekt
    wächst (z. B. mehrere gleichzeitig geöffnete Projekte), sollte
    `MeasurementProject` in eine eigene Datei ausgelagert werden.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from data.models import Channel, DeviceInfo, MeasurementSession, StorageFormat, resolve_rate_groups

logger = logging.getLogger(__name__)

PROJECT_FILE_NAME = "project.json"
MEASUREMENTS_DIR_NAME = "measurements"
METADATA_DIR_NAME = "metadata"


def build_measurement_metadata(
    session: MeasurementSession,
    device_infos: list[DeviceInfo],
) -> dict[str, Any]:
    """Baut das Metadaten-Dictionary für eine Messung.

    Enthält gemäß Vorgabe: Startzeit, Abtastrate, Hardwareinformationen
    sowie Kanalinformationen inklusive Skalierungen und Einheiten.

    Args:
        session: Die (laufende oder abgeschlossene) Messsitzung.
        device_infos: Geräteinformationen der verwendeten Hardware
            (z. B. `controller.active_device_infos`).
    """
    config = session.config
    # Ratengruppen aus der Konfiguration ableiten (siehe
    # `data/models.py::resolve_rate_groups`) - rein informativ fuer die
    # Metadaten, beeinflusst die Messung selbst nicht mehr (das ist
    # bereits beim Start in `core/controller.py::start_measurement`
    # passiert). "sample_rate_hz" behaelt seine bisherige Bedeutung: die
    # TATSAECHLICHE Tick-Rate der gespeicherten Datei (= schnellste
    # Gruppe), NICHT zwangslaeufig die vom Nutzer eingestellte Zielrate -
    # das ist die Rate, mit der `data/exporter.py::StorageWriter` die
    # `time_s`-Spalte berechnet hat, also exakt das, was
    # `gui/analysis_view.py::_resolve_sample_rate()` als Fallback braucht.
    rate_groups = resolve_rate_groups(config.active_channels(), config.sample_rate_hz)
    tick_rate_hz = max(
        (g.resolved_sample_rate_hz for g in rate_groups), default=config.sample_rate_hz
    )
    rate_by_hw_channel = {
        ch.hardware_channel: group.resolved_sample_rate_hz
        for group in rate_groups
        for ch in group.channels
    }

    channels_meta = []
    for ch in config.active_channels():
        channel_dict = ch.to_dict()
        # Native Rate je Kanal (kann von "sample_rate_hz" abweichen, z. B.
        # beim NI9210: 14 S/s eigene Rate trotz schnellerer Tick-Rate der
        # Datei) - Grundlage fuer rate-bewusste FFT/Filter in
        # `analysis/basic_analysis.py`, die auf einem forward-gefuellten
        # Kanal sonst faelschlich wiederholte Werte als echte neue Samples
        # behandeln wuerden.
        channel_dict["native_sample_rate_hz"] = rate_by_hw_channel.get(
            ch.hardware_channel, tick_rate_hz
        )
        channels_meta.append(channel_dict)

    return {
        "measurement_name": config.name,
        "start_time": session.start_time.isoformat() if session.start_time else None,
        "end_time": session.end_time.isoformat() if session.end_time else None,
        "duration_seconds": session.duration_seconds,
        "sample_rate_hz": tick_rate_hz,
        "target_sample_rate_hz": config.sample_rate_hz,
        "rate_groups": [
            {
                "resolved_sample_rate_hz": group.resolved_sample_rate_hz,
                "channel_hardware_ids": [ch.hardware_channel for ch in group.channels],
            }
            for group in rate_groups
        ],
        "samples_per_read": config.samples_per_read,
        "storage_format": config.storage_format.value,
        "hardware": [
            {
                "device_name": d.device_name,
                "product_type": d.product_type,
                "module_type": d.module_type.value if d.module_type else None,
                "num_channels": d.num_channels,
            }
            for d in device_infos
        ],
        "channels": channels_meta,
    }


def save_measurement_metadata(path: Path, metadata: dict[str, Any]) -> None:
    """Speichert Mess-Metadaten als JSON-Datei.

    Legt fehlende übergeordnete Verzeichnisse automatisch an.

    Raises:
        OSError: falls die Datei nicht geschrieben werden kann.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)
    logger.debug("Mess-Metadaten gespeichert: %s", path)


def load_measurement_metadata(path: Path) -> dict[str, Any]:
    """Lädt Mess-Metadaten aus einer JSON-Datei.

    Raises:
        FileNotFoundError: falls die Datei nicht existiert.
        json.JSONDecodeError: falls die Datei kein gültiges JSON enthält.
    """
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def channels_from_metadata(metadata: dict[str, Any]) -> list[Channel]:
    """Rekonstruiert die Kanalliste aus einem Mess-Metadaten-Dictionary."""
    return [Channel.from_dict(d) for d in metadata.get("channels", [])]


@dataclass
class MeasurementProject:
    """Repräsentiert ein Messprojekt gemäß der vorgegebenen Struktur::

        Projektname/
        |-- project.json
        |-- measurements/
        |     `-- measurement_001.parquet
        `-- metadata/
              `-- measurement_001_info.json

    Attributes:
        project_path: Wurzelverzeichnis des Projekts.
        name: Anzeigename des Projekts.
        created_at: Erstellungszeitpunkt.
        measurement_names: Namen der im Projekt enthaltenen Messungen
            (ohne Dateiendung), in der Reihenfolge ihrer Erstellung.
    """

    project_path: Path
    name: str
    created_at: datetime = field(default_factory=datetime.now)
    measurement_names: list[str] = field(default_factory=list)

    @property
    def measurements_dir(self) -> Path:
        """Verzeichnis, in dem die eigentlichen Messdatendateien liegen."""
        return self.project_path / MEASUREMENTS_DIR_NAME

    @property
    def metadata_dir(self) -> Path:
        """Verzeichnis, in dem die Metadaten-JSON-Dateien liegen."""
        return self.project_path / METADATA_DIR_NAME

    @property
    def project_file(self) -> Path:
        """Pfad zur project.json dieses Projekts."""
        return self.project_path / PROJECT_FILE_NAME

    @classmethod
    def create(cls, project_path: Path, name: str) -> "MeasurementProject":
        """Legt ein neues, leeres Messprojekt auf der Festplatte an.

        Raises:
            FileExistsError: falls unter `project_path` bereits ein
                Projekt (project.json) existiert.
        """
        project_path.mkdir(parents=True, exist_ok=True)
        project_file = project_path / PROJECT_FILE_NAME
        if project_file.exists():
            raise FileExistsError(
                f"Unter '{project_path}' existiert bereits ein Projekt "
                f"({PROJECT_FILE_NAME})."
            )

        project = cls(project_path=project_path, name=name)
        project.measurements_dir.mkdir(parents=True, exist_ok=True)
        project.metadata_dir.mkdir(parents=True, exist_ok=True)
        project.save()
        logger.info("Neues Messprojekt erstellt: %s", project_path)
        return project

    @classmethod
    def open(cls, project_path: Path) -> "MeasurementProject":
        """Öffnet ein bestehendes Messprojekt.

        Raises:
            FileNotFoundError: falls keine project.json existiert.
        """
        project_file = project_path / PROJECT_FILE_NAME
        if not project_file.exists():
            raise FileNotFoundError(
                f"Kein Messprojekt gefunden unter '{project_path}' "
                f"(erwartet: {PROJECT_FILE_NAME})."
            )
        with project_file.open("r", encoding="utf-8") as f:
            data = json.load(f)

        created_at_str = data.get("created_at")
        created_at = datetime.fromisoformat(created_at_str) if created_at_str else datetime.now()

        return cls(
            project_path=project_path,
            name=data.get("name", project_path.name),
            created_at=created_at,
            measurement_names=data.get("measurement_names", []),
        )

    def save(self) -> None:
        """Speichert `project.json` auf die Festplatte."""
        data = {
            "name": self.name,
            "created_at": self.created_at.isoformat(),
            "measurement_names": self.measurement_names,
        }
        with self.project_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.debug("Projektdatei gespeichert: %s", self.project_file)

    def register_measurement(self, measurement_name: str) -> None:
        """Fügt eine neue Messung zum Projekt hinzu und speichert sofort.

        Idempotent: mehrfaches Registrieren derselben Messung fügt sie
        nicht doppelt hinzu.
        """
        if measurement_name not in self.measurement_names:
            self.measurement_names.append(measurement_name)
            self.save()

    def measurement_data_path(self, measurement_name: str, storage_format: StorageFormat) -> Path:
        """Pfad zur Messdatendatei (.parquet oder .csv) für eine Messung."""
        suffix = ".parquet" if storage_format == StorageFormat.PARQUET else ".csv"
        return self.measurements_dir / f"{measurement_name}{suffix}"

    def measurement_metadata_path(self, measurement_name: str) -> Path:
        """Pfad zur Metadaten-JSON-Datei für eine Messung."""
        return self.metadata_dir / f"{measurement_name}_info.json"
