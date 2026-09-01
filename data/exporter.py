"""
data/exporter.py

Storage Writer: reads continuously and losslessly from the ring buffer
and writes measurement data to disk in blocks WHILE the measurement is
running (Parquet or CSV).

Architecture (per spec):

    DAQ thread -> ring buffer -> storage writer

The storage writer registers itself as its own, independent ring-buffer
reader (see `core/ringbuffer.py::RingBuffer`) and reads WITHOUT a
`max_samples` limit, so it's guaranteed not to lose any samples -
regardless of how fast or slow the live view reads in parallel.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from core.measurement import apply_scaling
from core.ringbuffer import RingBuffer
from data.models import Channel, StorageFormat

logger = logging.getLogger(__name__)


def _sanitize_column_name(name: str) -> str:
    """Ensures a channel display name is usable as a column name.

    Does not change the channel's own `display_name` - only the column
    name used in the file (e.g. in case the display name were empty).
    """
    stripped = name.strip()
    return stripped if stripped else "kanal"


def _build_dataframe(
    scaled_block: np.ndarray,
    channels: list[Channel],
    start_sample_index: int,
    sample_rate_hz: float,
) -> pd.DataFrame:
    """Converts an already physically scaled raw data block into a
    DataFrame with a computed time column ("time_s")."""
    n = scaled_block.shape[1]
    time_s = (start_sample_index + np.arange(n)) / sample_rate_hz
    data: dict[str, np.ndarray] = {"time_s": time_s}
    for i, channel in enumerate(channels):
        data[_sanitize_column_name(channel.display_name)] = scaled_block[i]
    return pd.DataFrame(data)


class StorageWriter:
    """Writes measurement data to disk continuously while the measurement is running.

    Reads via its own ring-buffer reader and writes new blocks to the
    target file immediately as they arrive:
        * Parquet: incrementally via `pyarrow.parquet.ParquetWriter`
          (one writer stays open for the entire measurement, each block
          is appended as another row group).
        * CSV: incremental appending (`DataFrame.to_csv(mode="a")`).

    This means measurement data survives up to the last block written
    even if the app crashes during a long measurement (see project spec
    "save data while the measurement is running").
    """

    def __init__(
        self,
        ring_buffer: RingBuffer,
        channels: list[Channel],
        output_path: Path,
        storage_format: StorageFormat,
        sample_rate_hz: float,
        poll_interval_seconds: float = 0.05,
        reader_back_samples: int = 0,
    ) -> None:
        """Initializes the storage writer.

        Args:
            ring_buffer: Ring buffer of the running measurement.
            channels: Active channels in the order of the ring buffer rows
                (must match the order the DAQ thread writes in, see
                `core/measurement.py::create_devices`).
            output_path: Target file. The extension is NOT adjusted
                automatically - see
                `data/metadata.py::MeasurementProject.measurement_data_path`.
            storage_format: `StorageFormat.PARQUET` or `StorageFormat.CSV`.
            sample_rate_hz: Sample rate used for the computed time column.
            poll_interval_seconds: Wait time between read attempts when
                no new data is currently available in the ring buffer.
            reader_back_samples: Optional lead-in in samples - lets the
                internal ring-buffer reader start retroactively (see
                `core/ringbuffer.py::RingBuffer.register_reader`), so
                data already buffered before this StorageWriter was
                created gets written too (pre-trigger recording for the
                threshold trigger, see
                `gui/main_window.py::_on_trigger_fired`). `0` (default)
                matches the previous behavior (start "now").
        """
        self._ring_buffer = ring_buffer
        self._channels = channels
        self._output_path = output_path
        self._storage_format = storage_format
        self._sample_rate_hz = sample_rate_hz
        self._poll_interval_seconds = poll_interval_seconds

        self._reader_id = ring_buffer.register_reader(back_samples=reader_back_samples)
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_error: Optional[Exception] = None

        self._total_samples_written = 0
        self._lost_samples = 0
        self._parquet_writer: Optional[pq.ParquetWriter] = None
        self._csv_header_written = False

    @property
    def is_running(self) -> bool:
        """True while the storage-writer thread is actively running."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def last_error(self) -> Optional[Exception]:
        """The most recent error, if the thread terminated because of it."""
        return self._last_error

    @property
    def total_samples_written(self) -> int:
        """Number of samples written so far, per channel."""
        return self._total_samples_written

    @property
    def output_path(self) -> Path:
        """Target file being written to."""
        return self._output_path

    @property
    def pending_samples(self) -> int:
        """Number of samples already written by the DAQ thread but not yet
        transferred to disk by the StorageWriter.

        If this value approaches the ring buffer capacity (e.g. because
        the disk can't keep up or is full), an overrun is imminent -
        unrecoverable data loss, see
        `core/ringbuffer.py::RingBuffer._check_overruns_locked`.
        """
        return self._ring_buffer.available_samples(self._reader_id)

    @property
    def lost_samples(self) -> int:
        """Number of samples that never reached the file.

        Above 0 means the ring buffer overran (see
        `core/ringbuffer.py::lost_samples`): the DAQ thread was faster
        than this writer, and the missing samples cannot be recovered.
        The resulting file does NOT look damaged - the time column is
        built from the samples actually written and closes over the gap
        seamlessly - so this is the only way to find out.
        """
        try:
            self._lost_samples = self._ring_buffer.lost_samples(self._reader_id)
        except KeyError:
            # Reader already unregistered (after `stop()`) - `stop()` read
            # the final value before doing so, and that is what stays
            # available here. A caller checking the outcome of a finished
            # measurement is the main consumer of this property.
            pass
        return self._lost_samples

    def start(self) -> None:
        """Starts the storage-writer thread.

        Raises:
            RuntimeError: if the thread is already running.
        """
        if self.is_running:
            raise RuntimeError("StorageWriter läuft bereits.")
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="StorageWriterThread", daemon=True
        )
        self._thread.start()
        logger.info("StorageWriter gestartet: %s", self._output_path)

    def stop(self, timeout: float = 10.0) -> None:
        """Stops the storage-writer thread and closes the target file cleanly.

        Waits until the thread has processed all data already read but
        not yet written, before the file is closed.
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning(
                    "StorageWriter reagiert nicht innerhalb von %.1fs auf das Stop-Signal.",
                    timeout,
                )
        self._close_writer()

        # Verluste und Fehler VOR dem Abmelden des Readers ablesen - danach
        # ist der Zaehler weg.
        lost = self.lost_samples
        self._ring_buffer.unregister_reader(self._reader_id)

        logger.info(
            "StorageWriter gestoppt, %d Samples in %s gespeichert.",
            self._total_samples_written,
            self._output_path,
        )

        # Die drei Faelle, in denen die Messung NICHT vollstaendig auf der
        # Platte steht. Bisher endeten alle drei still - der Aufrufer sah nur
        # die Erfolgsmeldung oben und hielt die Messung fuer gelungen.
        if self._last_error is not None:
            logger.error(
                "StorageWriter wurde durch einen Fehler beendet, %s enthaelt "
                "nur die Daten bis zum letzten erfolgreichen Block: %s",
                self._output_path,
                self._last_error,
            )
        if lost > 0:
            logger.error(
                "%d Samples fehlen in %s (Ring Buffer Overrun). Die Datei "
                "sieht dabei unauffaellig aus: die Zeitspalte wird aus den "
                "geschriebenen Samples berechnet und schliesst die Luecke "
                "nahtlos. Die Messung ist unvollstaendig.",
                lost,
                self._output_path,
            )
        if self._total_samples_written == 0:
            logger.error(
                "Es wurden keine Samples geschrieben, %s wurde daher gar "
                "nicht erst angelegt.",
                self._output_path,
            )

    def _run(self) -> None:
        try:
            while True:
                data = self._ring_buffer.read_new(self._reader_id)
                if data.shape[1] == 0:
                    if self._stop_event.is_set():
                        break
                    time.sleep(self._poll_interval_seconds)
                    continue
                self._write_block(data)
                self._total_samples_written += data.shape[1]
        except Exception as exc:
            logger.exception("Fehler im StorageWriter, Messdaten bis zum letzten "
                              "erfolgreich geschriebenen Block bleiben erhalten.")
            self._last_error = exc

    def _write_block(self, raw_block: np.ndarray) -> None:
        """Scales and writes a single raw data block."""
        scaled = apply_scaling(raw_block, self._channels)
        df = _build_dataframe(
            scaled, self._channels, self._total_samples_written, self._sample_rate_hz
        )

        if self._storage_format == StorageFormat.PARQUET:
            table = pa.Table.from_pandas(df, preserve_index=False)
            if self._parquet_writer is None:
                self._parquet_writer = pq.ParquetWriter(str(self._output_path), table.schema)
            self._parquet_writer.write_table(table)
        else:
            df.to_csv(
                self._output_path,
                mode="a",
                header=not self._csv_header_written,
                index=False,
            )
            self._csv_header_written = True

    def _close_writer(self) -> None:
        """Closes a possibly open Parquet writer (idempotent)."""
        if self._parquet_writer is not None:
            try:
                self._parquet_writer.close()
            except Exception:
                logger.exception("Fehler beim Schließen des Parquet-Writers")
            self._parquet_writer = None
