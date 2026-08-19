"""
core/acquisition.py

DAQ thread: cyclically reads from all configured hardware devices and
writes the combined raw data into the ring buffer.

Architecture (see spec):

    DAQ Thread -> Ring Buffer -> Live View
                              -> Storage Writer

Design decision:
    There is EXACTLY ONE `AcquisitionThread` per measurement, which in
    each cycle acquires a shared data block from all configured devices
    and joins the resulting partial blocks along the channel axis into
    ONE combined block, before it is written to the (single, shared)
    ring buffer exactly once. This avoids multiple simultaneous writers
    to the same ring buffer and keeps the channel order well-defined
    (device order corresponds to channel order in the ring buffer, see
    `core/measurement.py::create_devices`).

    With multiple NI devices, a shared nidaqmx task is automatically
    used so that the samples originate from the same acquisition. The
    acquisition loop uses this shared task automatically, without the
    user having to perform any additional configuration.

    More than one device group (see `core/rate_merge.py::DeviceGroup`)
    is only possible with an intrinsic rate conflict (currently: NI9210
    together with another module, see
    `data/models.py::resolve_rate_groups`) - in that case `RateMerger`
    merges the groups via zero-order hold into a jointly clocked block,
    BEFORE it is written to the ring buffer. The single-group case (the
    normal case) is unaffected by this and continues to run via the
    direct read path.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

import numpy as np

from core.rate_merge import DeviceGroup, RateMerger, _read_group_block
from core.ringbuffer import RingBuffer
from hardware.base_device import AcquisitionError

logger = logging.getLogger(__name__)

ErrorCallback = Callable[[Exception], None]


class AcquisitionThread:
    """Runs the cyclic data acquisition in its own thread.

    In each cycle, reads from all given devices and writes the combined
    raw data (UNSCALED) into the ring buffer. Physical scaling
    (`core.measurement.apply_scaling`) is deliberately NOT done here, but
    only when the data is consumed (storage writer, live view), so the
    DAQ thread stays as fast as possible.
    """

    def __init__(
        self,
        device_groups: list[DeviceGroup],
        ring_buffer: RingBuffer,
        samples_per_read: int,
        read_timeout_seconds: float = 5.0,
        on_error: Optional[ErrorCallback] = None,
        target_samples: Optional[int] = None,
    ) -> None:
        """Initializes the DAQ thread.

        Args:
            device_groups: Rate-compatible device groups (see
                `data/models.py::resolve_rate_groups` +
                `core/rate_merge.py::DeviceGroup`), already configured
                and started. The channel order across all groups and
                devices (group 1 first, within it device 1 first, ...)
                MUST match the channel order of `ring_buffer`. The
                normal case is exactly ONE group; more than one group
                means a genuine rate conflict (see
                `core/rate_merge.py::RateMerger`).
            ring_buffer: Target ring buffer; `ring_buffer.num_channels`
                must match the sum of active channels across all devices.
            samples_per_read: Block size per read cycle, relative to the
                FASTEST group.
            read_timeout_seconds: Timeout per `device.read()` call.
            on_error: Optional callback invoked on an error in the DAQ
                thread. IMPORTANT: The callback itself runs in the DAQ
                thread (see `core/controller.py` for the resulting
                consequences regarding deadlock avoidance).
            target_samples: Optional target sample count per channel
                (see
                `data/models.py::MeasurementConfig.target_recording_stop_samples`).
                NEVER exceeded: the last block is trimmed exactly to the
                remaining count, after which the thread ends itself
                IMMEDIATELY - without waiting for `stop()`/the external
                stop signal (see `_run`). `None` = unlimited (runs until
                `stop()` is called - previous default behavior).

        Raises:
            ValueError: if the devices' channel count doesn't match the
                ring buffer's channel count.
        """
        self._devices = [d for group in device_groups for d in group.devices]
        total_channels = sum(len(d.active_channels) for d in self._devices)
        if total_channels != ring_buffer.num_channels:
            raise ValueError(
                f"Summe der aktiven Kanäle aller Geräte ({total_channels}) "
                f"stimmt nicht mit ring_buffer.num_channels "
                f"({ring_buffer.num_channels}) überein."
            )

        self._device_groups = device_groups
        self._ring_buffer = ring_buffer
        self._samples_per_read = samples_per_read
        self._read_timeout_seconds = read_timeout_seconds
        self._target_samples = target_samples
        self._on_error = on_error
        self._merger = RateMerger(device_groups, read_timeout_seconds) if len(device_groups) > 1 else None

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_error: Optional[Exception] = None
        self.total_samples_acquired = 0

    @property
    def is_running(self) -> bool:
        """True while the DAQ thread is actively running."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def last_error(self) -> Optional[Exception]:
        """The most recent error, if the thread ended due to an error
        (otherwise None)."""
        return self._last_error

    def start(self) -> None:
        """Starts the DAQ thread.

        Expects that all given devices are already configured AND
        started (see `core/controller.py::start_measurement`).

        Raises:
            RuntimeError: if the thread is already running.
        """
        if self.is_running:
            raise RuntimeError("AcquisitionThread läuft bereits.")
        self._stop_event.clear()
        self._last_error = None
        self.total_samples_acquired = 0
        self._thread = threading.Thread(
            target=self._run, name="DAQAcquisitionThread", daemon=True
        )
        self._thread.start()
        logger.info("DAQ-Thread gestartet (%d Geräte)", len(self._devices))

    def stop(self, timeout: float = 5.0) -> None:
        """Signals the DAQ thread to stop and waits for it to end.

        Idempotent: can also be called if the thread has already ended
        (e.g. due to an error).
        """
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning(
                    "DAQ-Thread reagiert nicht innerhalb von %.1fs auf das Stop-Signal.",
                    timeout,
                )
        logger.info(
            "DAQ-Thread gestoppt, insgesamt %d Samples pro Kanal erfasst.",
            self.total_samples_acquired,
        )

    def _run(self) -> None:
        """Main loop of the DAQ thread (runs in the background thread).

        With `target_samples` set: for the last block, specifically
        requests only the remaining sample count (instead of a full
        block) and breaks the loop IMMEDIATELY once the target is
        reached - no waiting for `_stop_event`. This guarantees exactly
        `target_samples` in the ring buffer (neither more nor less) AND
        makes stopping on a configured limit practically delay-free,
        since the thread has, as a rule, already ended by the time the
        next external `stop()` call happens (see
        `gui/live_view.py::_on_timer_tick`).
        """
        try:
            while not self._stop_event.is_set():
                samples_to_read = self._samples_per_read
                if self._target_samples is not None:
                    remaining = self._target_samples - self.total_samples_acquired
                    if remaining <= 0:
                        break
                    samples_to_read = min(samples_to_read, remaining)

                combined = self._read_combined_block(samples_to_read)
                if self._target_samples is not None:
                    # Safety net in case a device delivers more than the
                    # requested `samples_to_read` anyway - guarantees
                    # exactly `target_samples`, regardless of driver
                    # behavior.
                    remaining = self._target_samples - self.total_samples_acquired
                    combined = combined[:, :remaining]
                self._ring_buffer.write(combined)
                self.total_samples_acquired += combined.shape[1]

                if self._target_samples is not None and self.total_samples_acquired >= self._target_samples:
                    break
        except AcquisitionError as exc:
            logger.error("Fehler im DAQ-Thread: %s", exc)
            self._last_error = exc
            if self._on_error is not None:
                self._on_error(exc)
        except Exception as exc:  # unexpected error is deliberately not swallowed
            logger.exception("Unerwarteter Fehler im DAQ-Thread")
            self._last_error = exc
            if self._on_error is not None:
                self._on_error(exc)

    def _read_combined_block(self, samples_to_read: int) -> np.ndarray:
        """Reads the (possibly merged) raw data block for this cycle.

        The single-group case (the normal case) uses exactly the same
        read path as before (`_read_group_block`), unchanged. Only with
        > 1 group (currently only NI9210 combined with another module)
        does `RateMerger` come into play (see `core/rate_merge.py`).
        """
        if self._merger is not None:
            return self._merger.read_merged_block(samples_to_read)
        devices = self._device_groups[0].devices if self._device_groups else []
        return _read_group_block(devices, samples_to_read, self._read_timeout_seconds)
