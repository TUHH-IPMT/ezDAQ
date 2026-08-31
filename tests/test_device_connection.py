"""
tests/test_device_connection.py

Tests for the hardware connection probe added to device discovery
(`hardware/nidaq_device.py::discover_devices`/`_is_device_connected`)
and the resulting refusal at measurement start
(`core/measurement.py::create_devices`).

Motivating case: a network cDAQ chassis reserved in NI-MAX stays fully
listed in the NI-DAQmx configuration database - name, product type and
complete channel tree - even after its cable has been pulled. Everything
`discover_devices()` reads apart from the probe comes from that database,
so without the probe such a device kept being offered as selectable.

`nidaqmx` is faked here rather than required: the real driver cannot be
made to produce an unplugged-but-configured device on demand, and the
tests would otherwise only run on a machine with NI hardware attached.

`SetupViewProblemReportingTest` additionally covers the reporting side:
since switching to the setup view now triggers a device search on its
own, an unchanged problem must not raise its dialog again every time.

`TwoStageDiscoveryTest` covers the split of that discovery into a fast
listing stage and a separate probing stage
(`discover_devices(probe_connections=False)` plus
`probe_device_connections`), which keeps a network chassis running into
its timeout from delaying the device list.
"""

from __future__ import annotations

import unittest

import core.measurement as measurement
import hardware.nidaq_device as nidaq_device
from data.models import Channel, ModuleType


class _FakeDaqmxError(Exception):
    """Stands in for `nidaqmx.errors.Error`."""


class _FakeChannel:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeDevice:
    """A device as the NI-DAQmx CONFIGURATION DATABASE reports it.

    Everything except `self_test_device()` is answered from the cached
    configuration and therefore stays available after a disconnect -
    that is exactly what makes the probe necessary.
    """

    di_lines: list = []
    do_lines: list = []
    ci_physical_chans: list = []
    co_physical_chans: list = []
    ao_physical_chans: list = []

    def __init__(
        self,
        name: str,
        product_type: str,
        chassis_name: str | None = None,
        num_ai_channels: int = 2,
        reachable: bool = True,
        probe_log: list[str] | None = None,
    ) -> None:
        self.name = name
        self.product_type = product_type
        self._chassis_name = chassis_name
        self._num_ai_channels = num_ai_channels
        self._reachable = reachable
        self._probe_log = probe_log if probe_log is not None else []

    @property
    def ai_physical_chans(self) -> list[_FakeChannel]:
        return [_FakeChannel(f"{self.name}/ai{i}") for i in range(self._num_ai_channels)]

    @property
    def compact_daq_chassis_device(self) -> "_FakeDevice":
        if self._chassis_name is None:
            # What the real driver does for a non-cDAQ device.
            raise _FakeDaqmxError("device is not a CompactDAQ module")
        return _FakeDevice(self._chassis_name, "cDAQ-9189")

    def self_test_device(self) -> None:
        self._probe_log.append(self.name)
        if not self._reachable:
            raise _FakeDaqmxError("-201252 NetworkTargetUnreachable")


class _FakeSystemFactory:
    """Replacement for `nidaqmx.system.System` with `local()`."""

    def __init__(self, devices: list[_FakeDevice], by_name: dict[str, _FakeDevice]) -> None:
        self._devices = devices
        self._by_name = by_name

    def local(self) -> "_FakeSystemFactory":
        return self

    @property
    def devices(self) -> "_FakeDeviceCollection":
        return _FakeDeviceCollection(self._devices, self._by_name)


class _FakeDeviceCollection:
    def __init__(self, devices: list[_FakeDevice], by_name: dict[str, _FakeDevice]) -> None:
        self._devices = devices
        self._by_name = by_name

    def __iter__(self):
        return iter(self._devices)

    def __getitem__(self, name: str) -> _FakeDevice:
        return self._by_name[name]


