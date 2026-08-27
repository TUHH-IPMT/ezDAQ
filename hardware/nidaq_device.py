"""
hardware/nidaq_device.py

Shared nidaqmx task management for NI cDAQ modules.

This module (together with `ni9215.py`/`ni9234.py`) is the ONLY place
in the application that imports and uses `nidaqmx` directly. The GUI,
measurement controller, and ring buffer know only `BaseDevice` from
`hardware/base_device.py`.

Also contains `discover_devices()` for detecting connected
NI cDAQ modules for the setup view.

Note on the development environment:
    `nidaqmx` (the Python package) can be imported even without an
    installed NI-DAQmx driver and without connected hardware - method
    signatures have been checked against the installed package version.
    An actual task call (`nidaqmx.Task()`, `System.local()`, ...),
    however, fails without a driver/hardware with an exception from
    `nidaqmx.errors` (e.g. `DaqNotFoundError` without a driver,
    `DaqError` with a driver but without hardware). This file has
    therefore NOT been tested against real NI 9215/NI 9234
    hardware - that was not possible in this environment. Testing with
    real hardware is strongly recommended before measuring in production.
"""

from __future__ import annotations

import logging
import subprocess
from abc import abstractmethod
from pathlib import Path
from typing import Optional

import numpy as np

from data.models import Channel, DeviceInfo, ModuleType
from hardware.base_device import AcquisitionError, BaseDevice

logger = logging.getLogger(__name__)

# Registry path that the NI installer itself sets up for NI-MAX
# (Measurement & Automation Explorer) - see `find_ni_max_executable()`.
_NI_MAX_REGISTRY_KEY = r"SOFTWARE\WOW6432Node\National Instruments\Measurement & Automation Explorer"
_NI_MAX_FALLBACK_PATHS = (
    Path(r"C:\Program Files (x86)\National Instruments\MAX\NIMax.exe"),
    Path(r"C:\Program Files\National Instruments\MAX\NIMax.exe"),
)

try:
    import nidaqmx
    from nidaqmx.constants import AcquisitionType
    # nidaqmx.errors.Error is the COMMON base class of all nidaqmx
    # errors. Important: DaqNotFoundError (missing NI-DAQmx driver) and
    # DaqNotSupportedError do NOT inherit from DaqError, but only (like
    # DaqError itself) from Error. An `except DaqError` would therefore
    # NOT catch, e.g., the "no driver installed" case. Hence, errors are
    # caught here consistently against the common base class `Error`.
    from nidaqmx.errors import Error as DaqmxError
    from nidaqmx.stream_readers import AnalogMultiChannelReader
    from nidaqmx.system import System

    NIDAQMX_AVAILABLE = True
except ImportError:  # pragma: no cover - only relevant without nidaqmx installed
    NIDAQMX_AVAILABLE = False

    class DaqmxError(Exception):
        """Fallback in case `nidaqmx` is not installed."""

    AcquisitionType = None  # type: ignore[assignment]
    AnalogMultiChannelReader = None  # type: ignore[assignment]
    System = None  # type: ignore[assignment]
    nidaqmx = None  # type: ignore[assignment]


