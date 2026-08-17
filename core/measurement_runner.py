"""
core/measurement_runner.py

`MeasurementRunner`: Komfort-Wrapper um `MeasurementController` für
Skript-/Automatisierungs-Gebrauch (siehe `doku/messung_per_skript.md`).

Hintergrund:
    `MeasurementController` kümmert sich bewusst NUR um Hardware und
    Ring Buffer (siehe `core/controller.py`) - das Anlegen/Starten/Stoppen
    eines `StorageWriter` sowie das Schreiben der Metadaten-Datei ist in
    der GUI Aufgabe von `gui/main_window.py`. Für ein eigenständiges
    Skript wäre das dieselbe Handarbeit noch einmal - `MeasurementConfig`
    (`save_to_disk`, `storage_format`) sagt aber bereits vollständig aus,
    OB und WIE gespeichert werden soll. `MeasurementRunner` zieht genau
    diese Orchestrierung heraus, damit ein Skript nur noch `start()`/
    `stop()` aufrufen muss.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from core.controller import MeasurementController
from data.exporter import StorageWriter
from data.metadata import build_measurement_metadata, save_measurement_metadata
from data.models import DeviceInfo, MeasurementConfig, MeasurementSession, StorageFormat

if TYPE_CHECKING:
    from gui.live_view import LiveView

logger = logging.getLogger(__name__)


class MeasurementRunner:
    """Startet/stoppt eine Messung inklusive automatischer Datenspeicherung
    und optionaler Live-Anzeige.

    Entspricht dem, was `gui/main_window.py` bei "Messung starten"/
    "Messung stoppen" tut - nur ohne GUI-Abhängigkeit, für den Gebrauch
    aus einem eigenen Python-Skript.
    """

    def __init__(
        self,
        controller: MeasurementController,
        storage_dir: Optional[Path] = None,
        live_view: Optional["LiveView"] = None,
    ) -> None:
        """Initialisiert den Runner.

        Args:
            controller: Ein (noch nicht gestarteter) `MeasurementController`.
            storage_dir: Zielverzeichnis für Messdaten- und Metadaten-Datei.
                Nur erforderlich, wenn Konfigurationen mit
                `save_to_disk=True` gestartet werden sollen.
            live_view: Optionales, bereits erzeugtes `LiveView`-Fenster. Wenn
                gesetzt, schalten `start()`/`stop()` dessen Anzeige
                automatisch mit ein/aus - kein eigener Aufruf von
                `live_view.start_display()`/`.stop_display()` mehr nötig.
        """
        self._controller = controller
        self._storage_dir = storage_dir
        self._live_view = live_view
        self._storage_writer: Optional[StorageWriter] = None

    @property
    def storage_writer(self) -> Optional[StorageWriter]:
        """Der `StorageWriter` der laufenden Messung, oder None (kein
        Speichern aktiv oder keine Messung gestartet)."""
        return self._storage_writer

    @property
    def live_view(self) -> Optional["LiveView"]:
        """Das mit dem Runner verknüpfte `LiveView`-Fenster, oder None."""
        return self._live_view

    @live_view.setter
    def live_view(self, value: Optional["LiveView"]) -> None:
        self._live_view = value

    def start(
        self,
        config: MeasurementConfig,
        discovered_devices: Optional[list[DeviceInfo]] = None,
    ) -> MeasurementSession:
        """Startet eine Messung und - falls `config.save_to_disk` gesetzt
        ist - automatisch den passenden `StorageWriter`.

        Args:
            config: Vollständige Messkonfiguration.
            discovered_devices: Siehe `MeasurementController.start_measurement`.

        Returns:
            Die gestartete `MeasurementSession`.

        Raises:
            ValueError: falls `config.save_to_disk=True`, aber kein
                `storage_dir` im Konstruktor angegeben wurde.
            MeasurementConfigError, AcquisitionError, RuntimeError: siehe
                `MeasurementController.start_measurement`.
        """
        if config.save_to_disk and self._storage_dir is None:
            raise ValueError(
                "config.save_to_disk ist gesetzt, aber MeasurementRunner "
                "wurde ohne storage_dir erzeugt."
            )

        session = self._controller.start_measurement(config, discovered_devices)

        if config.save_to_disk:
            extension = ".parquet" if config.storage_format == StorageFormat.PARQUET else ".csv"
            output_path = self._storage_dir / f"{config.name}{extension}"
            self._storage_writer = StorageWriter(
                ring_buffer=self._controller.get_ring_buffer(),
                channels=self._controller.active_channels,
                output_path=output_path,
                storage_format=config.storage_format,
                sample_rate_hz=config.sample_rate_hz,
            )
            self._storage_writer.start()
            logger.info("StorageWriter automatisch gestartet: %s", output_path)

        if self._live_view is not None:
            self._live_view.start_display(
                self._controller.active_channels,
                config.sample_rate_hz,
                storage_writer=self._storage_writer,
            )

        return session

    def stop(self, write_metadata: bool = True) -> Optional[MeasurementSession]:
        """Stoppt die laufende Messung inklusive `StorageWriter`.

        Reihenfolge bewusst so gewählt (siehe `doku/messung_per_skript.md`):
        zuerst der Controller (garantiert keine neuen Daten mehr im Ring
        Buffer), erst danach der `StorageWriter` (kann den Rest gefahrlos
        flushen) - andernfalls könnten die letzten Samples verloren gehen.

        Args:
            write_metadata: Ob zusätzlich eine `{name}_info.json` mit
                Mess-Metadaten geschrieben werden soll (nur relevant, wenn
                tatsächlich gespeichert wurde).

        Returns:
            Die abgeschlossene `MeasurementSession`, oder None, falls keine
            Messung lief.
        """
        # WICHTIG: `active_device_infos` VOR `stop_measurement()` auslesen.
        # `MeasurementController._stop_measurement_locked()` leert die
        # interne Geräteliste, bevor es zurückkehrt - danach ausgelesen
        # wäre `active_device_infos` immer `[]` und die Metadaten-Datei
        # würde nie echte Hardwareinformationen enthalten.
        device_infos = self._controller.active_device_infos
        session = self._controller.stop_measurement()

        if self._live_view is not None:
            self._live_view.stop_display()

        if self._storage_writer is not None:
            self._storage_writer.stop()
            if write_metadata and session is not None and self._storage_dir is not None:
                try:
                    metadata = build_measurement_metadata(session, device_infos)
                    metadata_path = self._storage_dir / f"{session.config.name}_info.json"
                    save_measurement_metadata(metadata_path, metadata)
                except OSError:
                    logger.exception("Metadaten konnten nicht gespeichert werden")
            self._storage_writer = None

        return session
