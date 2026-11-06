"""
tests/test_plot_groups.py

Tests for several channels in ONE live view plot (`Channel.plot_number`:
same number = shared plot, see `gui/live_view.py::_plot_slots`):

    - the model/persistence round trip (old configurations without the
      field keep one plot per channel),
    - which channels end up in which plot, and that the plot-wide
      settings stay identical within a group,
    - the real live view: one plot with several curves and a legend,
      stacked value readouts, shared Y autoscaling over all curves,
    - an own window for a whole group (incl. closing it by hand and the
      group changing while it is open),
    - the channel display dialog keeping a group consistent.

Runs against the real widgets, with a fake controller feeding a ring
buffer - no NI driver needed.
"""

from __future__ import annotations

import unittest

import numpy as np
from PyQt6.QtWidgets import QApplication

from data.models import Channel

_APP: QApplication | None = None


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def _channels() -> list[Channel]:
    return [
        Channel("cDAQ1Mod1/ai0", "Acc X", unit="g"),
        Channel("cDAQ1Mod1/ai1", "Acc Y", unit="g"),
        Channel("cDAQ1Mod1/ai2", "Force", unit="N"),
        Channel("cDAQ1Mod1/ai3", "Acc Z", unit="g"),
    ]


def _grouped() -> list[Channel]:
    """X, Y, Z in plot 1, Force in plot 2."""
    channels = _channels()
    for index, number in enumerate((1, 1, 2, 1)):
        channels[index].plot_number = number
    return channels


class ModelTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        channel = Channel("ai0", "X", plot_number=3)
        self.assertEqual(Channel.from_dict(channel.to_dict()).plot_number, 3)

    def test_old_configuration_has_no_number(self) -> None:
        data = Channel("ai0", "X").to_dict()
        del data["plot_number"]
        self.assertIsNone(Channel.from_dict(data).plot_number)

    def test_invalid_numbers_are_ignored(self) -> None:
        data = Channel("ai0", "X").to_dict()
        for bad in ("A", 0, -2, None):
            with self.subTest(stored=bad):
                data["plot_number"] = bad
                self.assertIsNone(Channel.from_dict(data).plot_number)

    def test_channel_table_keeps_the_group(self) -> None:
        from gui.widgets.channel_table import ChannelTableWidget

        _app()
        table = ChannelTableWidget()
        table.set_channels(_grouped())
        self.assertEqual([c.plot_number for c in table.get_channels()], [1, 1, 2, 1])
        table.deleteLater()


class SlotTest(unittest.TestCase):
    def test_same_number_shares_a_plot(self) -> None:
        from gui.live_view import _plot_slots

        slots = _plot_slots(_grouped())
        self.assertEqual([s.indices for s in slots], [(0, 1, 3), (2,)])

    def test_plots_are_ordered_by_number(self) -> None:
        from gui.live_view import _plot_slots

        channels = _channels()
        for index, number in enumerate((5, 5, 1, 9)):
            channels[index].plot_number = number
        self.assertEqual([s.indices for s in _plot_slots(channels)], [(2,), (0, 1), (3,)])

    def test_channels_without_a_number_count_on_from_the_highest(self) -> None:
        from gui.live_view import effective_plot_numbers

        channels = _channels()
        channels[1].plot_number = 3
        channels[3].plot_number = 1
        # A new channel (None) must not silently join plot 1 or 3.
        self.assertEqual(effective_plot_numbers(channels), [4, 3, 5, 1])

    def test_no_numbers_is_one_plot_per_channel(self) -> None:
        from gui.live_view import _plot_slots

        self.assertEqual([s.indices for s in _plot_slots(_channels())], [(0,), (1,), (2,), (3,)])

    def test_hidden_channels_are_left_out(self) -> None:
        from gui.live_view import _plot_slots

        channels = _grouped()
        channels[0].plot_visible = False
        self.assertEqual([s.indices for s in _plot_slots(channels)], [(1, 3), (2,)])

    def test_sync_copies_plot_wide_settings_from_the_first_member(self) -> None:
        from gui.live_view import sync_plot_groups

        channels = _grouped()
        channels[0].plot_time_window_seconds = 2.0
        channels[0].plot_y_max = 3.0
        channels[0].plot_popout = True
        channels[1].plot_color = "#ff0000"
        channels[3].plot_visible = False
        sync_plot_groups(channels)

        self.assertEqual(channels[1].plot_time_window_seconds, 2.0)
        self.assertEqual(channels[1].plot_y_max, 3.0)
        self.assertTrue(channels[1].plot_popout)
        # Per-channel settings are untouched ...
        self.assertEqual(channels[1].plot_color, "#ff0000")
        # ... and so are hidden members and other plots.
        self.assertEqual(channels[3].plot_time_window_seconds, 5.0)
        self.assertEqual(channels[2].plot_time_window_seconds, 5.0)

    def test_axis_label(self) -> None:
        from gui.live_view import _plot_axis_label

        channels = _channels()
        self.assertEqual(_plot_axis_label([channels[0]]), "Acc X [g]")
        self.assertEqual(_plot_axis_label([channels[0], channels[1]]), "[g]")
        self.assertEqual(_plot_axis_label([channels[0], channels[2]]), "Acc X [g], Force [N]")

    def test_union_range(self) -> None:
        from gui.live_view import _union_value_range

        self.assertIsNone(_union_value_range([None, None]))
        self.assertEqual(_union_value_range([(1.0, 2.0)]), (1.0, 2.0))
        self.assertEqual(_union_value_range([(1.0, 2.0), None, (-3.0, 0.5)]), (-3.0, 2.0))
        # NaN of one curve does not hide the other curve's range.
        self.assertEqual(_union_value_range([(float("nan"),) * 2, (0.0, 4.0)]), (0.0, 4.0))