def discover_devices() -> list[DeviceInfo]:
    """Detects connected NI cDAQ modules via the local NI-DAQmx system.

    Called by the setup view to show the user a list of
    available devices/modules to choose from.

    Everything except `DeviceInfo.is_connected` is read from the
    NI-DAQmx configuration database, NOT from the hardware. That
    database keeps a once-configured device listed - with its full
    channel tree - even after it has been physically disconnected; a
    RESERVED network cDAQ chassis whose cable was pulled is the case
    that motivated this. Each device is therefore additionally probed
    for an actual hardware response, see `_is_device_connected`.

    Returns:
        List of detected devices, INCLUDING those that are only
        configured but currently unreachable (`is_connected is False`) -
        deliberately not filtered out here, so the setup view can show
        them as unavailable rather than letting them silently vanish
        (see `gui/setup_view.py::set_discovered_devices`).
        Empty if the driver works fine but simply no hardware is
        configured - that is a normal, not an erroneous, result.

    Raises:
        RuntimeError: if `nidaqmx`/the NI-DAQmx driver is NOT available
            on this machine, or if device discovery itself fails
            (e.g. driver/system error). Deliberately no longer silently
            reduced to an empty list - `gui/main_window.py` catches this
            via the `BackgroundWorker` and shows the cause directly in
            the setup view's device browser (see
            `SetupView.show_discovery_error`) instead of just logging it.
    """
    if not NIDAQMX_AVAILABLE:
        message = (
            "NI-DAQmx-Treiber (oder das Python-Paket 'nidaqmx') ist auf "
            "diesem Rechner nicht installiert."
        )
        # DEBUG only, see the rationale in the DaqmxError branch below.
        logger.debug(message)
        raise RuntimeError(message)

    devices: list[DeviceInfo] = []
    try:
        system = System.local()
        # One probe result per PROBE TARGET (chassis resp. standalone
        # device), reused across all modules of the same chassis - see
        # `_probe_target_name`.
        connection_probe_cache: dict[str, bool] = {}
        for device in system.devices:
            module_type = _map_product_type(device.product_type)
            try:
                phys_chs = [ch.name for ch in device.ai_physical_chans]
                num_channels = len(phys_chs)
            except DaqmxError:
                phys_chs = []
                num_channels = 0
            devices.append(
                DeviceInfo(
                    device_name=device.name,
                    product_type=device.product_type,
                    module_type=module_type,
                    num_channels=num_channels,
                    has_any_channels=_has_any_channels(device),
                    physical_channels=phys_chs,
                    is_connected=_is_device_connected(device, connection_probe_cache),
                )
            )
    except DaqmxError as exc:
        # Deliberately DEBUG rather than ERROR: the message is passed on
        # unchanged in the RuntimeError and logged AND displayed there,
        # where the error is actually handled (`gui/main_window.py`:
        # `_on_discover_hardware_failed` resp. `_on_start_measurement`). An
        # additional ERROR here would have made the same cause appear
        # twice in the console and look like two separate failures.
        logger.debug("Geräteerkennung fehlgeschlagen: %s", exc)
        raise RuntimeError(str(exc)) from exc

    return devices


def find_ni_max_executable() -> Optional[Path]:
    """Looks up the installation path of NI-MAX (Measurement & Automation
    Explorer) on this machine.

    Prefers the `Command` registry value that the NI installer itself
    sets up under `_NI_MAX_REGISTRY_KEY` - more robust than a hardcoded
    path, since it also holds true for a different install location or
    future NI-MAX versions. Falls back to the two usual default paths
    if the registry entry is missing.

    Returns:
        Path to `NIMax.exe`, or None if NI-MAX was not found on this
        machine (e.g. NI-DAQmx/NI-MAX not installed).
    """
    try:
        import winreg
    except ImportError:  # pragma: no cover - only relevant on non-Windows
        return None

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _NI_MAX_REGISTRY_KEY) as key:
            command, _ = winreg.QueryValueEx(key, "Command")
            path = Path(command)
            if path.exists():
                return path
    except OSError:
        pass

    for fallback in _NI_MAX_FALLBACK_PATHS:
        if fallback.exists():
            return fallback

    return None


def open_ni_max() -> None:
    """Starts NI-MAX (Measurement & Automation Explorer) as a separate
    process - quick access e.g. for checking/renaming devices,
    without leaving the actual application.

    Raises:
        RuntimeError: if NI-MAX was not found on this machine,
            or the start attempt itself fails.
    """
    path = find_ni_max_executable()
    if path is None:
        raise RuntimeError(
            "NI-MAX (Measurement & Automation Explorer) wurde auf diesem "
            "Rechner nicht gefunden. Ist der NI-DAQmx-Treiber installiert?"
        )
    try:
        subprocess.Popen([str(path)])
    except OSError as exc:
        raise RuntimeError(f"NI-MAX konnte nicht gestartet werden: {exc}") from exc


