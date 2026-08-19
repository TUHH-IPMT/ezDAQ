"""
data/loader.py

Loads saved measurement data (Parquet/CSV) together with its metadata
for the analysis view (drag & drop, see `gui/analysis_view.py`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

from data.metadata import channels_from_metadata, load_measurement_metadata
from data.models import Channel

logger = logging.getLogger(__name__)


class LoaderError(Exception):
    """Raised when a measurement data file cannot be loaded."""


@dataclass
class LoadedMeasurement:
    """Container for a loaded, completed measurement.

    Attributes:
        data: DataFrame with column "time_s" and one column per channel
            (already physically scaled, see `data/exporter.py::StorageWriter`).
        channels: Channel list from the metadata (empty if no metadata
            file was found/given).
        metadata: Raw metadata dictionary (empty if no metadata was loaded).
        source_path: Path of the loaded measurement data file.
        x_column: Name of the x-axis column in `data`. Always "time_s"
            for regular measurement files; analysis results with a
            different x-axis (e.g. FFT results with "frequency_hz", see
            `gui/analysis_view.py`) use a synthetic `LoadedMeasurement`
            with a different `x_column`.
    """

    data: pd.DataFrame
    channels: list[Channel]
    metadata: dict
    source_path: Path
    x_column: str = "time_s"

    @property
    def channel_names(self) -> list[str]:
        """Names of the data columns, excluding the x-axis column."""
        return [c for c in self.data.columns if c != self.x_column]


def load_measurement_file(
    path: Path, metadata_path: Optional[Path] = None
) -> LoadedMeasurement:
    """Loads a measurement data file (.parquet or .csv), optionally with metadata.

    Args:
        path: Path to the measurement data file. The format is detected
            from the file extension (".parquet" or ".csv").
        metadata_path: Optional path to the associated
            "<name>_info.json" file. If given but not present, this is
            only logged (no exception) - the measurement data can still
            be loaded without channel information.

    Returns:
        `LoadedMeasurement` with data, channels (if available), and metadata.

    Raises:
        LoaderError: if the file does not exist or the format is not
            supported.
    """
    if not path.exists():
        raise LoaderError(f"Messdatendatei nicht gefunden: {path}")

    suffix = path.suffix.lower()
    try:
        if suffix == ".parquet":
            df = pd.read_parquet(path)
        elif suffix == ".csv":
            df = pd.read_csv(path)
        else:
            raise LoaderError(
                f"Nicht unterstütztes Dateiformat '{suffix}' "
                f"(erwartet .parquet oder .csv)."
            )
    except LoaderError:
        raise
    except Exception as exc:
        raise LoaderError(f"Fehler beim Laden von '{path}': {exc}") from exc

    metadata: dict = {}
    channels: list[Channel] = []
    if metadata_path is not None:
        if metadata_path.exists():
            try:
                metadata = load_measurement_metadata(metadata_path)
                channels = channels_from_metadata(metadata)
            except Exception as exc:
                logger.warning(
                    "Metadaten konnten nicht geladen werden (%s), Messdaten "
                    "werden trotzdem ohne Kanalinformationen geladen: %s",
                    metadata_path,
                    exc,
                )
        else:
            logger.info("Keine Metadaten-Datei gefunden unter: %s", metadata_path)

    return LoadedMeasurement(data=df, channels=channels, metadata=metadata, source_path=path)


def infer_metadata_path(measurement_path: Path) -> Path:
    """Guesses the likely metadata path for a measurement data file.

    Matching the naming convention from `data/metadata.py::MeasurementProject`::

        measurements/<name>.parquet -> metadata/<name>_info.json

    Useful for drag & drop in the analysis view, where the user only
    selects/drops the measurement data file without specifying the
    metadata file.
    """
    name = measurement_path.stem
    same_dir = measurement_path.with_name(f"{name}_info.json")
    if same_dir.exists():
        return same_dir
    return measurement_path.parent / "metadata" / f"{name}_info.json"
