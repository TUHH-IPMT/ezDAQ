"""
data/exporter.py

Storage Writer: reads continuously and losslessly from the ring buffer
and writes measurement data to disk WHILE the measurement is running
(Parquet or CSV).

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

# Parquet row groups: the storage writer collects the small DAQ blocks
# (typically `samples_per_read` = 1000 samples) and only writes a row
# group once one of these two limits is reached. Writing every DAQ block
# as its own row group produced thousands of tiny row groups per
# measurement - a footer of several MB that every reader has to parse
# first, poor compression, slow reads (especially for readers that work
# row group by row group, e.g. MATLAB), and noticeably more CPU in the
# writer thread while the measurement is running.
#
# The byte limit bounds the RAM used by the buffer regardless of sample
# rate and channel count; the time limit (in measurement time) keeps
# row groups reasonably small for slow measurements.
PARQUET_ROW_GROUP_MAX_BYTES = 64 * 1024 * 1024
PARQUET_ROW_GROUP_MAX_SECONDS = 10.0

# Dictionary encoding only pays off for columns with frequently repeating
# values. Measurement values (and the time column) practically never
# repeat, so pyarrow would build a dictionary for every row group, notice
# it is useless and fall back - costing CPU time and leaving the started
# dictionary page in the file. Has no effect on how the file is read.
_PARQUET_WRITER_OPTIONS = {"use_dictionary": False}


def _sanitize_column_name(name: str) -> str:
    """Ensures a channel display name is usable as a column name.

    Does not change the channel's own `display_name` - only the column
    name used in the file (e.g. in case the display name were empty).
    """
    stripped = name.strip()
    return stripped if stripped else "kanal"


def _build_columns(
    scaled_block: np.ndarray,
    channels: list[Channel],
    start_sample_index: int,
    sample_rate_hz: float,
) -> dict[str, np.ndarray]:
    """Converts an already physically scaled raw data block into the
    file columns: a computed time column ("time_s") plus one column per
    channel."""
    n = scaled_block.shape[1]
    time_s = (start_sample_index + np.arange(n)) / sample_rate_hz
    data: dict[str, np.ndarray] = {"time_s": time_s}
    for i, channel in enumerate(channels):
        data[_sanitize_column_name(channel.display_name)] = scaled_block[i]
    return data


def _build_dataframe(
    scaled_block: np.ndarray,
    channels: list[Channel],
    start_sample_index: int,
    sample_rate_hz: float,
) -> pd.DataFrame:
    """Like `_build_columns`, but as a DataFrame (used for CSV)."""
    return pd.DataFrame(
        _build_columns(scaled_block, channels, start_sample_index, sample_rate_hz)
    )


def parquet_row_group_rows(num_channels: int, sample_rate_hz: float) -> int:
    """Number of rows per Parquet row group for a measurement.

    Whichever is reached first: `PARQUET_ROW_GROUP_MAX_BYTES` (time column
    plus one float64 column per channel) or `PARQUET_ROW_GROUP_MAX_SECONDS`
    of measurement time. Always at least 1.
    """
    bytes_per_row = (num_channels + 1) * 8
    by_bytes = PARQUET_ROW_GROUP_MAX_BYTES // bytes_per_row
    by_time = int(PARQUET_ROW_GROUP_MAX_SECONDS * sample_rate_hz)
    return max(1, min(by_bytes, by_time))


class StorageWriter:
    """Writes measurement data to disk continuously while the measurement is running.

    Reads via its own ring-buffer reader and writes new blocks to the
    target file immediately as they arrive:
        * Parquet: incrementally via `pyarrow.parquet.ParquetWriter`
          (one writer stays open for the entire measurement). The DAQ
          blocks are collected in memory and written as one row group as
          soon as `parquet_row_group_rows` samples are together (at most
          `PARQUET_ROW_GROUP_MAX_BYTES` / `PARQUET_ROW_GROUP_MAX_SECONDS`),
          the remainder when stopping. The file layout (columns
          "time_s" + one per channel) is unaffected by this.
        * CSV: every block is appended immediately
          (`DataFrame.to_csv(mode="a")`).

    Crash behavior: a CSV file contains the data up to the last block
    written. A Parquet file only becomes readable once it has been closed
    in `stop()` - the footer describing all row groups is written last.
    If the app crashes mid-measurement, the Parquet file is unreadable
    no matter how often row groups were written before.
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
        self._parquet_row_group_rows = parquet_row_group_rows(
            len(channels), sample_rate_hz
        )
        # Raw (unscaled) DAQ blocks not yet written as a row group, and
        # the index of the first sample in it (for the time column).
        self._parquet_pending: list[np.ndarray] = []
        self._parquet_pending_samples = 0
        self._parquet_pending_start = 0
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
        """Number of samples taken over from the ring buffer so far, per
        channel (for Parquet including those still collected in memory
        for the next row group - they are written at the latest in `stop()`)."""
        return self._total_samples_written

    @property
    def output_path(self) -> Path:
        """Target file being written to."""
        return self._output_path

    @property
    def buffered_samples(self) -> int:
        """Number of samples (per channel) collected in memory for the
        next Parquet row group, not yet in the file. Always 0 for CSV."""
        return self._parquet_pending_samples

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
        thread_finished = self._thread is None or not self._thread.is_alive()
        if thread_finished and self._last_error is None:
            # Remainder of the last, not yet complete Parquet row group.
            # Only once the thread is done - never concurrently with it.
            try:
                self._flush_parquet()
            except Exception as exc:
                logger.exception("Fehler beim Schreiben der letzten Row Group")
                self._last_error = exc
        elif self._parquet_pending_samples:
            logger.error(
                "%d gepufferte Samples konnten nicht mehr nach %s geschrieben werden.",
                self._parquet_pending_samples,
                self._output_path,
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
                "nur die Daten bis zum letzten erfolgreich geschriebenen "
                "Block (bei Parquet: bis zur letzten vollstaendigen Row "
                "Group, max. %.0f s): %s",
                self._output_path,
                PARQUET_ROW_GROUP_MAX_SECONDS,
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
        """Takes over a single raw data block: for CSV it is scaled and
        appended immediately, for Parquet it is collected and written as
        soon as a row group is full."""
        if self._storage_format == StorageFormat.PARQUET:
            if self._parquet_writer is None:
                # Open the file right away (not only with the first row
                # group), so it exists from the start of the recording -
                # e.g. for the file-size display in the live view.
                self._open_parquet_writer()
            if self._parquet_pending_samples == 0:
                self._parquet_pending_start = self._total_samples_written
            self._parquet_pending.append(raw_block)
            self._parquet_pending_samples += raw_block.shape[1]
            while self._parquet_pending_samples >= self._parquet_row_group_rows:
                self._flush_parquet(self._parquet_row_group_rows)
        else:
            scaled = apply_scaling(raw_block, self._channels)
            df = _build_dataframe(
                scaled, self._channels, self._total_samples_written, self._sample_rate_hz
            )
            df.to_csv(
                self._output_path,
                mode="a",
                header=not self._csv_header_written,
                index=False,
            )
            self._csv_header_written = True

    def _parquet_schema(self) -> pa.Schema:
        columns = _build_columns(
            np.empty((len(self._channels), 0)), self._channels, 0, self._sample_rate_hz
        )
        return pa.schema([(name, pa.float64()) for name in columns])

    def _open_parquet_writer(self) -> None:
        self._parquet_writer = pq.ParquetWriter(
            str(self._output_path), self._parquet_schema(), **_PARQUET_WRITER_OPTIONS
        )

    def _flush_parquet(self, max_rows: Optional[int] = None) -> None:
        """Writes up to `max_rows` (default: all) collected samples as one
        row group; anything beyond that stays collected."""
        if self._parquet_pending_samples == 0:
            return
        pending = (
            self._parquet_pending[0]
            if len(self._parquet_pending) == 1
            else np.concatenate(self._parquet_pending, axis=1)
        )
        rows = pending.shape[1] if max_rows is None else min(max_rows, pending.shape[1])
        chunk, rest = pending[:, :rows], pending[:, rows:]

        scaled = apply_scaling(chunk, self._channels)
        columns = _build_columns(
            scaled, self._channels, self._parquet_pending_start, self._sample_rate_hz
        )
        table = pa.Table.from_pydict(columns, schema=self._parquet_writer.schema)
        self._parquet_writer.write_table(table, row_group_size=rows)

        # Copy the (smaller) remainder, so the view doesn't keep the whole
        # concatenated buffer alive until the next row group.
        self._parquet_pending = [rest.copy()] if rest.shape[1] > 0 else []
        self._parquet_pending_samples = rest.shape[1]
        self._parquet_pending_start += rows

    def _close_writer(self) -> None:
        """Closes a possibly open Parquet writer (idempotent)."""
        if self._parquet_writer is not None:
            try:
                self._parquet_writer.close()
            except Exception:
                logger.exception("Fehler beim Schließen des Parquet-Writers")
            self._parquet_writer = None
