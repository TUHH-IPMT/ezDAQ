"""
core/measurement_runner.py

`MeasurementRunner`: convenience wrapper around `MeasurementController`
for script/automation use (see `doku/messung_per_skript.md`).

Background:
    `MeasurementController` deliberately takes care of ONLY hardware and
    the ring buffer (see `core/controller.py`) - creating/starting/
    stopping a `StorageWriter` as well as writing the metadata file is
    the GUI's job in `gui/main_window.py`. For a standalone script that
    would be the same manual work all over again - but `MeasurementConfig`
    (`save_to_disk`, `storage_format`) already fully states WHETHER and
    HOW to store the data. `MeasurementRunner` extracts exactly that
    orchestration, so a script only has to call `start()`/`stop()`.
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
    """Starts/stops a measurement including automatic data storage and
    optional live display.

    Corresponds to what `gui/main_window.py` does on "Start Measurement"/
    "Stop Measurement" - just without a GUI dependency, for use from a
    standalone Python script.
    """

    def __init__(
        self,
        controller: MeasurementController,
        storage_dir: Optional[Path] = None,
        live_view: Optional["LiveView"] = None,
    ) -> None:
        """Initializes the runner.

        Args:
            controller: A (not yet started) `MeasurementController`.
            storage_dir: Target directory for the measurement data and
                metadata file. Only required if configurations with
                `save_to_disk=True` are to be started.
            live_view: Optional, already-created `LiveView` window. If
                set, `start()`/`stop()` automatically switch its display
                on/off - no separate call to
                `live_view.start_display()`/`.stop_display()` needed
                anymore.
        """
        self._controller = controller
        self._storage_dir = storage_dir
        self._live_view = live_view
        self._storage_writer: Optional[StorageWriter] = None

    @property
    def storage_writer(self) -> Optional[StorageWriter]:
        """The `StorageWriter` of the running measurement, or None (no
        storage active or no measurement started)."""
        return self._storage_writer

    @property
    def live_view(self) -> Optional["LiveView"]:
        """The `LiveView` window associated with the runner, or None."""
        return self._live_view

    @live_view.setter
    def live_view(self, value: Optional["LiveView"]) -> None:
        self._live_view = value

    def start(
        self,
        config: MeasurementConfig,
        discovered_devices: Optional[list[DeviceInfo]] = None,
    ) -> MeasurementSession:
        """Starts a measurement and - if `config.save_to_disk` is set -
        automatically the matching `StorageWriter`.

        Args:
            config: Complete measurement configuration.
            discovered_devices: See `MeasurementController.start_measurement`.

        Returns:
            The started `MeasurementSession`.

        Raises:
            ValueError: if `config.save_to_disk=True` but no
                `storage_dir` was given in the constructor.
            MeasurementConfigError, AcquisitionError, RuntimeError: see
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
        """Stops the running measurement including the `StorageWriter`.

        Order deliberately chosen this way (see `doku/messung_per_skript.md`):
        the controller first (guarantees no more new data in the ring
        buffer), only then the `StorageWriter` (can safely flush the
        rest) - otherwise the last samples could be lost.

        Args:
            write_metadata: Whether to additionally write a
                `{name}_info.json` with measurement metadata (only
                relevant if data was actually stored).

        Returns:
            The completed `MeasurementSession`, or None if no measurement
            was running.
        """
        # IMPORTANT: read `active_device_infos` BEFORE `stop_measurement()`.
        # `MeasurementController._stop_measurement_locked()` clears the
        # internal device list before it returns - if read afterward,
        # `active_device_infos` would always be `[]` and the metadata
        # file would never contain real hardware information.
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
