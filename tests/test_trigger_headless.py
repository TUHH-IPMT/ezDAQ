from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
from PyQt6.QtWidgets import QApplication, QLineEdit, QTreeWidgetItem

from analysis.basic_analysis import native_samples
from config.configuration_manager import ConfigurationManager
from config.sensor_database import SensorDatabaseManager
from core.controller import MeasurementController
from core.rate_merge import DeviceGroup, RateMerger
from core.measurement import (
    _device_name_from_channel,
    device_name_from_hw_channel,
)
from core.measurement import MeasurementConfigError
from data.metadata import build_measurement_metadata
from data.models import (
    Channel,
    MeasurementConfig,
    MeasurementSession,
    ModuleType,
    RateGroup,
    TriggerCondition,
    TriggerConfig,
    TriggerDirection,
    TriggerKind,
    resolve_rate_groups,
)
from data.loader import LoadedMeasurement
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


class _FakeAcquisitionDevice:
    """Minimaler Fake für `hardware.base_device.BaseDevice` in
    `RateMerger`-Tests - simuliert eine Hardware, deren Samples erst mit
    Verzögerung tatsächlich verfügbar werden (siehe
    `core/rate_merge.py`-Moduldocstring: laut `due()` fällig != vom
    Treiber tatsächlich schon geliefert). `read()` wirft bewusst einen
    `AssertionError`, falls jemals mehr angefordert wird, als
    `available_samples()` zuvor gemeldet hat - genau die Garantie, die
    der Fix in `RateMerger.read_merged_block` sicherstellen soll."""

    def __init__(self, num_channels: int = 1) -> None:
        self.active_channels = [object() for _ in range(num_channels)]
        self._produced = 0
        self._consumed = 0

    def produce(self, count: int) -> None:
        self._produced += count

    def available_samples(self) -> int:
        return self._produced - self._consumed

    def read(self, samples_per_channel: int, timeout: float = 10.0) -> np.ndarray:
        assert samples_per_channel <= self.available_samples(), (
            "RateMerger darf nie mehr anfordern, als available_samples() meldet"
        )
        start = self._consumed
        self._consumed += samples_per_channel
        values = np.arange(start, start + samples_per_channel, dtype=np.float64)
        return np.tile(values, (len(self.active_channels), 1))