def _map_product_type(product_type: str) -> Optional[ModuleType]:
    """Maps an NI product designation (e.g. "NI 9215") to a ModuleType.

    Returns None if the module is (not yet) supported - the setup view
    can then display such modules as "not supported" instead of
    crashing.
    """
    normalized = product_type.replace(" ", "").replace("-", "").upper()
    for module_type in ModuleType:
        if module_type.value in normalized:
            return module_type
    return None


def _probe_target_name(device: "nidaqmx.system.Device") -> str:
    """The device whose reachability decides `device`'s reachability.

    For a cDAQ module that is its CHASSIS, not the module itself: the
    modules of a chassis are only reachable through the chassis, so if
    it doesn't answer, none of them do. Probing per chassis instead of
    per module keeps the number of hardware round-trips at one per
    chassis - discovery is already noticeably slow with several
    chassis/modules (see `gui/setup_view.py::_on_discover_hardware`),
    and a full self-test per module would multiply that.

    Falls back to the device's own name for anything that is not a cDAQ
    module (a chassis entry itself, a PXI or USB device, ...) - reading
    `compact_daq_chassis_device` raises for those, and an empty string
    is returned for a device that has no chassis.
    """
    try:
        chassis_name = device.compact_daq_chassis_device.name
    except DaqmxError:
        return device.name
    return chassis_name or device.name


def _is_device_connected(
    device: "nidaqmx.system.Device", probe_cache: dict[str, bool]
) -> bool:
    """Checks whether `device` ACTUALLY responds, rather than just being
    present in the NI-DAQmx configuration database.

    Everything else `discover_devices()` reads (`system.devices`,
    `product_type`, `ai_physical_chans`, ...) comes from that database
    and therefore survives a disconnect unchanged. A network cDAQ
    chassis reserved in NI-MAX keeps being listed with its complete
    channel tree after its cable has been pulled, and without this check
    the setup view would offer its channels as selectable although no
    measurement can be started with them.

    `self_test_device()` is used as the probe because it is the only
    documented NI-DAQmx call that is guaranteed to talk to the hardware
    ("Performs a brief test of device resources"); it raises for an
    unreachable device (e.g. -201252 NetworkTargetUnreachable, -201390
    NetworkStatusConnectionLost, -88705 device not present). A cheaper
    property read such as `serial_num` was deliberately NOT used: the
    driver may answer it from the same cache that causes the problem in
    the first place.

    `probe_cache` is keyed by `_probe_target_name` and MUST be shared
    across one discovery run so each chassis is probed only once.

    Note that this can only ever be a snapshot - a cable pulled after
    discovery is caught at measurement start (`NIDAQSharedTask`) resp.
    during acquisition (`core/acquisition.py`), not here.
    """
    target_name = _probe_target_name(device)
    cached = probe_cache.get(target_name)
    if cached is not None:
        return cached

    try:
        System.local().devices[target_name].self_test_device()
        connected = True
    except DaqmxError as exc:
        # DEBUG, not WARNING: a disconnected but still configured device
        # is a normal state the user resolves in the UI (the device is
        # shown as unavailable), not an application error.
        logger.debug(
            "Gerät '%s' antwortet nicht (Prüfung über '%s'): %s",
            device.name,
            target_name,
            exc,
        )
        connected = False

    probe_cache[target_name] = connected
    return connected


