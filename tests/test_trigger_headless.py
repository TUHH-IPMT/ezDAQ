from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PyQt6.QtWidgets import QApplication, QLineEdit, QTreeWidgetItem

from config.configuration_manager import ConfigurationManager
from config.sensor_database import SensorDatabaseManager
from core.controller import MeasurementController
from core.measurement import (
    _device_name_from_channel,
    device_name_from_hw_channel,
)
from core.measurement import MeasurementConfigError
from data.models import (
    Channel,
    MeasurementConfig,
    ModuleType,
    TriggerCondition,
    TriggerConfig,
    TriggerDirection,
    TriggerKind,
    resolve_rate_groups,
)
from data.sensor_models import SensorEntry
from gui.live_view import ChannelDisplayDialog, LiveView
from gui.analysis_view import AnalysisView
from gui.analysis_view import ChannelTreeWidget
from gui.setup_view import SetupView
from gui.widgets.channel_table import ChannelTableWidget


class _SignalSpy:
    def __init__(self) -> None:
        self.count = 0

    def emit(self) -> None:
        self.count += 1


class TriggerModelTests(unittest.TestCase):
    def test_channel_display_dialog_keeps_popout_consistent_with_visibility(self) -> None:
        app = QApplication.instance() or QApplication([])
        channel = Channel(
            hardware_channel="ai0",
            display_name="Channel",
            plot_visible=False,
            plot_popout=True,
        )
        dialog = ChannelDisplayDialog([channel], "#ffffff", "#000000", "#cccccc")
        key = (channel.hardware_channel, channel.display_name)
        row = dialog._rows[key]
        visible_check = row["visible"]
        popout_check = row["popout"]

        self.assertFalse(popout_check.isEnabled())
        self.assertFalse(dialog.results()[key]["plot_popout"])

        visible_check.setChecked(True)
        self.assertTrue(popout_check.isEnabled())
        popout_check.setChecked(True)
        visible_check.setChecked(False)
        self.assertFalse(dialog.results()[key]["plot_popout"])

        dialog.deleteLater()
        app.processEvents()

    def test_setup_view_preserves_discovery_sentinel(self) -> None:
        setup_view = SetupView.__new__(SetupView)

        setup_view._discovered_devices = None
        self.assertIsNone(setup_view.get_discovered_devices())

        setup_view._discovered_devices = []
        self.assertEqual(setup_view.get_discovered_devices(), [])

    def test_device_name_extraction_has_shared_and_strict_paths(self) -> None:
        self.assertEqual(device_name_from_hw_channel("cDAQ1Mod1/ai0"), "cDAQ1Mod1")
        self.assertEqual(device_name_from_hw_channel(""), "")
        self.assertEqual(
            _device_name_from_channel(Channel("cDAQ1Mod1/ai0", "Channel")),
            "cDAQ1Mod1",
        )
        with self.assertRaises(MeasurementConfigError):
            _device_name_from_channel(Channel("invalid", "Channel"))

    def test_analysis_filter_shows_no_results_hint(self) -> None:
        app = QApplication.instance() or QApplication([])
        analysis_view = AnalysisView.__new__(AnalysisView)
        analysis_view._tree = ChannelTreeWidget()
        analysis_view._tree_search_edit = QLineEdit()
        analysis_view._tree.addTopLevelItem(QTreeWidgetItem(["measurement.csv"]))
        analysis_view._tree_search_edit.setText("missing")

        analysis_view._apply_tree_filter()

        self.assertFalse(analysis_view._tree._empty_hint_label.isHidden())
        self.assertEqual(
            analysis_view._tree._empty_hint_label.text(),
            "Keine Treffer für diese Suche.",
        )

        analysis_view._tree.takeTopLevelItem(0)
        analysis_view._tree_search_edit.clear()
        analysis_view._apply_tree_filter()
        self.assertFalse(analysis_view._tree._empty_hint_label.isHidden())

        analysis_view._tree.deleteLater()
        analysis_view._tree_search_edit.deleteLater()
        app.processEvents()

    def test_channel_table_syncs_adc_timing_mode_per_ni9213_module(self) -> None:
        app = QApplication.instance() or QApplication([])
        table = ChannelTableWidget()
        table.set_channels(
            [
                Channel(
                    "cDAQ1Mod1/ai0",
                    "Channel 1",
                    module_type=ModuleType.NI9213,
                    adc_timing_mode="HIGH_SPEED",
                ),
                Channel(
                    "cDAQ1Mod1/ai1",
                    "Channel 2",
                    module_type=ModuleType.NI9213,
                    adc_timing_mode="AUTOMATIC",
                ),
            ]
        )

        self.assertEqual(
            [channel.adc_timing_mode for channel in table.get_channels()],
            ["HIGH_SPEED", "HIGH_SPEED"],
        )

        table.deleteLater()
        app.processEvents()

    def test_controller_rejects_new_measurement_until_session_is_cleaned_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = MeasurementController(ConfigurationManager(Path(directory)))
            controller._session = object()
            controller._acquisition_thread = None

            with self.assertRaisesRegex(RuntimeError, "stop_measurement"):
                controller.start_measurement(MeasurementConfig("Test", 1000.0))

    def test_limited_measurement_rejects_non_positive_stop_value(self) -> None:
        with self.assertRaisesRegex(ValueError, "größer als 0"):
            MeasurementConfig(
                "Limited",
                1000.0,
                recording_unlimited=False,
                recording_stop_value=0.0,
            )

        MeasurementConfig("Unlimited", 1000.0, recording_stop_value=0.0)

    def test_ni9210_alone_ignores_target_rate_and_resolves_to_14hz(self) -> None:
        # Ein alleinstehender NI9210 hat keine "andere" Gruppe, die die
        # Zielrate erfuellen muesste - resolve_rate_groups() loest ihn
        # immer auf seine feste 14 S/s auf, unabhaengig vom Zielwert.
        channels = [
            Channel(
                "cDAQ1Mod1/ai0",
                "Temperature",
                module_type=ModuleType.NI9210,
            )
        ]

        config = MeasurementConfig("NI9210 only", 1000.0, channels=channels)
        groups = resolve_rate_groups(config.active_channels(), config.sample_rate_hz)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].resolved_sample_rate_hz, 14.0)

        MeasurementConfig("Valid NI9210", 14.0, channels=channels)

    def test_ni9210_combined_with_faster_module_yields_two_rate_groups(self) -> None:
        # Kernverhalten von Phase A: NI9210 + ein schnelleres Modul ist
        # kein Fehler mehr, sondern zwei getrennte Ratengruppen (siehe
        # data/models.py::resolve_rate_groups).
        channels = [
            Channel("cDAQ1Mod1/ai0", "Temperature", module_type=ModuleType.NI9210),
            Channel("cDAQ1Mod2/ai0", "Vibration", module_type=ModuleType.NI9234),
        ]
        target_rate = 51_200.0 / 31  # gueltige NI9234-Rate

        config = MeasurementConfig("Mixed rates", target_rate, channels=channels)
        groups = resolve_rate_groups(config.active_channels(), config.sample_rate_hz)

        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0].resolved_sample_rate_hz, target_rate)
        self.assertEqual(groups[1].resolved_sample_rate_hz, 14.0)

    def test_ni9210_combined_with_invalid_ni9234_rate_still_raises(self) -> None:
        # Intrinsische Ratenverstoesse (hier: NI9234-Raster) bleiben
        # weiterhin Fehler - unabhaengig davon, ob zusaetzlich ein NI9210
        # in der Messung ist.
        channels = [
            Channel("cDAQ1Mod1/ai0", "Temperature", module_type=ModuleType.NI9210),
            Channel("cDAQ1Mod2/ai0", "Vibration", module_type=ModuleType.NI9234),
        ]

        with self.assertRaisesRegex(ValueError, "51200 Hz / n"):
            MeasurementConfig("Invalid NI9234 rate", 1000.0, channels=channels)

    def test_sensor_database_ignores_invalid_sensor_data(self) -> None:
        invalid_documents = [
            [{"name": "broken", "channels": [{"signal_type": "removed-type"}]}],
            [{"name": "broken"}, "not-a-sensor"],
            {"sensors": {"not": "a-list"}},
        ]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sensor_database.json"
            for document in invalid_documents:
                path.write_text(json.dumps(document), encoding="utf-8")
                manager = SensorDatabaseManager(Path(directory))
                self.assertEqual(manager.list_sensors(), [])

            path.write_text(
                json.dumps([SensorEntry(name="valid").to_dict()]),
                encoding="utf-8",
            )
            self.assertEqual(
                [sensor.name for sensor in SensorDatabaseManager(Path(directory)).list_sensors()],
                ["valid"],
            )

    def test_trigger_config_round_trip_preserves_start_and_stop(self) -> None:
        config = TriggerConfig(
            start=TriggerCondition(
                kind=TriggerKind.THRESHOLD,
                threshold_channel_hardware_id="cDAQ1Mod1/ai0",
                threshold_value=2.5,
                threshold_direction=TriggerDirection.RISES_ABOVE,
            ),
            stop=TriggerCondition(
                kind=TriggerKind.SERIAL,
                serial_port="COM7",
                serial_baud_rate=115200,
                serial_expected_message="STOP",
            ),
            pretrigger_seconds=1.25,
        )

        restored = TriggerConfig.from_dict(config.to_dict())

        self.assertEqual(restored, config)

    def test_configuration_manager_persists_one_nested_trigger_dict(self) -> None:
        config = TriggerConfig(
            start=TriggerCondition(kind=TriggerKind.SERIAL, serial_port="COM3"),
            stop=TriggerCondition(
                kind=TriggerKind.THRESHOLD,
                threshold_channel_hardware_id="ai1",
                threshold_value=-4.0,
                threshold_direction=TriggerDirection.FALLS_BELOW,
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            manager = ConfigurationManager(Path(directory))
            manager.update_last_trigger_settings(config)
            restored = ConfigurationManager(Path(directory))

            self.assertEqual(restored.settings.last_trigger_config, config.to_dict())
            self.assertNotIn("last_start_trigger", restored.settings.to_dict())
            self.assertNotIn("last_stop_trigger", restored.settings.to_dict())


class LiveViewStopTriggerTests(unittest.TestCase):
    def test_popout_close_clears_y_range_cache(self) -> None:
        live_view = LiveView.__new__(LiveView)
        live_view._popout_windows = {"ai0": object()}
        live_view._popout_y_auto_active = {"ai0": False}

        with patch("gui.live_view.sip.isdeleted", return_value=True):
            live_view._on_popout_window_closed("ai0")

        self.assertNotIn("ai0", live_view._popout_windows)
        self.assertNotIn("ai0", live_view._popout_y_auto_active)

    def test_analysis_busy_state_is_reference_counted(self) -> None:
        app = QApplication.instance() or QApplication([])
        analysis_view = AnalysisView.__new__(AnalysisView)
        analysis_view._busy_count = 0
        analysis_view._function_buttons = {}

        analysis_view._begin_busy()
        analysis_view._begin_busy()
        self.assertEqual(analysis_view._busy_count, 2)

        analysis_view._end_busy()
        self.assertEqual(analysis_view._busy_count, 1)
        analysis_view._end_busy()
        self.assertEqual(analysis_view._busy_count, 0)
        analysis_view._end_busy()
        self.assertEqual(analysis_view._busy_count, 0)

        app.processEvents()

    def _live_view_for_stop_check(self, condition: TriggerCondition) -> LiveView:
        live_view = LiveView.__new__(LiveView)
        live_view._armed = False
        live_view._trigger_config = TriggerConfig(stop=condition)
        live_view._stop_trigger_channel_index = 0
        live_view._stop_trigger_last_condition = False
        live_view.stop_requested = _SignalSpy()
        return live_view

    def test_stop_threshold_emits_once_on_false_to_true_transition(self) -> None:
        live_view = self._live_view_for_stop_check(
            TriggerCondition(
                kind=TriggerKind.THRESHOLD,
                threshold_value=10.0,
                threshold_direction=TriggerDirection.RISES_ABOVE,
            )
        )

        live_view._check_stop_threshold_trigger(np.array([[5.0, 12.0]]))

        self.assertEqual(live_view.stop_requested.count, 1)
        self.assertTrue(live_view._stop_trigger_last_condition)

        live_view._check_stop_threshold_trigger(np.array([[15.0]]))
        self.assertEqual(live_view.stop_requested.count, 1)

    def test_mark_recording_started_resets_stop_edge_detector(self) -> None:
        live_view = self._live_view_for_stop_check(
            TriggerCondition(
                kind=TriggerKind.THRESHOLD,
                threshold_value=10.0,
                threshold_direction=TriggerDirection.RISES_ABOVE,
            )
        )
        live_view._recording_baseline_samples = 0

        live_view._check_stop_threshold_trigger(np.array([[12.0]]))
        self.assertEqual(live_view.stop_requested.count, 1)

        live_view.stop_requested = _SignalSpy()
        live_view.mark_recording_started(42)
        live_view._check_stop_threshold_trigger(np.array([[12.0]]))

        self.assertEqual(live_view._recording_baseline_samples, 42)
        self.assertEqual(live_view.stop_requested.count, 0)


if __name__ == "__main__":
    unittest.main()