class RateMergerTests(unittest.TestCase):
    def test_slow_group_never_blocks_and_catches_up_once_hardware_delivers(self) -> None:
        # Fast-Geraet hat "immer genug" (steht fuer den blockierenden,
        # aber in der Praxis schnell erfuellten Read der schnellen
        # Gruppe) - Rate 100 vs. 10 (Verhaeltnis 10), 10 Samples/Zyklus.
        fast_device = _FakeAcquisitionDevice()
        fast_device.produce(10_000)
        slow_device = _FakeAcquisitionDevice()

        fast_group = DeviceGroup(devices=[fast_device], resolved_sample_rate_hz=100.0)
        slow_group = DeviceGroup(devices=[slow_device], resolved_sample_rate_hz=10.0)
        merger = RateMerger([fast_group, slow_group], read_timeout_seconds=1.0)

        # Zyklus 1: ein langsames Sample waere laut due() faellig, die
        # "Hardware" hat es aber noch NICHT produziert - kein Blockieren/
        # Fehler, der zuletzt bekannte Wert (0.0) wird gehalten.
        block = merger.read_merged_block(10)
        self.assertEqual(block.shape, (2, 10))
        np.testing.assert_array_equal(block[1], np.zeros(10))
        self.assertEqual(slow_device._consumed, 0)

        # Zyklus 2: weiterhin nichts produziert - Rueckstand waechst auf
        # 2 faellige Samples, weiterhin kein Blockieren.
        block = merger.read_merged_block(10)
        np.testing.assert_array_equal(block[1], np.zeros(10))

        # Jetzt liefert die "Hardware" die beiden inzwischen faelligen
        # Samples nach - RateMerger muss den Rueckstand automatisch
        # nachholen, sobald verfuegbar.
        slow_device.produce(2)
        block = merger.read_merged_block(10)
        self.assertEqual(slow_device._consumed, 2)
        self.assertTrue(np.all(block[1] == 1.0))


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

    def test_ni9210_joins_shared_group_when_target_rate_matches_its_fixed_rate(self) -> None:
        # Entspricht die Zielrate zufaellig genau der festen NI9210-Rate
        # (14 S/s), gibt es keinen Ratenkonflikt - der NI9210 bleibt dann
        # im selben Task wie andere, damit kompatible Module (hier
        # NI9215, das keine Ratenbeschraenkung hat), statt unnoetig eine
        # eigene Merge-Gruppe zu bekommen.
        channels = [
            Channel("cDAQ1Mod1/ai0", "Temperature", module_type=ModuleType.NI9210),
            Channel("cDAQ1Mod2/ai0", "Voltage", module_type=ModuleType.NI9215),
        ]

        config = MeasurementConfig("Matching fixed rate", 14.0, channels=channels)
        groups = resolve_rate_groups(config.active_channels(), config.sample_rate_hz)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].resolved_sample_rate_hz, 14.0)
        self.assertEqual(len(groups[0].channels), 2)

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

    def test_metadata_reflects_actual_tick_rate_and_native_rate_per_channel(self) -> None:
        # Phase B: Metadaten muessen die TATSAECHLICHE Tick-Rate (nicht
        # die rohe Zielrate) unter "sample_rate_hz" fuehren, da
        # StorageWriter die time_s-Spalte damit berechnet - sowie die
        # aufgeloesten Ratengruppen und die native Rate je Kanal.
        channels = [
            Channel("cDAQ1Mod1/ai0", "Temperature", module_type=ModuleType.NI9210),
            Channel("cDAQ1Mod2/ai0", "Vibration", module_type=ModuleType.NI9234),
        ]
        target_rate = 51_200.0 / 31
        config = MeasurementConfig("Mixed rates", target_rate, channels=channels)
        session = MeasurementSession(config=config, start_time=datetime.now())

        metadata = build_measurement_metadata(session, device_infos=[])

        self.assertEqual(metadata["sample_rate_hz"], target_rate)
        self.assertEqual(metadata["target_sample_rate_hz"], target_rate)
        self.assertEqual(len(metadata["rate_groups"]), 2)

        native_rates = {ch["hardware_channel"]: ch["native_sample_rate_hz"] for ch in metadata["channels"]}
        self.assertEqual(native_rates["cDAQ1Mod1/ai0"], 14.0)
        self.assertEqual(native_rates["cDAQ1Mod2/ai0"], target_rate)

    def test_native_samples_removes_consecutive_duplicates(self) -> None:
        # Zero-Order-Hold-Treppenstufe (siehe core/rate_merge.py::RateMerger):
        # nur der jeweils ERSTE einer Folge identischer Werte ist ein
        # echtes neues Sample.
        data = pd.DataFrame({
            "time_s": [0.0, 0.1, 0.2, 0.3, 0.4, 0.5],
            "Temp": [10.0, 10.0, 10.0, 11.0, 11.0, 12.0],
        })

        reduced = native_samples(data, "Temp")

        self.assertEqual(list(reduced["Temp"]), [10.0, 11.0, 12.0])
        self.assertEqual(list(reduced["time_s"]), [0.0, 0.3, 0.5])

    def test_prepare_channel_for_rate_aware_analysis_decimates_forward_filled_channel(self) -> None:
        # Mischt einen forward-gefuellten NI9210-Kanal (native 14 S/s)
        # mit einem echten schnellen NI9234-Kanal (native == Tick-Rate)
        # in derselben Datei - nur der NI9210-Kanal darf entdoppelt werden.
        tick_rate = 100.0
        data = pd.DataFrame({
            "time_s": np.arange(6) / tick_rate,
            "Temp": [10.0, 10.0, 10.0, 11.0, 11.0, 11.0],
            "Vib": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
        })
        metadata = {
            "sample_rate_hz": tick_rate,
            "channels": [
                {"hardware_channel": "cDAQ1Mod4/ai0", "display_name": "Temp", "native_sample_rate_hz": 14.0},
                {"hardware_channel": "cDAQ1Mod1/ai0", "display_name": "Vib", "native_sample_rate_hz": tick_rate},
            ],
        }
        measurement = LoadedMeasurement(
            data=data, channels=[], metadata=metadata, source_path=Path("test.parquet"), x_column="time_s",
        )
        analysis_view = AnalysisView.__new__(AnalysisView)

        temp_data, temp_rate = analysis_view._prepare_channel_for_rate_aware_analysis(
            measurement, "Temp", tick_rate
        )
        self.assertEqual(temp_rate, 14.0)
        self.assertEqual(list(temp_data["Temp"]), [10.0, 11.0])

        vib_data, vib_rate = analysis_view._prepare_channel_for_rate_aware_analysis(
            measurement, "Vib", tick_rate
        )
        self.assertEqual(vib_rate, tick_rate)
        self.assertIs(vib_data, data)

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

    def test_resolve_rate_groups_snaps_ni9234_rate_to_exact_valid_value(self) -> None:
        # DAQmx rundet eine Rate, die auch nur minimal UEBER einem
        # gueltigen NI9234-Wert liegt, auf den NAECHSTHOEHEREN gueltigen
        # Wert auf (nicht auf den naechstgelegenen) - an echter Hardware
        # verifiziert: 17066.7 (0.03 Hz ueber 51200/3) sprang intern auf
        # 25600 Hz (51200/2). resolve_rate_groups() muss deshalb auf den
        # EXAKTEN gueltigen Wert einrasten, bevor er an die Hardware-
        # Schicht weitergereicht wird - nicht die rohe (z. B. auf eine
        # Nachkommastelle gerundete) Zielrate durchreichen.
        channels = [Channel("cDAQ1Mod1/ai0", "Vibration", module_type=ModuleType.NI9234)]
        exact_valid_rate = 51_200.0 / 3
        raw_rounded_target = 17066.7  # wie von der 1-Nachkommastellen-Spinbox geliefert

        self.assertNotEqual(raw_rounded_target, exact_valid_rate)
        groups = resolve_rate_groups(channels, raw_rounded_target)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].resolved_sample_rate_hz, exact_valid_rate)

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

    def test_sample_rate_label_shows_plain_rate_for_single_group(self) -> None:
        live_view = LiveView.__new__(LiveView)
        live_view._sample_rate_hz = 1651.6129032258063
        live_view._rate_groups = []

        self.assertEqual(live_view._format_sample_rate_label_value(), "1651.6 Hz")

    def test_sample_rate_label_shows_fixed_group_rate_for_multiple_groups(self) -> None:
        live_view = LiveView.__new__(LiveView)
        live_view._sample_rate_hz = 1651.6129032258063
        live_view._rate_groups = [
            RateGroup(
                channels=[Channel("cDAQ1Mod1/ai0", "Vib", module_type=ModuleType.NI9234)],
                resolved_sample_rate_hz=1651.6129032258063,
                reason="Zielrate",
            ),
            RateGroup(
                channels=[Channel("cDAQ1Mod2/ai0", "Temp", module_type=ModuleType.NI9210)],
                resolved_sample_rate_hz=14.0,
                reason="NI9210 (feste 14.0 S/s)",
            ),
        ]

        self.assertEqual(live_view._format_sample_rate_label_value(), "1651.6 Hz (+ NI9210 @ 14.0 Hz)")

    def test_write_to_display_buffer_fast_channel_writes_every_row_unchanged(self) -> None:
        # Regressionsschutz: ein Kanal an der Tick-Rate selbst darf NIE der
        # Reduktion auf native-Rate-Samples unterliegen, auch wenn er
        # zufaellig wiederholte Werte liefert (echtes, gueltiges
        # Messsignal) - Verhalten muss exakt wie vor dem Fix bleiben.
        live_view = LiveView.__new__(LiveView)
        live_view._channels = [Channel("cDAQ1Mod2/ai0", "Vib", module_type=ModuleType.NI9234)]
        live_view._sample_rate_hz = 1000.0
        live_view._channel_native_rates = {0: 1000.0}
        live_view._display_capacity_samples = 10
        live_view._display_buffer = np.zeros((1, 10))
        live_view._channel_buffer_positions = {0: 0}
        live_view._channel_cycle_starts = {0: 0.0}
        live_view._channel_total_ticks_seen = {0: 0}

        block = np.array([[1.0, 1.0, 2.0, 2.0, 3.0]])
        live_view._write_to_display_buffer(block)

        self.assertEqual(live_view._channel_buffer_positions[0], 5)
        np.testing.assert_array_equal(live_view._display_buffer[0, :5], block[0])

    def test_channel_display_capacity_uses_native_rate_not_tick_rate(self) -> None:
        live_view = LiveView.__new__(LiveView)
        temp = Channel("cDAQ1Mod1/ai0", "Temp", module_type=ModuleType.NI9210)
        vib = Channel("cDAQ1Mod2/ai0", "Vib", module_type=ModuleType.NI9234)
        live_view._channels = [temp, vib]
        live_view._sample_rate_hz = 20000.0
        live_view._channel_native_rates = {0: 14.0, 1: 20000.0}
        live_view._display_capacity_samples = 200_000

        self.assertEqual(
            live_view._channel_display_capacity(0), int(14.0 * temp.plot_time_window_seconds)
        )
        self.assertEqual(
            live_view._channel_display_capacity(1), int(20000.0 * vib.plot_time_window_seconds)
        )
        self.assertLess(
            live_view._channel_display_capacity(0), live_view._channel_display_capacity(1)
        )

    def test_write_to_display_buffer_slow_channel_advances_on_due_ticks_even_with_constant_values(self) -> None:
        # Regressionstest fuer einen gemeldeten Realzeit-Versatz: ein
        # STABILER (unveraenderter) Messwert eines langsamen Kanals (z. B.
        # ein Thermoelement, das sich kaum aendert) muss trotzdem im
        # korrekten Takt als neue Samples gezaehlt werden - rein anhand
        # der verstrichenen Tick-Anzahl, NICHT anhand von Wertaenderungen
        # (ein frueherer, wertbasierter Ansatz haette das faelschlich als
        # eine einzige ZOH-Wiederholung verworfen und die Sweep-Anzeige
        # gegenueber der echten Messzeit nachlaufen lassen).
        live_view = LiveView.__new__(LiveView)
        temp = Channel("cDAQ1Mod1/ai0", "Temp", module_type=ModuleType.NI9210)
        live_view._channels = [temp]
        live_view._sample_rate_hz = 140.0  # Verhaeltnis 10:1 zur nativen Rate
        live_view._channel_native_rates = {0: 14.0}
        live_view._display_capacity_samples = 1000
        live_view._display_buffer = np.zeros((1, 1000))
        live_view._channel_buffer_positions = {0: 0}
        live_view._channel_cycle_starts = {0: 0.0}
        live_view._channel_total_ticks_seen = {0: 0}

        # 25 rohe Tick-Zeilen (Verhaeltnis 10:1) -> 2 faellige native
        # Samples, Wert komplett KONSTANT.
        block1 = np.full((1, 25), 10.0)
        live_view._write_to_display_buffer(block1)
        self.assertEqual(live_view._channel_buffer_positions[0], 2)
        np.testing.assert_array_equal(live_view._display_buffer[0, :2], [10.0, 10.0])

        # Weitere 4 rohe Tick-Zeilen (insgesamt 29) reichen noch nicht
        # fuer ein drittes faelliges Sample (erst bei 30 Ticks).
        live_view._write_to_display_buffer(np.full((1, 4), 10.0))
        self.assertEqual(live_view._channel_buffer_positions[0], 2)

    def test_channel_cycle_start_increments_by_native_rate_elapsed_seconds(self) -> None:
        live_view = LiveView.__new__(LiveView)
        temp = Channel("cDAQ1Mod1/ai0", "Temp", module_type=ModuleType.NI9210)
        live_view._channels = [temp]
        live_view._sample_rate_hz = 140.0  # Verhaeltnis 10:1 zur nativen Rate
        live_view._channel_native_rates = {0: 14.0}
        cap = LiveView._capacity_for_rate(14.0, temp.plot_time_window_seconds)
        live_view._display_capacity_samples = cap
        live_view._display_buffer = np.zeros((1, cap))
        live_view._channel_buffer_positions = {0: 0}
        live_view._channel_cycle_starts = {0: 0.0}
        live_view._channel_total_ticks_seen = {0: 0}

        # `cap * 10` rohe Tick-Zeilen (Verhaeltnis 10:1) fuellen den
        # Durchlauf exakt mit `cap` faelligen nativen Samples.
        raw_ticks = cap * 10
        block = np.arange(raw_ticks, dtype=float).reshape(1, -1)
        live_view._write_to_display_buffer(block)
        self.assertEqual(live_view._channel_buffer_positions[0], cap)
        self.assertEqual(live_view._channel_cycle_starts[0], 0.0)

        # Zehn weitere rohe Tick-Zeilen (= genau 1 faelliges natives
        # Sample) loesen den Umbruch aus.
        live_view._write_to_display_buffer(np.full((1, 10), 999.0))
        self.assertAlmostEqual(live_view._channel_cycle_starts[0], cap / 14.0)
        self.assertAlmostEqual(live_view._channel_cycle_starts[0], temp.plot_time_window_seconds)
        self.assertEqual(live_view._channel_buffer_positions[0], 1)