def _has_any_channels(device: "nidaqmx.system.Device") -> bool:
    """Checks whether `device` has ANY channel at all - analog in/out,
    digital in/out, or counter.

    This app currently supports exclusively analog input (see
    `DeviceInfo.num_channels`, which deliberately counts only
    `ai_physical_chans`) - a pure analog-output module like the NI9263
    would thus have `num_channels == 0`, just like an empty chassis
    controller entry with no channels at all. Without this separate,
    cross-channel-type check, a physically present but (not yet)
    unsupported non-AI module would be confused, when filtering for
    "has channels" (see `gui/setup_view.py::set_discovered_devices`),
    with an empty chassis entry and thereby wrongly and silently hidden -
    see `DeviceInfo.has_any_channels`.
    """
    channel_lists = (
        "ai_physical_chans",
        "ao_physical_chans",
        "di_lines",
        "do_lines",
        "ci_physical_chans",
        "co_physical_chans",
    )
    for attr_name in channel_lists:
        try:
            if len(getattr(device, attr_name)) > 0:
                return True
        except DaqmxError:
            continue
    return False


class NIDAQSharedTask:
    """Manages a shared nidaqmx task for multiple devices."""

    def __init__(self) -> None:
        self._task: Optional["nidaqmx.Task"] = None
        self._reader: Optional["AnalogMultiChannelReader"] = None
        self._channel_count = 0
        self._configured = False
        self._started = False
        self._sample_rate_hz = 0.0
        self._samples_per_read = 0

    def configure(self, sample_rate_hz: float, samples_per_read: int) -> None:
        """Creates the underlying task.

        Sample clock timing is deliberately NOT configured here:
        `cfg_samp_clk_timing()` fails with "no devices in the task" as
        long as the task contains no channels yet. Channels are only
        added afterwards by `NIDAQDevice.configure()` (once per device,
        via `_add_channel_to_task()`) directly to `self._task`; timing
        is finally configured via `finalize()` once all devices have
        added their channels.
        """
        if not NIDAQMX_AVAILABLE:
            raise AcquisitionError("nidaqmx ist nicht installiert oder der NI-DAQmx-Treiber ist auf diesem System nicht verfügbar.")

        self._task = nidaqmx.Task()
        self._sample_rate_hz = sample_rate_hz
        self._samples_per_read = samples_per_read
        self._configured = True

    def finalize(self) -> None:
        """Configures the sample clock timing after all devices have
        added their channels. Must be called exactly once before the
        task is started."""
        if self._task is None:
            raise AcquisitionError("Shared task ist nicht konfiguriert.")
        if self._channel_count == 0:
            raise AcquisitionError("Shared task hat keine Kanäle.")

        try:
            self._task.timing.cfg_samp_clk_timing(
                rate=self._sample_rate_hz,
                sample_mode=AcquisitionType.CONTINUOUS,
                samps_per_chan=self._samples_per_read,
                source="OnboardClock",
            )
            self._reader = AnalogMultiChannelReader(self._task.in_stream)
        except DaqmxError as exc:
            raise AcquisitionError(
                f"Timing-Konfiguration des gemeinsamen Tasks fehlgeschlagen: {exc}"
            ) from exc

    def start(self) -> None:
        if self._task is None:
            raise AcquisitionError("Shared task ist nicht konfiguriert.")
        if self._started:
            # Multiple devices share this task and call start()
            # individually - starting the already running task again
            # would cause nidaqmx to raise an error.
            return
        self._task.start()
        self._started = True

    def stop(self) -> None:
        if self._task is not None:
            self._task.stop()
            self._task.close()
            self._task = None
            self._reader = None
            self._configured = False
            self._started = False

    def read(self, samples_per_channel: int, timeout: float = 10.0) -> np.ndarray:
        if not self._configured or self._reader is None:
            raise AcquisitionError("Shared task ist nicht konfiguriert.")

        buffer = np.zeros((self._channel_count, samples_per_channel), dtype=np.float64)
        self._reader.read_many_sample(
            buffer,
            number_of_samples_per_channel=samples_per_channel,
            timeout=timeout,
        )
        return buffer

    def available_samples(self) -> int:
        if not self._configured or self._task is None:
            return 0
        return self._task.in_stream.avail_samp_per_chan