class DiscoverDevicesConnectionProbeTest(unittest.TestCase):
    """`discover_devices()` with one reachable and one unplugged chassis."""

    def setUp(self) -> None:
        self.probe_log: list[str] = []
        modules = [
            _FakeDevice("cDAQ1Mod1", "NI 9215", "cDAQ1", probe_log=self.probe_log),
            _FakeDevice("cDAQ1Mod2", "NI 9234", "cDAQ1", probe_log=self.probe_log),
            _FakeDevice("cDAQ2Mod1", "NI 9215", "cDAQ2", probe_log=self.probe_log),
            _FakeDevice("cDAQ2Mod2", "NI 9235", "cDAQ2", probe_log=self.probe_log),
        ]
        by_name = {module.name: module for module in modules}
        # The chassis entries themselves are what actually gets probed.
        by_name["cDAQ1"] = _FakeDevice(
            "cDAQ1", "cDAQ-9189", num_ai_channels=0, reachable=True, probe_log=self.probe_log
        )
        by_name["cDAQ2"] = _FakeDevice(
            "cDAQ2", "cDAQ-9189", num_ai_channels=0, reachable=False, probe_log=self.probe_log
        )

        self._originals = (
            nidaq_device.NIDAQMX_AVAILABLE,
            nidaq_device.System,
            nidaq_device.DaqmxError,
        )
        nidaq_device.NIDAQMX_AVAILABLE = True
        nidaq_device.System = _FakeSystemFactory(modules, by_name)
        nidaq_device.DaqmxError = _FakeDaqmxError

    def tearDown(self) -> None:
        (
            nidaq_device.NIDAQMX_AVAILABLE,
            nidaq_device.System,
            nidaq_device.DaqmxError,
        ) = self._originals

    def test_unreachable_chassis_marks_all_its_modules_disconnected(self) -> None:
        by_name = {d.device_name: d for d in nidaq_device.discover_devices()}

        self.assertTrue(by_name["cDAQ1Mod1"].is_connected)
        self.assertTrue(by_name["cDAQ1Mod2"].is_connected)
        self.assertFalse(by_name["cDAQ2Mod1"].is_connected)
        self.assertFalse(by_name["cDAQ2Mod2"].is_connected)

    def test_disconnected_devices_are_still_listed(self) -> None:
        """Not filtered out - the setup view shows them grayed out, so a
        configured-but-unreachable device doesn't silently vanish."""
        names = [d.device_name for d in nidaq_device.discover_devices()]

        self.assertEqual(
            names, ["cDAQ1Mod1", "cDAQ1Mod2", "cDAQ2Mod1", "cDAQ2Mod2"]
        )

    def test_probes_once_per_chassis_not_per_module(self) -> None:
        """Discovery is already slow with several chassis/modules - a
        self-test per module would multiply the hardware round-trips."""
        nidaq_device.discover_devices()

        self.assertEqual(self.probe_log, ["cDAQ1", "cDAQ2"])

    def test_channel_metadata_still_read_for_disconnected_device(self) -> None:
        """The config database still answers these - the point of the flag
        is that they can no longer be trusted as "available"."""
        by_name = {d.device_name: d for d in nidaq_device.discover_devices()}

        offline = by_name["cDAQ2Mod1"]
        self.assertEqual(offline.num_channels, 2)
        self.assertEqual(offline.physical_channels, ["cDAQ2Mod1/ai0", "cDAQ2Mod1/ai1"])


class StandaloneDeviceProbeTest(unittest.TestCase):
    """A device without a chassis is probed under its own name."""

    def setUp(self) -> None:
        self.probe_log: list[str] = []
        device = _FakeDevice(
            "Dev1", "NI USB-6210", chassis_name=None, reachable=False, probe_log=self.probe_log
        )
        self._originals = (
            nidaq_device.NIDAQMX_AVAILABLE,
            nidaq_device.System,
            nidaq_device.DaqmxError,
        )
        nidaq_device.NIDAQMX_AVAILABLE = True
        nidaq_device.System = _FakeSystemFactory([device], {"Dev1": device})
        nidaq_device.DaqmxError = _FakeDaqmxError

    def tearDown(self) -> None:
        (
            nidaq_device.NIDAQMX_AVAILABLE,
            nidaq_device.System,
            nidaq_device.DaqmxError,
        ) = self._originals

    def test_falls_back_to_the_device_itself(self) -> None:
        devices = nidaq_device.discover_devices()

        self.assertEqual(self.probe_log, ["Dev1"])
        self.assertFalse(devices[0].is_connected)


