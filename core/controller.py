"""
core/controller.py

Measurement controller: central interface between the GUI and the
underlying layers (hardware, ring buffer, DAQ thread).

Architecture (see spec):

    GUI -> Measurement Controller -> Hardware Interface -> nidaqmx -> NI cDAQ

The GUI exclusively calls methods of this controller - it knows neither
`RingBuffer` nor `BaseDevice` nor `nidaqmx` directly.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable, Optional

import numpy as np

from config.configuration_manager import ConfigurationManager
from core.acquisition import AcquisitionThread
from core.measurement import MeasurementConfigError, create_devices
from core.rate_merge import DeviceGroup
from core.ringbuffer import RingBuffer
from data.models import Channel, DeviceInfo, MeasurementConfig, MeasurementSession, TriggerKind, resolve_rate_groups
from hardware.base_device import AcquisitionError, BaseDevice
from hardware.nidaq_device import (
    NIDAQSharedTask,
    discover_devices,
    open_ni_max,
    probe_device_connections,
)

logger = logging.getLogger(__name__)

ErrorCallback = Callable[[Exception], None]


def _start_devices_sequentially(devices: list[BaseDevice]) -> None:
    """Starts all devices of ONE rate group one after another (see
    `start_measurement`). Within a group, multiple devices share a
    `NIDAQSharedTask` anyway - only the first `start()` call actually
    does anything (see `NIDAQSharedTask.start()`'s idempotency check),
    the rest are essentially free. Staying sequential within the group
    is therefore both sufficient and necessary - starting a
    `NIDAQSharedTask` from multiple threads at once would not be
    race-free."""
    for device in devices:
        device.start()


class MeasurementController:
    """Orchestrates a complete measurement.

    Responsibilities:
        * Configure and start hardware based on a `MeasurementConfig`.
        * Operate the DAQ thread.
        * Provide live data to consumers (live view, storage writer) via
          independent ring buffer readers.
        * Cleanly stop a measurement and release hardware resources.
                * Provide hardware-related tools such as NI-MAX via the
                    hardware layer, so the GUI never has to call hardware
                    implementations directly.

    An instance manages at most one running measurement at a time
    (see project spec: "Initially, only one project should be able to be
    open at a time").
    """

    def __init__(self, configuration_manager: ConfigurationManager) -> None:
        self._configuration_manager = configuration_manager
        self._lock = threading.RLock()

        self._devices: list[BaseDevice] = []
        self._ring_buffer: Optional[RingBuffer] = None
        self._acquisition_thread: Optional[AcquisitionThread] = None
        self._session: Optional[MeasurementSession] = None

        self._error_listeners: list[ErrorCallback] = []

    # ------------------------------------------------------------------ #
    # Device discovery
    # ------------------------------------------------------------------ #

    def discover_hardware(self, probe_connections: bool = True) -> list[DeviceInfo]:
        """Detects connected NI cDAQ modules (for the setup view).

        See `hardware.nidaq_device.discover_devices` for why the setup
        view passes `probe_connections=False` here and asks for the
        probe separately via `probe_hardware_connections`.
        """
        return discover_devices(probe_connections=probe_connections)

    def probe_hardware_connections(self, devices: list[DeviceInfo]) -> dict[str, bool]:
        """Second stage of the device discovery - see
        `hardware.nidaq_device.probe_device_connections`.
        """
        return probe_device_connections(devices)

    def open_ni_max(self) -> None:
        """Starts NI-MAX (see `hardware/nidaq_device.py::open_ni_max`)."""
        open_ni_max()

    # ------------------------------------------------------------------ #
    # Error notification
    # ------------------------------------------------------------------ #

    def add_error_listener(self, callback: ErrorCallback) -> None:
        """Registers a callback for DAQ thread errors.

        Used e.g. by `gui/main_window.py` to display an error message on
        a hardware error during a measurement. The callback is called IN
        THE DAQ THREAD (see `_handle_acquisition_error`) - GUI callbacks
        must therefore communicate thread-safely with the Qt event loop
        (e.g. via Qt signals, not by direct widget manipulation).
        """
        self._error_listeners.append(callback)

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #

    @property
    def is_running(self) -> bool:
        """True while a measurement is actively running."""
        with self._lock:
            return self._acquisition_thread is not None and self._acquisition_thread.is_running

    @property
    def current_session(self) -> Optional[MeasurementSession]:
        """The currently running (or most recently started) `MeasurementSession`."""
        with self._lock:
            return self._session

    @property
    def total_samples_acquired(self) -> int:
        """Total number of samples per channel acquired so far by the DAQ thread.

        Basis for a configured sample-count limit (see
        `data/models.py::MeasurementConfig.is_recording_limit_reached`,
        called from `gui/live_view.py::_on_timer_tick`).
        """
        with self._lock:
            if self._acquisition_thread is None:
                return 0
            return self._acquisition_thread.total_samples_acquired

    @property
    def active_channels(self) -> list[Channel]:
        """Active channels in the same order the ring buffer writes them."""
        with self._lock:
            if self._session is None:
                return []
            return self.acquisition_channels

    @property
    def acquisition_channels(self) -> list[Channel]:
        """Returns the active channels in DAQ acquisition order."""
        with self._lock:
            if self._session is None:
                return []
            if not self._devices:
                return self._session.config.active_channels()

            channels: list[Channel] = []
            for device in self._devices:
                channels.extend(device.active_channels)
            return channels

    @property
    def active_device_infos(self) -> list[DeviceInfo]:
        """Device information for the hardware currently in use (for metadata)."""
        with self._lock:
            return [d.device_info for d in self._devices]

    # ------------------------------------------------------------------ #
    # Start/stop measurement
    # ------------------------------------------------------------------ #

    def start_measurement(
        self,
        config: MeasurementConfig,
        discovered_devices: Optional[list[DeviceInfo]] = None,
    ) -> MeasurementSession:
        """Starts a new measurement according to `config`.

        Args:
            config: Complete measurement configuration (channels, sample
                rate, ...).
            discovered_devices: Optional result of `discover_hardware()`,
                to avoid repeated device discovery. Otherwise called
                automatically.

        Returns:
            The newly started `MeasurementSession`.

        Raises:
            RuntimeError: if a measurement is already running.
            MeasurementConfigError: on an invalid channel configuration.
            AcquisitionError: if hardware configuration/start fails.
        """
        with self._lock:
            # The session stays set until explicit cleanup in
            # `stop_measurement()`, even if the DAQ thread has already
            # reached its own sample limit and ended on its own.
            if self._session is not None:
                raise RuntimeError(
                    "Es läuft bereits eine Messung. Bitte zuerst stop_measurement() aufrufen."
                )

            active_channels = config.active_channels()
            if not active_channels:
                raise MeasurementConfigError(
                    "Die Messkonfiguration enthält keine aktiven Kanäle."
                )

            if discovered_devices is None:
                discovered_devices = self.discover_hardware()

            # Group channels by rate compatibility (see
            # `data/models.py::resolve_rate_groups`) - the normal case is
            # exactly ONE group (all devices still share a common
            # task/sample clock). More than one group only occurs on a
            # genuine rate conflict (currently: NI9210 together with
            # another module) - these groups each get their own task and
            # are only merged after reading, via `RateMerger` (see
            # `core/acquisition.py`).
            rate_groups = resolve_rate_groups(active_channels, config.sample_rate_hz)

            configured_devices: list[BaseDevice] = []
            device_groups: list[DeviceGroup] = []
            try:
                for rate_group in rate_groups:
                    group_devices = create_devices(rate_group.channels, discovered_devices)
                    shared_task: Optional[NIDAQSharedTask] = None
                    if len(group_devices) > 1:
                        # Multiple devices in ONE group get a shared task,
                        # so they share the same acquisition on the
                        # hardware side - the preferred case.
                        shared_task = NIDAQSharedTask()
                    for device in group_devices:
                        device.configure(
                            rate_group.resolved_sample_rate_hz,
                            config.samples_per_read,
                            sample_clock_source=None,
                            shared_task=shared_task,
                        )
                        configured_devices.append(device)
                    if shared_task is not None:
                        # Only configure the shared task's timing after
                        # ALL devices of THIS group have added their
                        # channels (see NIDAQSharedTask.finalize()).
                        shared_task.finalize()
                    device_groups.append(
                        DeviceGroup(
                            devices=group_devices,
                            resolved_sample_rate_hz=rate_group.resolved_sample_rate_hz,
                        )
                    )
            except AcquisitionError:
                self._close_devices(configured_devices)
                raise

            devices = [device for group in device_groups for device in group.devices]

            ring_buffer = RingBuffer(
                num_channels=len(active_channels),
                capacity=config.ring_buffer_size,
            )

            try:
                if len(device_groups) > 1:
                    # Separate groups (only on a genuine rate conflict,
                    # see resolve_rate_groups) each have their own,
                    # independent task - starting it is a pure network
                    # round trip to the driver commit (on an Ethernet
                    # cDAQ chassis roughly 0.4-1.0s, measured on real
                    # hardware) and can be parallelized instead of waited
                    # for sequentially - on real hardware (two groups,
                    # same chassis) this yields roughly 15-20% shorter
                    # total start latency instead of the theoretical
                    # halving, presumably because both groups share
                    # network/chassis resources during startup. Within a
                    # group, the start stays sequential (see
                    # `_start_devices_sequentially`).
                    with ThreadPoolExecutor(max_workers=len(device_groups)) as executor:
                        futures = [
                            executor.submit(_start_devices_sequentially, group.devices)
                            for group in device_groups
                        ]
                        for future in futures:
                            future.result()
                else:
                    _start_devices_sequentially(devices)
            except AcquisitionError:
                self._close_devices(devices)
                raise

            # The hardware hard stop (target_samples) counts samples from
            # the start of hardware acquisition - with an automatic
            # trigger (threshold/serial) that is the arming moment, NOT
            # the actual recording start (see
            # `data/models.py::TriggerConfig`). A limit set here would
            # therefore already elapse during the pre-roll/waiting phase.
            # Only in manual mode do both moments coincide - there the
            # previous behavior remains unchanged. For threshold/serial,
            # the limit is instead enforced exclusively via a software
            # check in `gui/live_view.py::_on_timer_tick` (with the
            # correct zero point relative to the actual trigger moment).
            hardware_target_samples = (
                None
                if config.recording_unlimited or config.trigger.start.kind != TriggerKind.NONE
                else config.target_recording_stop_samples()
            )
            acquisition_thread = AcquisitionThread(
                device_groups=device_groups,
                ring_buffer=ring_buffer,
                samples_per_read=config.samples_per_read,
                on_error=self._handle_acquisition_error,
                target_samples=hardware_target_samples,
            )
            acquisition_thread.start()

            self._devices = devices
            self._ring_buffer = ring_buffer
            self._acquisition_thread = acquisition_thread
            self._session = MeasurementSession(config=config, start_time=datetime.now())

            if devices:
                self._configuration_manager.update_last_device_name(
                    devices[0].device_info.device_name
                )
            self._configuration_manager.save_channel_configuration(active_channels)

            logger.info(
                "Messung '%s' gestartet: %d Kanäle, %.1f Hz",
                config.name,
                len(active_channels),
                config.sample_rate_hz,
            )
            return self._session

    def stop_measurement(self) -> Optional[MeasurementSession]:
        """Stops the running measurement (if any).

        Returns:
            The completed `MeasurementSession` (with `end_time` set), or
            None if no measurement was running.
        """
        with self._lock:
            return self._stop_measurement_locked()

    def _stop_measurement_locked(self) -> Optional[MeasurementSession]:
        """Internal stop logic. Must be called while holding `self._lock`."""
        if self._acquisition_thread is not None:
            self._acquisition_thread.stop()
            self._acquisition_thread = None

        self._close_devices(self._devices)
        self._devices = []
        self._ring_buffer = None

        session = self._session
        if session is not None and session.end_time is None:
            session.end_time = datetime.now()
        self._session = None

        if session is not None:
            logger.info(
                "Messung '%s' gestoppt nach %.1f s",
                session.config.name,
                session.duration_seconds or 0.0,
            )
        return session

    def _close_devices(self, devices: list[BaseDevice]) -> None:
        """Closes a list of devices; errors are logged, not re-raised."""
        for device in devices:
            try:
                device.close()
            except Exception:
                logger.exception(
                    "Fehler beim Schließen von %s", device.device_info.device_name
                )

    def _handle_acquisition_error(self, exc: Exception) -> None:
        """Reacts to an error in the DAQ thread.

        IMPORTANT: This method is called BY THE DAQ THREAD ITSELF (the
        `on_error` callback in `AcquisitionThread._run` runs in the same
        thread that raised the exception). It therefore deliberately does
        NOT call `stop_measurement()`, since `AcquisitionThread.stop()`
        would try to join the DAQ thread with itself (`Thread.join()` on
        the thread's own, currently running thread leads to an infinite
        wait, i.e. a logical deadlock).

        Instead, only the hardware resources are cleaned up; the DAQ
        thread ends on its own by returning from its read loop. A later,
        explicit `stop_measurement()` call (e.g. by the GUI, after being
        informed of the error) then finds an already-ended thread and
        cleans up idempotently.
        """
        with self._lock:
            logger.error(
                "Messung wird aufgrund eines Fehlers im DAQ-Thread beendet: %s", exc
            )
            self._close_devices(self._devices)
            self._devices = []
            if self._session is not None and self._session.end_time is None:
                self._session.end_time = datetime.now()

        for callback in list(self._error_listeners):
            try:
                callback(exc)
            except Exception:
                logger.exception("Fehler in einem Error-Listener")

    # ------------------------------------------------------------------ #
    # Live data for consumers (live view, storage writer)
    # ------------------------------------------------------------------ #

    def register_reader(self) -> int:
        """Registers a new live data consumer on the ring buffer.

        Raises:
            RuntimeError: if no measurement is currently running.
        """
        with self._lock:
            if self._ring_buffer is None:
                raise RuntimeError("Es läuft aktuell keine Messung.")
            return self._ring_buffer.register_reader()

    def unregister_reader(self, reader_id: int) -> None:
        """Removes a live data consumer (e.g. when closing the live view)."""
        with self._lock:
            if self._ring_buffer is not None:
                self._ring_buffer.unregister_reader(reader_id)

    def read_live_data(self, reader_id: int, max_samples: Optional[int] = None) -> np.ndarray:
        """Reads new raw data (UNSCALED) for a registered reader.

        Consumers must apply scaling themselves via
        `core.measurement.apply_scaling(data, controller.active_channels)`.

        Raises:
            RuntimeError: if no measurement is currently running.
        """
        with self._lock:
            if self._ring_buffer is None:
                raise RuntimeError("Es läuft aktuell keine Messung.")
            ring_buffer = self._ring_buffer
        # Deliberately OUTSIDE the lock: RingBuffer is itself thread-safe;
        # this way a read does not block the controller lock for other
        # concurrent operations (e.g. register_reader from another thread).
        return ring_buffer.read_new(reader_id, max_samples=max_samples)

    def get_ring_buffer(self) -> Optional[RingBuffer]:
        """Returns the ring buffer of the running measurement (for the storage writer).

        Needed by `data/exporter.py::StorageWriter` to register its own
        reader that is guaranteed not to lose any data.
        """
        with self._lock:
            return self._ring_buffer