class NIDAQDevice(BaseDevice):
    """Base class for NI cDAQ modules; manages the lifecycle of an
    individual task or a shared task.

    Concrete subclasses (`NI9215`, `NI9234`) implement exclusively
    `_add_channel_to_task()` to add the module-specific nidaqmx channel
    type (e.g. voltage vs. IEPE acceleration). Task creation, timing
    configuration, start/stop, and block-wise reading are implemented
    here once, in common.
    """

    def __init__(self, device_info: DeviceInfo, channels: list[Channel]) -> None:
        super().__init__(device_info, channels)
        self._task: Optional["nidaqmx.Task"] = None
        self._reader: Optional["AnalogMultiChannelReader"] = None
        self._shared_task: Optional[NIDAQSharedTask] = None
        self._channel_offset = 0

        if not NIDAQMX_AVAILABLE:
            logger.warning(
                "nidaqmx ist nicht installiert - %s kann instanziiert, "
                "aber nicht konfiguriert/gestartet werden.",
                device_info.device_name,
            )

    def configure(
        self,
        sample_rate_hz: float,
        samples_per_read: int,
        sample_clock_source: str | None = None,
        shared_task: Optional[NIDAQSharedTask] = None,
    ) -> None:
        if not NIDAQMX_AVAILABLE:
            raise AcquisitionError(
                "nidaqmx ist nicht installiert oder der NI-DAQmx-Treiber "
                "ist auf diesem System nicht verfügbar."
            )
        if not self.active_channels:
            raise AcquisitionError(
                f"Gerät {self.device_info.device_name} hat keine aktiven Kanäle."
            )

        try:
            if shared_task is not None:
                # Multiple devices share the same hardware task, so
                # their samples come from the same sampling instant.
                self._shared_task = shared_task
                if not self._shared_task._configured:
                    self._shared_task.configure(sample_rate_hz, samples_per_read)
                self._channel_offset = self._shared_task._channel_count
                for channel in self.active_channels:
                    # Deliberately use the same module-specific method as
                    # in the standalone task path (see below), so that
                    # sensitivity, measurement range, excitation current,
                    # etc. are also set correctly in the shared task. A
                    # separate NIDAQSharedTask.add_channel() with only
                    # physical_channel/name would fall back to nidaqmx
                    # defaults for IEPE channels (e.g. sensitivity=1000
                    # mV/g instead of the configured sensor value) and
                    # thus return incorrect measurement values.
                    self._add_channel_to_task(self._shared_task._task, channel)
                    self._shared_task._channel_count += 1
                self._is_configured = True
                logger.info(
                    "%s konfiguriert als Teil eines gemeinsamen Tasks: %d Kanäle",
                    self.device_info.device_name,
                    len(self.active_channels),
                )
                return

            self._task = nidaqmx.Task()
            for channel in self.active_channels:
                self._add_channel_to_task(self._task, channel)

            timing_kwargs = {
                "rate": sample_rate_hz,
                "sample_mode": AcquisitionType.CONTINUOUS,
                "samps_per_chan": samples_per_read,
            }
            if sample_clock_source is None:
                timing_kwargs["source"] = "OnboardClock"
            else:
                timing_kwargs["source"] = sample_clock_source

            self._task.timing.cfg_samp_clk_timing(**timing_kwargs)

            self._reader = AnalogMultiChannelReader(self._task.in_stream)
            self._is_configured = True
            logger.info(
                "%s konfiguriert: %d Kanäle, %.1f Hz, %d Samples/Read",
                self.device_info.device_name,
                len(self.active_channels),
                sample_rate_hz,
                samples_per_read,
            )
        except DaqmxError as exc:
            self._cleanup_task()
            raise AcquisitionError(
                f"Konfiguration von {self.device_info.device_name} fehlgeschlagen: {exc}"
            ) from exc

    @abstractmethod
    def _add_channel_to_task(self, task: "nidaqmx.Task", channel: Channel) -> None:
        """Adds a single channel to the task in a module-specific way.

        Must be implemented by concrete subclasses, e.g. via
        `task.ai_channels.add_ai_voltage_chan(...)` (NI9215) or
        `task.ai_channels.add_ai_accel_chan(...)` (NI9234).
        """

    def start(self) -> None:
        if not self._is_configured:
            raise AcquisitionError(
                f"{self.device_info.device_name} ist nicht konfiguriert. "
                f"configure() muss zuerst aufgerufen werden."
            )
        try:
            if self._shared_task is not None:
                self._shared_task.start()
            else:
                if self._task is None:
                    raise AcquisitionError(
                        f"{self.device_info.device_name} ist nicht konfiguriert."
                    )
                self._task.start()
            self._is_running = True
            logger.info("%s gestartet", self.device_info.device_name)
        except DaqmxError as exc:
            raise AcquisitionError(
                f"Start von {self.device_info.device_name} fehlgeschlagen: {exc}"
            ) from exc

    def stop(self) -> None:
        if self._shared_task is not None:
            try:
                self._shared_task.stop()
            except Exception as exc:
                logger.warning(
                    "Fehler beim Stoppen des gemeinsamen Tasks für %s (wird ignoriert): %s",
                    self.device_info.device_name,
                    exc,
                )
            finally:
                self._shared_task = None
        if self._task is not None:
            try:
                self._task.stop()
            except DaqmxError as exc:
                logger.warning(
                    "Fehler beim Stoppen von %s (wird ignoriert): %s",
                    self.device_info.device_name,
                    exc,
                )
            finally:
                self._cleanup_task()
        self._is_running = False
        self._is_configured = False
        logger.info("%s gestoppt", self.device_info.device_name)

    def read_shared_block(self, samples_per_channel: int, timeout: float = 10.0) -> np.ndarray:
        if self._shared_task is None:
            raise AcquisitionError(
                f"{self.device_info.device_name} hat keinen gemeinsamen Task."
            )
        if not self._is_running:
            self._shared_task.start()
            self._is_running = True
        return self._shared_task.read(samples_per_channel, timeout=timeout)

    def read_from_shared_block(self, shared_block: np.ndarray) -> np.ndarray:
        if self._shared_task is None:
            raise AcquisitionError(
                f"{self.device_info.device_name} hat keinen gemeinsamen Task."
            )
        start = self._channel_offset
        end = start + len(self.active_channels)
        return shared_block[start:end, :]

    def read(self, samples_per_channel: int, timeout: float = 10.0) -> np.ndarray:
        if not self._is_running:
            if self._shared_task is not None:
                self._shared_task.start()
                self._is_running = True
            else:
                raise AcquisitionError(
                    f"{self.device_info.device_name} läuft nicht - start() muss "
                    f"zuerst aufgerufen werden."
                )

        if self._shared_task is not None:
            block = self._shared_task.read(samples_per_channel, timeout=timeout)
            return self.read_from_shared_block(block)

        if self._reader is None:
            raise AcquisitionError(
                f"{self.device_info.device_name} läuft nicht - start() muss "
                f"zuerst aufgerufen werden."
            )

        buffer = np.zeros((len(self.active_channels), samples_per_channel), dtype=np.float64)
        try:
            self._reader.read_many_sample(
                buffer,
                number_of_samples_per_channel=samples_per_channel,
                timeout=timeout,
            )
        except DaqmxError as exc:
            raise AcquisitionError(
                f"Lesefehler bei {self.device_info.device_name}: {exc}"
            ) from exc
        return buffer

    def available_samples(self) -> int:
        if self._shared_task is not None:
            return self._shared_task.available_samples()
        if self._task is None:
            return 0
        return self._task.in_stream.avail_samp_per_chan

    def _cleanup_task(self) -> None:
        """Closes the nidaqmx task and resets internal references."""
        if self._task is not None:
            try:
                self._task.close()
            except DaqmxError:
                pass
            self._task = None
        self._reader = None
