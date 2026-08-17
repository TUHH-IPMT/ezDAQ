"""
data/loader.py

Lädt gespeicherte Messdaten (Parquet/CSV) zusammen mit ihren Metadaten
für die Analyse-Ansicht (Drag & Drop, siehe `gui/analysis_view.py`).
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
    """Wird geworfen, wenn eine Messdatendatei nicht geladen werden kann."""


@dataclass
class LoadedMeasurement:
    """Container für eine geladene, abgeschlossene Messung.

    Attributes:
        data: DataFrame mit Spalte "time_s" und einer Spalte je Kanal
            (bereits physikalisch skaliert, siehe `data/exporter.py::StorageWriter`).
        channels: Kanalliste aus den Metadaten (leer, falls keine
            Metadaten-Datei gefunden/angegeben wurde).
        metadata: Rohes Metadaten-Dictionary (leer, falls keine Metadaten geladen wurden).
        source_path: Pfad der geladenen Messdatendatei.
        x_column: Name der x-Achsen-Spalte in `data`. Für reguläre
            Messdateien immer "time_s"; Analyseergebnisse mit anderer
            x-Achse (z. B. FFT-Ergebnisse mit "frequency_hz", siehe
            `gui/analysis_view.py`) verwenden einen synthetischen
            `LoadedMeasurement` mit abweichendem `x_column`.
    """

    data: pd.DataFrame
    channels: list[Channel]
    metadata: dict
    source_path: Path
    x_column: str = "time_s"

    @property
    def channel_names(self) -> list[str]:
        """Namen der Datenspalten ohne die x-Achsen-Spalte."""
        return [c for c in self.data.columns if c != self.x_column]


def load_measurement_file(
    path: Path, metadata_path: Optional[Path] = None
) -> LoadedMeasurement:
    """Lädt eine Messdatendatei (.parquet oder .csv), optional mit Metadaten.

    Args:
        path: Pfad zur Messdatendatei. Das Format wird anhand der
            Dateiendung erkannt (".parquet" bzw. ".csv").
        metadata_path: Optionaler Pfad zur zugehörigen
            "<name>_info.json"-Datei. Falls angegeben, aber nicht
            vorhanden, wird dies nur geloggt (keine Exception) - die
            Messdaten können auch ohne Kanalinformationen geladen werden.

    Returns:
        `LoadedMeasurement` mit Daten, Kanälen (falls verfügbar) und Metadaten.

    Raises:
        LoaderError: falls die Datei nicht existiert oder das Format
            nicht unterstützt wird.
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
    """Errät den wahrscheinlichen Metadaten-Pfad zu einer Messdatendatei.

    Passend zur Namenskonvention aus `data/metadata.py::MeasurementProject`::

        measurements/<name>.parquet -> metadata/<name>_info.json

    Nützlich für Drag & Drop in der Analyse-Ansicht, wo der Nutzer nur die
    Messdatendatei auswählt/hineinzieht, ohne die Metadaten-Datei anzugeben.
    """
    name = measurement_path.stem
    same_dir = measurement_path.with_name(f"{name}_info.json")
    if same_dir.exists():
        return same_dir
    return measurement_path.parent / "metadata" / f"{name}_info.json"