class CreateDevicesConnectionTest(unittest.TestCase):
    """`create_devices()` must not build a device that was positively
    detected as disconnected - otherwise the driver fails later, deep
    inside task creation, with a generic DAQmx error."""

    def _channel(self, hardware_channel: str = "cDAQ2Mod1/ai0") -> Channel:
        return Channel(hardware_channel, "Force", module_type=ModuleType.NI9215)

    def _device_info(self, is_connected: bool) -> measurement.DeviceInfo:
        return measurement.DeviceInfo(
            device_name="cDAQ2Mod1",
            product_type="NI 9215",
            module_type=ModuleType.NI9215,
            num_channels=2,
            is_connected=is_connected,
        )

    def test_refuses_disconnected_device(self) -> None:
        with self.assertRaises(measurement.MeasurementConfigError) as ctx:
            measurement.create_devices(
                [self._channel()], discovered_devices=[self._device_info(False)]
            )

        self.assertIn("cDAQ2Mod1", str(ctx.exception))

    def test_accepts_connected_device(self) -> None:
        devices = measurement.create_devices(
            [self._channel()], discovered_devices=[self._device_info(True)]
        )

        self.assertEqual(len(devices), 1)

    def test_without_discovery_result_behavior_is_unchanged(self) -> None:
        """No probe has run, so nothing was positively detected as
        disconnected - a configuration loaded without a preceding device
        search must not be blocked here."""
        devices = measurement.create_devices([self._channel()], discovered_devices=None)

        self.assertEqual(len(devices), 1)