class _FakeController:
    """Just enough of `MeasurementController` for `LiveView` ticks."""

    def __init__(self, num_channels: int) -> None:
        from core.ringbuffer import RingBuffer

        self.ring_buffer = RingBuffer(num_channels, 100_000)
        self.current_session = None
        self.total_samples_acquired = 0

    def register_reader(self, back_samples: int = 0) -> int:
        return self.ring_buffer.register_reader(back_samples)

    def unregister_reader(self, reader_id: int) -> None:
        self.ring_buffer.unregister_reader(reader_id)

    def read_live_data(self, reader_id: int, max_samples=None):
        return self.ring_buffer.read_new(reader_id, max_samples=max_samples)

    def get_ring_buffer(self):
        return self.ring_buffer


def _live_view(num_channels: int):
    from config.configuration_manager import ConfigurationManager
    from core.controller import MeasurementController
    from gui.live_view import LiveView

    _app()
    view = LiveView(MeasurementController(ConfigurationManager()))
    fake = _FakeController(num_channels)
    view._controller = fake
    return view, fake


class LiveViewGroupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.channels = _grouped()
        self.view, self.fake = _live_view(len(self.channels))

    def tearDown(self) -> None:
        for key in list(self.view._popout_windows):
            self.view._close_popout_window(key)
        self.view.stop_display() if self.view._reader_id is not None else None
        self.view.deleteLater()
        _app().processEvents()

    def _start(self) -> None:
        self.view.start_display(self.channels, 1000.0)

    def _feed(self, block: np.ndarray) -> None:
        self.fake.ring_buffer.write(block)
        self.view._on_timer_tick()

    def test_one_plot_with_three_curves_and_a_legend(self) -> None:
        self._start()
        self.assertEqual(len(self.view._plot_items), 2)
        self.assertEqual(self.view._curve_channel_indices, [0, 1, 3, 2])
        self.assertEqual(self.view._curve_plot_positions, [0, 0, 0, 1])
        group_plot, force_plot = self.view._plot_items
        legend_names = [label.text for _sample, label in group_plot.legend.items]
        self.assertEqual(legend_names, ["Acc X", "Acc Y", "Acc Z"])
        self.assertIsNone(getattr(force_plot, "legend", None))
        self.assertEqual(group_plot.getAxis("left").labelText, "[g]")

    def test_every_curve_gets_its_own_data(self) -> None:
        self._start()
        block = np.vstack([np.full(200, v) for v in (1.0, 2.0, 30.0, 4.0)])
        self._feed(block)
        for curve, index in zip(self.view._curves, self.view._curve_channel_indices):
            _x, y = curve.getData()
            np.testing.assert_array_equal(y, np.full(200, block[index, 0]))

    def test_y_autoscale_covers_all_curves_of_the_group(self) -> None:
        self._start()
        # All within the default fixed range (+-10) ...
        self._feed(np.vstack([np.full(100, v) for v in (1.0, 2.0, 3.0, 4.0)]))
        self.assertFalse(self.view._channel_y_auto_active[("cDAQ1Mod1/ai0", "Acc X")])
        # ... until ONE member (Acc Z, not the lead) leaves it.
        self._feed(np.vstack([np.full(100, v) for v in (1.0, 2.0, 3.0, 50.0)]))
        self.assertTrue(self.view._channel_y_auto_active[("cDAQ1Mod1/ai0", "Acc X")])

    def test_value_readouts_are_stacked_and_colored_per_curve(self) -> None:
        from PyQt6.QtGui import QColor

        from gui.live_view import channel_curve_color, plot_foreground_color

        for channel in self.channels:
            channel.plot_show_value = True
        self._start()
        labels = self.view._value_labels
        self.assertTrue(all(label is not None for label in labels))
        group_boxes = self.view._value_boxes[:3]
        # Three boxes of the group live in ONE nested layout (one line each).
        parents = {box.parentItem() for box in group_boxes}
        self.assertEqual(len(parents), 1)
        # Group readouts in their curve color, the single channel (Force,
        # last curve) in the theme foreground as before.
        self.assertEqual(labels[1].color.name(), QColor(channel_curve_color(1)).name())
        self.assertEqual(labels[3].color.name(), QColor(plot_foreground_color()).name())

    def test_group_in_own_window(self) -> None:
        self.channels[0].plot_popout = True
        self._start()
        # Only Force is left in the main grid ...
        self.assertEqual(self.view._curve_channel_indices, [2])
        # ... the group is in ONE window with three curves.
        self.assertEqual(len(self.view._popout_windows), 1)
        window = next(iter(self.view._popout_windows.values()))
        self.assertEqual(len(window.curves), 3)
        self.assertEqual(window.windowTitle(), "Acc X, Acc Y, Acc Z")

        block = np.vstack([np.full(50, v) for v in (1.0, 2.0, 3.0, 4.0)])
        self._feed(block)
        np.testing.assert_array_equal(window.curves[2].getData()[1], np.full(50, 4.0))

    def test_closing_the_group_window_brings_the_group_back(self) -> None:
        self.channels[0].plot_popout = True
        self._start()
        window = next(iter(self.view._popout_windows.values()))
        window.close()
        _app().processEvents()

        self.assertEqual(self.view._popout_windows, {})
        self.assertFalse(any(c.plot_popout for c in self.channels))
        self.assertEqual(len(self.view._plot_items), 2)

    def test_window_is_rebuilt_when_the_group_changes(self) -> None:
        self.channels[0].plot_popout = True
        self._start()
        old = next(iter(self.view._popout_windows.values()))

        # Acc Y leaves the group while the window is open.
        settings = {("cDAQ1Mod1/ai1", "Acc Y"): {**self.channels[1].to_dict(), "plot_number": 7}}
        self.view._apply_display_settings_to_live_channels(settings)
        _app().processEvents()

        new = next(iter(self.view._popout_windows.values()))
        self.assertIsNot(new, old)
        self.assertEqual(len(new.curves), 2)
        # The view closed the old window itself - that must NOT count as
        # the user closing it (the group stays in its own window).
        self.assertTrue(self.channels[0].plot_popout)
        self.assertIn(("cDAQ1Mod1/ai0", "Acc X"), self.view._popout_windows)

    def test_joining_a_group_syncs_the_time_window(self) -> None:
        self.channels[0].plot_time_window_seconds = 2.0
        self._start()
        self.assertEqual(self.channels[1].plot_time_window_seconds, 2.0)
        self.assertEqual(self.channels[3].plot_time_window_seconds, 2.0)
        self.assertEqual(self.channels[2].plot_time_window_seconds, 5.0)


