"""
data/metadata.py

Building and persisting metadata:
    * Metadata for a single measurement (measurement_info.json)
    * Bookkeeping for an entire measurement project (project.json)

Architecture note:
    The original module sketch envisioned `metadata.py` for measurement
    metadata only. Project bookkeeping (list of measurements, project
    name, creation date) is closely related in content (here too it's
    just plain JSON persistence of meta information), so it was housed
    here instead of creating an additional file for a very small
    responsibility. If the project grows (e.g. multiple projects open at
    once), `MeasurementProject` should be moved into its own file.
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
    """Builds the metadata dictionary for a measurement.

    Contains, per spec: start time, sample rate, hardware information,
    and channel information including scalings and units.

    Args:
        session: The (running or completed) measurement session.
        device_infos: Device information for the hardware used
            (e.g. `controller.active_device_infos`).
    """
    config = session.config
    # Derive rate groups from the configuration (see
    # `data/models.py::resolve_rate_groups`) - purely informational for
    # the metadata, no longer affects the measurement itself (that
    # already happened at start time in
    # `core/controller.py::start_measurement`). "sample_rate_hz" keeps
    # its previous meaning: the ACTUAL tick rate of the saved file (=
    # fastest group), NOT necessarily the target rate set by the user -
    # this is the rate `data/exporter.py::StorageWriter` used to compute
    # the `time_s` column, i.e. exactly what
    # `gui/analysis_view.py::_resolve_sample_rate()` needs as a fallback.
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
        # Native rate per channel (can differ from "sample_rate_hz", e.g.
        # for the NI9210: its own 14 S/s rate despite a faster file tick
        # rate) - basis for rate-aware FFT/filtering in
        # `analysis/basic_analysis.py`, which would otherwise mistakenly
        # treat repeated values from a forward-filled channel as genuine
        # new samples.
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
    """Saves measurement metadata as a JSON file.

    Creates any missing parent directories automatically.

    Raises:
        OSError: if the file cannot be written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)
    logger.debug("Mess-Metadaten gespeichert: %s", path)


def load_measurement_metadata(path: Path) -> dict[str, Any]:
    """Loads measurement metadata from a JSON file.

    Raises:
        FileNotFoundError: if the file does not exist.
        json.JSONDecodeError: if the file does not contain valid JSON.
    """
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def channels_from_metadata(metadata: dict[str, Any]) -> list[Channel]:
    """Reconstructs the channel list from a measurement metadata dictionary."""
    return [Channel.from_dict(d) for d in metadata.get("channels", [])]


@dataclass
class MeasurementProject:
    """Represents a measurement project according to the prescribed structure::

        ProjectName/
        |-- project.json
        |-- measurements/
        |     `-- measurement_001.parquet
        `-- metadata/
              `-- measurement_001_info.json

    Attributes:
        project_path: Root directory of the project.
        name: Display name of the project.
        created_at: Creation timestamp.
        measurement_names: Names of the measurements contained in the
            project (without file extension), in the order they were created.
    """

    project_path: Path
    name: str
    created_at: datetime = field(default_factory=datetime.now)
    measurement_names: list[str] = field(default_factory=list)

    @property
    def measurements_dir(self) -> Path:
        """Directory holding the actual measurement data files."""
        return self.project_path / MEASUREMENTS_DIR_NAME

    @property
    def metadata_dir(self) -> Path:
        """Directory holding the metadata JSON files."""
        return self.project_path / METADATA_DIR_NAME

    @property
    def project_file(self) -> Path:
        """Path to this project's project.json."""
        return self.project_path / PROJECT_FILE_NAME

    @classmethod
    def create(cls, project_path: Path, name: str) -> "MeasurementProject":
        """Creates a new, empty measurement project on disk.

        Raises:
            FileExistsError: if a project (project.json) already exists
                under `project_path`.
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
        """Opens an existing measurement project.

        Raises:
            FileNotFoundError: if no project.json exists.
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
        """Saves `project.json` to disk."""
        data = {
            "name": self.name,
            "created_at": self.created_at.isoformat(),
            "measurement_names": self.measurement_names,
        }
        with self.project_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.debug("Projektdatei gespeichert: %s", self.project_file)

    def register_measurement(self, measurement_name: str) -> None:
        """Adds a new measurement to the project and saves immediately.

        Idempotent: registering the same measurement multiple times does
        not add it twice.
        """
        if measurement_name not in self.measurement_names:
            self.measurement_names.append(measurement_name)
            self.save()

    def measurement_data_path(self, measurement_name: str, storage_format: StorageFormat) -> Path:
        """Path to the measurement data file (.parquet or .csv) for a measurement."""
        suffix = ".parquet" if storage_format == StorageFormat.PARQUET else ".csv"
        return self.measurements_dir / f"{measurement_name}{suffix}"

    def measurement_metadata_path(self, measurement_name: str) -> Path:
        """Path to the metadata JSON file for a measurement."""
        return self.metadata_dir / f"{measurement_name}_info.json"
