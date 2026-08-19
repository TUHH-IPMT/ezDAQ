"""
core/ringbuffer.py

Thread-safe ring buffer for multi-channel measurement data.

Architecture context (see spec):

    DAQ Thread -> Ring Buffer -> Live View
                              -> Storage Writer

The ring buffer decouples the DAQ acquisition thread from the two
consumers (live view, storage writer), which typically read at
different speeds:

    * The storage writer must receive EVERY sample without loss.
    * The live view is allowed - and supposed - to skip samples (e.g.
      when the GUI briefly isn't rendering), without affecting the
      storage writer.

For this reason, this ring buffer does NOT use a single global read
position, but independent "readers" each with their own read pointer
(`register_reader` / `read_new`). A slow reader can therefore never
block another reader or "consume away" its data.

Performance design for sample rates up to 100 kHz:
    * Memory is pre-allocated once as a NumPy array
      (`np.zeros((num_channels, capacity))`) - no per-sample allocations.
    * Writing/reading happens block-wise (NumPy slicing), not sample by
      sample.
    * The lock is held only for the (cheap) pointer bookkeeping; the
      actual data copying happens via NumPy vectorization within the
      lock, but stays short due to block processing.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class RingBuffer:
    """Thread-safe, block-wise ring buffer for multi-channel measurement data.

    A single `RingBuffer` holds data for a fixed number of channels with
    a fixed capacity (samples per channel). The DAQ thread writes blocks
    via `write()`. Any number of independent consumers can register via
    `register_reader()` and fetch their new data via `read_new()`,
    without affecting one another.

    Thread safety:
        All public methods are thread-safe (internally protected by a
        `threading.Lock`) and can safely be called from different
        threads (e.g. the DAQ thread writes while the GUI thread and the
        storage thread read concurrently).
    """

    def __init__(self, num_channels: int, capacity: int) -> None:
        """Initializes the ring buffer.

        Args:
            num_channels: Number of channels (rows of the internal array).
            capacity: Capacity in samples per channel.

        Raises:
            ValueError: if `num_channels` or `capacity` is not positive.
        """
        if num_channels <= 0:
            raise ValueError("num_channels muss positiv sein")
        if capacity <= 0:
            raise ValueError("capacity muss positiv sein")

        self._num_channels = num_channels
        self._capacity = capacity
        self._buffer = np.zeros((num_channels, capacity), dtype=np.float64)

        self._lock = threading.Lock()
        self._write_pos = 0
        self._total_written = 0

        self._readers: dict[int, int] = {}
        self._next_reader_id = 0

    @property
    def num_channels(self) -> int:
        """Number of channels in the buffer."""
        return self._num_channels

    @property
    def capacity(self) -> int:
        """Capacity of the buffer in samples per channel."""
        return self._capacity

    # ------------------------------------------------------------------ #
    # Reader management
    # ------------------------------------------------------------------ #

    def register_reader(self, back_samples: int = 0) -> int:
        """Registers a new, independent consumer (reader).

        By default, the new reader starts at the current write position,
        so it only sees data written AFTER registration.

        Args:
            back_samples: Optional - lets the reader start
                `back_samples` samples BEFORE the current write position
                instead (retroactive reading, e.g. for a pre-roll/
                pre-trigger buffer on a threshold trigger, see
                `gui/main_window.py::_on_trigger_fired`). Clamped to the
                oldest position still present in the buffer - both from
                below (not before sample 0) and from above (not further
                back than the buffer still holds, otherwise already
                overwritten data). `0` (default) corresponds exactly to
                the previous behavior.

        Returns:
            A reader ID to be used in all further calls (`read_new`,
            `available_samples`, `unregister_reader`).
        """
        with self._lock:
            reader_id = self._next_reader_id
            self._next_reader_id += 1
            oldest_valid = max(0, self._total_written - self._capacity)
            start_count = max(oldest_valid, self._total_written - max(0, back_samples))
            self._readers[reader_id] = start_count
            logger.debug(
                "Reader %d registriert (start_count=%d, back_samples=%d)",
                reader_id,
                start_count,
                back_samples,
            )
            return reader_id

    def unregister_reader(self, reader_id: int) -> None:
        """Removes a reader (e.g. when the live view is closed)."""
        with self._lock:
            self._readers.pop(reader_id, None)
            logger.debug("Reader %d entfernt", reader_id)

    # ------------------------------------------------------------------ #
    # Writing
    # ------------------------------------------------------------------ #

    def write(self, data: np.ndarray) -> None:
        """Writes a new data block into the ring buffer.

        Args:
            data: Array of shape (num_channels, num_new_samples).

        Raises:
            ValueError: if the channel count doesn't match, or the block
                is larger than the total capacity (configuration error,
                e.g. `samples_per_read > ring_buffer_size`).
        """
        if data.ndim != 2 or data.shape[0] != self._num_channels:
            raise ValueError(
                f"Erwartete Form (num_channels={self._num_channels}, N), "
                f"erhalten {data.shape}"
            )

        n = data.shape[1]
        if n == 0:
            return
        if n > self._capacity:
            raise ValueError(
                f"Block (N={n}) ist größer als die Ring-Buffer-Kapazität "
                f"({self._capacity}). ring_buffer_size in der Konfiguration erhöhen."
            )

        with self._lock:
            start = self._write_pos
            end = start + n

            if end <= self._capacity:
                self._buffer[:, start:end] = data
            else:
                first_part = self._capacity - start
                self._buffer[:, start:] = data[:, :first_part]
                self._buffer[:, : n - first_part] = data[:, first_part:]

            self._write_pos = (start + n) % self._capacity
            self._total_written += n

            self._check_overruns_locked()

    def _check_overruns_locked(self) -> None:
        """Detects and handles readers that have fallen too far behind.

        Must be called while holding `self._lock`. A reader that is more
        than `capacity` samples behind is guaranteed to have lost data
        (it has already been overwritten). In this case the reader is
        advanced to the oldest still-valid position and the data loss is
        logged.
        """
        for reader_id, read_count in list(self._readers.items()):
            lag = self._total_written - read_count
            if lag > self._capacity:
                lost = lag - self._capacity
                logger.warning(
                    "Ring Buffer Overrun fuer Reader %d: %d Samples wurden "
                    "ueberschrieben, bevor sie gelesen wurden (Kapazitaet %d). "
                    "Reader wird auf die aelteste verfuegbare Position vorgezogen.",
                    reader_id,
                    lost,
                    self._capacity,
                )
                self._readers[reader_id] = self._total_written - self._capacity

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #

    def read_new(self, reader_id: int, max_samples: Optional[int] = None) -> np.ndarray:
        """Reads all samples newly written since the last call.

        Non-blocking: if nothing new is available, an empty array is
        returned (the caller decides its own polling interval).

        Args:
            reader_id: ID from `register_reader`.
            max_samples: Optional upper bound on the number of samples
                returned per call. Useful for the live view, so that a
                GUI update after a longer pause doesn't have to draw a
                huge amount of data all at once. Skipped, older samples
                count as regularly read in this case (no overrun log).

        Returns:
            Array of shape (num_channels, num_available_samples).
            `num_available_samples` can be 0.

        Raises:
            KeyError: if `reader_id` is not (or no longer) registered.
        """
        with self._lock:
            if reader_id not in self._readers:
                raise KeyError(f"Unbekannte reader_id: {reader_id}")

            read_count = self._readers[reader_id]
            available = self._total_written - read_count

            if available <= 0:
                return np.empty((self._num_channels, 0), dtype=np.float64)

            if max_samples is not None and available > max_samples:
                skipped = available - max_samples
                read_count += skipped
                available = max_samples

            start = read_count % self._capacity
            end = start + available

            if end <= self._capacity:
                out = self._buffer[:, start:end].copy()
            else:
                first_part = self._capacity - start
                out = np.empty((self._num_channels, available), dtype=np.float64)
                out[:, :first_part] = self._buffer[:, start:]
                out[:, first_part:] = self._buffer[:, : available - first_part]

            self._readers[reader_id] = read_count + available
            return out

    def available_samples(self, reader_id: int) -> int:
        """Number of currently unread samples for a reader.

        Raises:
            KeyError: if `reader_id` is not (or no longer) registered.
        """
        with self._lock:
            if reader_id not in self._readers:
                raise KeyError(f"Unbekannte reader_id: {reader_id}")
            return max(0, self._total_written - self._readers[reader_id])

    def reset(self) -> None:
        """Resets the buffer and all read pointers (e.g. on measurement start)."""
        with self._lock:
            self._buffer.fill(0.0)
            self._write_pos = 0
            self._total_written = 0
            for reader_id in self._readers:
                self._readers[reader_id] = 0
            logger.debug("Ring Buffer zurueckgesetzt")
