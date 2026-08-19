"""
hardware/base_device.py

Abstract interface for hardware modules.

The GUI and the measurement controller (`core/controller.py`) communicate
EXCLUSIVELY through this interface - never directly with `nidaqmx`
or a concrete device class. This makes it possible to add further modules
later (e.g. NI 9411 digital I/O, a simulation device for tests without
hardware, or devices from other manufacturers) without having to modify
the GUI or the controller.

Architecture (see spec):

    GUI -> Measurement Controller -> Hardware Interface -> nidaqmx -> NI cDAQ
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np

from data.models import Channel, DeviceInfo


class AcquisitionError(Exception):
    """Raised for errors during configuration, start, or acquisition.

    Wraps nidaqmx-specific errors (`nidaqmx.DaqError`) in a
    hardware-independent error type, so GUI/controller code doesn't have
    to program against `nidaqmx` exceptions.
    """


class BaseDevice(ABC):
    """Abstract base class for a single hardware module.

    A concrete device (e.g. `NI9215`, `NI9234`) encapsulates the complete
    communication with the underlying driver API. This base class
    defines a synchronous lifecycle:

        configure() -> start() -> read() [repeated] -> stop()

    Continuous acquisition on a dedicated thread (DAQ thread) and
    writing into the ring buffer are NOT part of this class, but are the
    responsibility of `core/acquisition.py`. This separation keeps the
    hardware layer testable (synchronous, without threading complexity).
    """

    def __init__(self, device_info: DeviceInfo, channels: list[Channel]) -> None:
        """Initializes the device with device info and channel list.

        Args:
            device_info: Description of the physical module.
            channels: Channels to acquire (only `enabled` channels are
                actually configured by the concrete implementations).
        """
        self.device_info = device_info
        self.channels = channels
        self._is_configured = False
        self._is_running = False

    @property
    def is_configured(self) -> bool:
        """True as soon as `configure()` has been called successfully."""
        return self._is_configured

    @property
    def is_running(self) -> bool:
        """True while acquisition is running (between `start()` and `stop()`)."""
        return self._is_running

    @property
    def active_channels(self) -> list[Channel]:
        """Only the enabled channels (see `Channel.enabled`)."""
        return [ch for ch in self.channels if ch.enabled]

    @abstractmethod
    def configure(
        self,
        sample_rate_hz: float,
        samples_per_read: int,
        sample_clock_source: str | None = None,
    ) -> None:
        """Configures the hardware for a measurement.

        Typically creates the underlying task, adds all active channels,
        and configures the sample clock timing.

        Args:
            sample_rate_hz: Sample rate in Hz.
            samples_per_read: Block size for later `read()` calls
                (among other things, determines the hardware's timing buffer).
            sample_clock_source: Optional clock source for devices that
                operate without a shared task. For NI devices with a
                shared task, sampling is normally driven by the shared
                task's default onboard clock.

        Raises:
            AcquisitionError: if configuration fails
                (e.g. invalid channel, device unreachable).
        """

    @abstractmethod
    def start(self) -> None:
        """Starts acquisition.

        Raises:
            AcquisitionError: if the device is not configured
                or the start fails.
        """

    @abstractmethod
    def stop(self) -> None:
        """Stops acquisition and releases hardware resources.

        Must be idempotent (calling it multiple times must not fail).
        """

    @abstractmethod
    def read(self, samples_per_channel: int, timeout: float = 10.0) -> np.ndarray:
        """Reads a block of raw (unscaled) values from the hardware.

        Args:
            samples_per_channel: Number of samples to read per channel.
            timeout: Timeout in seconds before an error is raised.

        Returns:
            Array of shape (num_active_channels, samples_per_channel).

        Raises:
            AcquisitionError: on timeout or read error.
        """

    @abstractmethod
    def available_samples(self) -> int:
        """Number of samples per channel currently available in the
        hardware buffer that have NOT yet been read - a NON-blocking
        query (a pure status query to the driver, does not wait for new
        data).

        Intended for callers that want to decide for themselves how many
        samples they can read WITHOUT blocking (see
        `core/rate_merge.py::RateMerger` - prevents a slow,
        hardware-fixed sample rate like the NI9210's from blocking the
        acquisition thread while a faster group keeps running in
        parallel).
        """

    def close(self) -> None:
        """Cleans up resources. Idempotent, calls `stop()` if needed."""
        if self._is_running:
            try:
                self.stop()
            except AcquisitionError:
                pass

    def __enter__(self) -> "BaseDevice":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