class SetupViewProblemReportingTest(unittest.TestCase):
    """`SetupView.set_discovered_devices()` must not turn an unchanged
    problem into a recurring modal dialog.

    Switching to the setup view triggers a device search on its own
    (`gui/main_window.py::_on_nav_changed`), so an unplugged chassis
    would otherwise pop up a warning on EVERY switch - while already
    being visible, grayed out, in the device list.

    Built via `__new__` without running `__init__`, the pattern already
    used for `SetupView` in `tests/test_trigger_headless.py`: only the
    few attributes this one method touches are needed.
    """

    def setUp(self) -> None:
        from PyQt6.QtWidgets import QApplication, QTreeWidget
        from gui.setup_view import SetupView

        self._app = QApplication.instance() or QApplication([])

        class _FakeChannelTable:
            def set_available_devices(self, devices):
                pass

        self.view = SetupView.__new__(SetupView)
        self.view._device_list = QTreeWidget()
        self.view._channel_table = _FakeChannelTable()
        self.view._discovered_devices = None
        self.view._reported_problem_devices = set()

        self.warnings: list[str] = []

    def _set_devices(self, devices: list) -> list[str]:
        """Runs a discovery result through the view and returns the
        titles of the dialogs it raised."""
        from unittest.mock import patch

        self.warnings = []
        with patch(
            "gui.setup_view.QMessageBox.warning",
            side_effect=lambda parent, title, body: self.warnings.append(title),
        ):
            self.view.set_discovered_devices(devices)
        return list(self.warnings)

    def _device(
        self,
        name: str,
        is_connected: bool = True,
        supported: bool = True,
        probed: bool = True,
    ):
        from data.models import DeviceInfo

        # `probed=True` by default: these cases describe the state after
        # a COMPLETED discovery. `probed=False` is what stage one
        # delivers - listed from the configuration database, reachability
        # not yet asked about.
        return DeviceInfo(
            device_name=name,
            product_type="NI 9215" if supported else "NI 9263",
            module_type=ModuleType.NI9215 if supported else None,
            num_channels=2,
            has_any_channels=True,
            physical_channels=[f"{name}/ai0", f"{name}/ai1"],
            is_connected=is_connected,
            connection_probed=probed,
        )

    def test_disconnected_device_reported_once_then_suppressed(self) -> None:
        offline = self._device("cDAQ2Mod1", is_connected=False)

        self.assertEqual(len(self._set_devices([offline])), 1)
        # Same problem, e.g. the user switching back to setup.
        self.assertEqual(self._set_devices([offline]), [])
        self.assertEqual(self._set_devices([offline]), [])

    def test_newly_disconnected_device_is_reported(self) -> None:
        first = self._device("cDAQ2Mod1", is_connected=False)
        second = self._device("cDAQ3Mod1", is_connected=False)

        self.assertEqual(len(self._set_devices([first])), 1)
        self.assertEqual(len(self._set_devices([first, second])), 1)

    def test_device_reported_again_after_recovering_and_dropping_out(self) -> None:
        name = "cDAQ2Mod1"

        self.assertEqual(len(self._set_devices([self._device(name, is_connected=False)])), 1)
        self.assertEqual(self._set_devices([self._device(name, is_connected=True)]), [])
        self.assertEqual(len(self._set_devices([self._device(name, is_connected=False)])), 1)

    def test_empty_result_clears_the_memo(self) -> None:
        """All devices gone (driver restarted, chassis removed from MAX):
        a device that returns and is still a problem must be reported."""
        offline = self._device("cDAQ2Mod1", is_connected=False)

        self.assertEqual(len(self._set_devices([offline])), 1)
        self.assertEqual(self._set_devices([]), [])
        self.assertEqual(len(self._set_devices([offline])), 1)

    def test_disconnected_device_is_not_also_reported_as_unsupported(self) -> None:
        """Its module type comes from the same stale cache - naming it
        'unsupported' would report the wrong cause."""
        offline = self._device("cDAQ2Mod1", is_connected=False, supported=False)

        titles = self._set_devices([offline])
        self.assertEqual(len(titles), 1)
        self.assertNotIn("unsupported_modules_title", titles[0].lower().replace(" ", "_"))

    def test_unprobed_device_is_not_reported_as_disconnected(self) -> None:
        """Stage one has no probe result - an unprobed device must not be
        declared disconnected on the strength of a default value."""
        unprobed = self._device("cDAQ2Mod1", is_connected=True, probed=False)

        self.assertEqual(self._set_devices([unprobed]), [])

    def test_stage_one_does_not_clear_the_memo_of_a_reported_device(self) -> None:
        """The regression this guards: every entry into the setup view
        runs stage one, then stage two. If stage one - which knows
        nothing about reachability - reset the memo, stage two would pop
        up a modal dialog about the same unchanged device every time.
        """
        name = "cDAQ2Mod1"
        stage_one = self._device(name, is_connected=True, probed=False)
        stage_two = self._device(name, is_connected=False, probed=True)

        # First visit: listed, then probed and reported once.
        self.assertEqual(self._set_devices([stage_one]), [])
        self.assertEqual(len(self._set_devices([stage_two])), 1)

        # Second visit, unchanged situation: silent in both stages.
        self.assertEqual(self._set_devices([stage_one]), [])
        self.assertEqual(self._set_devices([stage_two]), [])