class DialogTest(unittest.TestCase):
    def setUp(self) -> None:
        from gui.live_view import ChannelDisplayDialog

        _app()
        channels = _channels()
        channels[0].plot_time_window_seconds = 2.0
        self.channels = channels
        self.dialog = ChannelDisplayDialog(channels, "#000000", "#cccccc")
        self.keys = [(c.hardware_channel, c.display_name) for c in channels]

    def tearDown(self) -> None:
        self.dialog.deleteLater()
        _app().processEvents()

    def _set_group(self, index: int, number: int) -> None:
        self.dialog._rows[self.keys[index]]["group"].setValue(number)

    def test_prefilled_with_one_plot_per_channel(self) -> None:
        values = [self.dialog._rows[k]["group"].value() for k in self.keys]
        self.assertEqual(values, [1, 2, 3, 4])
        self.assertEqual([r["plot_number"] for r in self.dialog.results().values()], [1, 2, 3, 4])

    def test_joining_member_takes_over_the_group_settings(self) -> None:
        self._set_group(1, 1)
        results = self.dialog.results()
        self.assertEqual(results[self.keys[1]]["plot_number"], 1)
        self.assertEqual(results[self.keys[1]]["plot_time_window_seconds"], 2.0)

    def test_own_window_click_applies_to_the_whole_group(self) -> None:
        self._set_group(1, 1)
        self.dialog._rows[self.keys[1]]["popout"].click()
        results = self.dialog.results()
        self.assertTrue(results[self.keys[0]]["plot_popout"])
        self.assertTrue(results[self.keys[1]]["plot_popout"])
        self.assertFalse(results[self.keys[2]]["plot_popout"])

    def test_results_keep_curve_settings_per_channel(self) -> None:
        self._set_group(1, 1)
        self.dialog._plot_settings[self.keys[1]]["plot_color"] = "#00ff00"
        self.dialog._plot_settings[self.keys[1]]["plot_time_window_seconds"] = 9.0
        results = self.dialog.results()
        # Color is per channel, the time window belongs to the plot (lead wins).
        self.assertEqual(results[self.keys[1]]["plot_color"], "#00ff00")
        self.assertEqual(results[self.keys[1]]["plot_time_window_seconds"], 2.0)


if __name__ == "__main__":
    unittest.main()