class CalculateSamplesPerReadTests(unittest.TestCase):
    """`SetupView._calculate_samples_per_read` ueber `__new__` isoliert
    getestet (gleiches Muster wie die `LiveView`-Tests oben) - die Methode
    braucht nur zwei einfache Zahlenattribute, keine Qt-Widgets."""

    def _make_setup_view(self) -> SetupView:
        setup_view = SetupView.__new__(SetupView)
        setup_view._target_read_block_ms = 25.0
        setup_view._max_samples_per_read = 2000
        return setup_view

    def test_low_rate_scales_down_instead_of_using_fixed_minimum(self) -> None:
        # Regressionstest: ein alleinstehendes NI9210 (14 S/s) bekam frueher
        # wegen einer festen Sample-Untergrenze (50) einen Block, der erst
        # nach 0,5s (bzw. urspruenglich sogar 3,6s) fertig war - sichtbar
        # hakelige Live View (Daten kamen in seltenen Schueben statt
        # kontinuierlich). Die Blockgroesse muss stattdessen mit der Rate
        # mitskalieren, hier auf 1 Sample (Blockdauer ~71ms).
        setup_view = self._make_setup_view()
        self.assertEqual(setup_view._calculate_samples_per_read(14.0), 1)

    def test_rate_at_former_fixed_minimum_boundary_is_unchanged(self) -> None:
        # Bei 2000 Hz traf schon die alte, feste Untergrenze (50 Samples)
        # zufaellig genau den Zielwert - hier darf sich durch die
        # Umstellung nichts aendern.
        setup_view = self._make_setup_view()
        self.assertEqual(setup_view._calculate_samples_per_read(2000.0), 50)

    def test_typical_ni9234_rate_matches_25ms_target_block(self) -> None:
        setup_view = self._make_setup_view()
        self.assertEqual(setup_view._calculate_samples_per_read(20_000.0), 500)

    def test_high_rate_is_capped_at_max_samples_per_read(self) -> None:
        setup_view = self._make_setup_view()
        self.assertEqual(setup_view._calculate_samples_per_read(100_000.0), 2000)


if __name__ == "__main__":
    unittest.main()