class TwoStageDiscoveryTest(unittest.TestCase):
    """`discover_devices(probe_connections=False)` plus
    `probe_device_connections()` - the split that keeps the probe off the
    path the user waits on.
    """

    def setUp(self) -> None:
        self.probe_log: list[str] = []
        modules = [
            _FakeDevice("cDAQ1Mod1", "NI 9215", "cDAQ1", probe_log=self.probe_log),
            _FakeDevice("cDAQ1Mod2", "NI 9234", "cDAQ1", probe_log=self.probe_log),
            _FakeDevice("cDAQ2Mod1", "NI 9215", "cDAQ2", probe_log=self.probe_log),
        ]
        by_name = {module.name: module for module in modules}
        by_name["cDAQ1"] = _FakeDevice(
            "cDAQ1", "cDAQ-9189", num_ai_channels=0, reachable=True, probe_log=self.probe_log
        )
        by_name["cDAQ2"] = _FakeDevice(
            "cDAQ2", "cDAQ-9189", num_ai_channels=0, reachable=False, probe_log=self.probe_log
        )

        self._originals = (
            nidaq_device.NIDAQMX_AVAILABLE,
            nidaq_device.System,
            nidaq_device.DaqmxError,
        )
        nidaq_device.NIDAQMX_AVAILABLE = True
        nidaq_device.System = _FakeSystemFactory(modules, by_name)
        nidaq_device.DaqmxError = _FakeDaqmxError

    def tearDown(self) -> None:
        (
            nidaq_device.NIDAQMX_AVAILABLE,
            nidaq_device.System,
            nidaq_device.DaqmxError,
        ) = self._originals

    def test_stage_one_lists_devices_without_touching_the_hardware(self) -> None:
        devices = nidaq_device.discover_devices(probe_connections=False)

        self.assertEqual(
            [d.device_name for d in devices], ["cDAQ1Mod1", "cDAQ1Mod2", "cDAQ2Mod1"]
        )
        self.assertEqual(self.probe_log, [])
        self.assertTrue(all(not d.connection_probed for d in devices))
        # Optimistic default, deliberately: the unprobed state is the one
        # the app showed before the probe existed at all.
        self.assertTrue(all(d.is_connected for d in devices))

    def test_stage_two_reports_reachability_per_device(self) -> None:
        devices = nidaq_device.discover_devices(probe_connections=False)

        states = nidaq_device.probe_device_connections(devices)

        self.assertEqual(
            states, {"cDAQ1Mod1": True, "cDAQ1Mod2": True, "cDAQ2Mod1": False}
        )

    def test_stage_two_probes_once_per_chassis_not_per_module(self) -> None:
        devices = nidaq_device.discover_devices(probe_connections=False)

        nidaq_device.probe_device_connections(devices)

        # Probes run concurrently, so the ORDER is not fixed - only that
        # each chassis was probed exactly once.
        self.assertEqual(sorted(self.probe_log), ["cDAQ1", "cDAQ2"])

    def test_stage_one_and_two_together_match_the_single_stage_result(self) -> None:
        """The split must not change the outcome, only its timing."""
        single_stage = {
            d.device_name: d.is_connected for d in nidaq_device.discover_devices()
        }
        self.probe_log.clear()

        devices = nidaq_device.discover_devices(probe_connections=False)
        two_stage = nidaq_device.probe_device_connections(devices)

        self.assertEqual(two_stage, single_stage)

    def test_device_gone_from_the_driver_is_left_out(self) -> None:
        """Removed in NI-MAX between the two stages: reported as unknown
        rather than as disconnected - the next run drops it anyway."""
        from data.models import DeviceInfo

        states = nidaq_device.probe_device_connections(
            [DeviceInfo(device_name="cDAQ9Mod1", product_type="NI 9215")]
        )

        self.assertEqual(states, {})

    def test_probe_without_devices_does_not_touch_the_hardware(self) -> None:
        self.assertEqual(nidaq_device.probe_device_connections([]), {})
        self.assertEqual(self.probe_log, [])

    def test_probe_without_nidaqmx_keeps_the_optimistic_state(self) -> None:
        """Empty rather than all-False: a missing driver is not evidence
        that the devices are unreachable."""
        from data.models import DeviceInfo

        nidaq_device.NIDAQMX_AVAILABLE = False
        devices = [DeviceInfo(device_name="cDAQ1Mod1", product_type="NI 9215")]

        self.assertEqual(nidaq_device.probe_device_connections(devices), {})
        self.assertEqual(self.probe_log, [])


if __name__ == "__main__":
    unittest.main()
