from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from config.configuration_manager import ConfigurationManager
from core.controller import MeasurementController
from data.models import (
    MeasurementConfig,
    TriggerCondition,
    TriggerConfig,
    TriggerDirection,
    TriggerKind,
)
from gui.live_view import LiveView


class _SignalSpy:
    def __init__(self) -> None:
        self.count = 0

    def emit(self) -> None:
        self.count += 1


class TriggerModelTests(unittest.TestCase):
    def test_controller_rejects_new_measurement_until_session_is_cleaned_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = MeasurementController(ConfigurationManager(Path(directory)))
            controller._session = object()
            controller._acquisition_thread = None

            with self.assertRaisesRegex(RuntimeError, "stop_measurement"):
                controller.start_measurement(MeasurementConfig("Test", 1000.0))

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
