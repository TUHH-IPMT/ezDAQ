"""
tests/test_display_menu.py

Tests for the live view display menu
(`gui/live_view.py::ChannelDisplayDialog` and its two sub-dialogs):

    - the grid layout (`LiveView.set_plot_columns`), which places several
      channels side by side instead of strictly one per row,
    - "apply to all channels" in the sub-dialogs,
    - grid lines on/off and curve line width per channel,
    - and the scroll area, without which the dialog grew past the screen
      with a fully populated chassis.

The layout assertions run against a real `GraphicsLayoutWidget` rather
than a fake: the point of the feature is where items end up in Qt's
layout, which only the real thing can answer.
"""

from __future__ import annotations

import unittest

from PyQt6.QtWidgets import QApplication

from data.models import Channel

# Module-level reference, see tests/test_axis_labels.py for why.
_APP: QApplication | None = None


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def _channels(count: int, show_value: bool = False) -> list[Channel]:
    return [
        Channel(f"cDAQ1Mod{i // 4 + 1}/ai{i % 4}", f"Channel {i + 1}", plot_show_value=show_value)
        for i in range(count)
    ]


def _live_view():
    from config.configuration_manager import ConfigurationManager
    from core.controller import MeasurementController
    from gui.live_view import LiveView

    _app()
    return LiveView(MeasurementController(ConfigurationManager()))


class PlotGridLayoutTest(unittest.TestCase):
    """Channels flow into `plot_columns` cells per row."""

    def setUp(self) -> None:
        self.view = _live_view()

    def tearDown(self) -> None:
        self.view.deleteLater()
        _app().processEvents()

    def _rows_used(self) -> int:
        rows = set()
        for item in self.view._plot_items:
            for row, _col in self.view._plot_widget.ci.items[item]:
                rows.add(row)
        return len(rows)

    def test_one_column_keeps_one_channel_per_row(self) -> None:
        """The previous behaviour, and the default."""
        self.view._channels = _channels(6)
        self.view._plot_columns = 1
        self.view._rebuild_plots()

        self.assertEqual(len(self.view._plot_items), 6)
        self.assertEqual(self._rows_used(), 6)

    def test_more_columns_mean_fewer_rows(self) -> None:
        self.view._channels = _channels(6)

        for columns, expected_rows in ((2, 3), (3, 2)):
            with self.subTest(columns=columns):
                self.view._plot_columns = columns
                self.view._rebuild_plots()
                self.assertEqual(self._rows_used(), expected_rows)

    def test_last_row_may_be_partially_filled(self) -> None:
        """5 channels in 2 columns is 3 rows, the last holding one."""
        self.view._channels = _channels(5)
        self.view._plot_columns = 2
        self.view._rebuild_plots()

        self.assertEqual(len(self.view._plot_items), 5)
        self.assertEqual(self._rows_used(), 3)

    def test_each_cell_spans_three_layout_columns(self) -> None:
        """Number, unit and plot - so the value readout of the second
        channel cannot land on top of the first channel's plot."""
        self.view._channels = _channels(4, show_value=True)
        self.view._plot_columns = 2
        self.view._rebuild_plots()

        columns = set()
        for item in self.view._plot_items:
            for _row, col in self.view._plot_widget.ci.items[item]:
                columns.add(col)
        # Plots sit at col_base + 2, with col_base = 0 and 3.
        self.assertEqual(columns, {2, 5})


class PlotColumnsSettingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.view = _live_view()

    def tearDown(self) -> None:
        self.view.deleteLater()
        _app().processEvents()

    def test_default_is_one_column(self) -> None:
        self.assertEqual(self.view.plot_columns(), 1)

    def test_value_is_clamped_on_both_sides(self) -> None:
        """0 would divide by zero in the layout, and beyond the cap the
        plot is narrower than the value readout beside it."""
        from gui.live_view import _MAX_PLOT_COLUMNS

        for requested, expected in ((0, 1), (-5, 1), (999, _MAX_PLOT_COLUMNS)):
            with self.subTest(requested=requested):
                self.view.set_plot_columns(requested)
                self.assertEqual(self.view.plot_columns(), expected)

    def test_setting_the_same_value_does_not_rebuild(self) -> None:
        """`_rebuild_plots()` discards and recreates every subplot, which
        is a visible flicker during a running measurement."""
        self.view._channels = _channels(2)
        self.view.set_plot_columns(2)
        before = list(self.view._plot_items)

        self.view.set_plot_columns(2)

        self.assertEqual(self.view._plot_items, before)


class ApplyToAllTest(unittest.TestCase):
    """`ChannelDisplayDialog._apply_sub_dialog_result` fans one result
    out to every channel."""

    class _FakeSubDialog:
        def __init__(self, result: dict, to_all: bool) -> None:
            self._result = result
            self._to_all = to_all

        def results(self) -> dict:
            return dict(self._result)

        def apply_to_all(self) -> bool:
            return self._to_all

    def _store(self) -> dict:
        return {("a", "A"): {"v": 1}, ("b", "B"): {"v": 2}, ("c", "C"): {"v": 3}}

    def test_plain_ok_touches_only_the_edited_channel(self) -> None:
        from gui.live_view import ChannelDisplayDialog

        store = self._store()
        ChannelDisplayDialog._apply_sub_dialog_result(
            store, ("a", "A"), self._FakeSubDialog({"v": 99}, to_all=False)
        )

        self.assertEqual([store[k]["v"] for k in store], [99, 2, 3])

    def test_apply_to_all_reaches_every_channel(self) -> None:
        from gui.live_view import ChannelDisplayDialog

        store = self._store()
        ChannelDisplayDialog._apply_sub_dialog_result(
            store, ("a", "A"), self._FakeSubDialog({"v": 7}, to_all=True)
        )

        self.assertEqual([store[k]["v"] for k in store], [7, 7, 7])

    def test_channels_get_independent_copies(self) -> None:
        """Sharing one dict would make a later edit of a single channel
        silently change all the others."""
        from gui.live_view import ChannelDisplayDialog

        store = self._store()
        ChannelDisplayDialog._apply_sub_dialog_result(
            store, ("a", "A"), self._FakeSubDialog({"v": 7}, to_all=True)
        )
        store[("a", "A")]["v"] = 42

        self.assertEqual([store[k]["v"] for k in store], [42, 7, 7])


class GridAndLineWidthTest(unittest.TestCase):
    def test_defaults_match_the_previous_hardwired_values(self) -> None:
        channel = Channel("cDAQ1Mod1/ai0", "Force")

        self.assertTrue(channel.plot_show_grid)
        self.assertEqual(channel.plot_line_width, 1.5)

    def test_survive_dict_round_trip(self) -> None:
        channel = Channel(
            "cDAQ1Mod1/ai0", "Force", plot_show_grid=False, plot_line_width=3.5
        )

        restored = Channel.from_dict(channel.to_dict())

        self.assertFalse(restored.plot_show_grid)
        self.assertEqual(restored.plot_line_width, 3.5)

    def test_configuration_without_the_keys_keeps_the_old_look(self) -> None:
        data = Channel("cDAQ1Mod1/ai0", "Force").to_dict()
        del data["plot_show_grid"]
        del data["plot_line_width"]

        restored = Channel.from_dict(data)

        self.assertTrue(restored.plot_show_grid)
        self.assertEqual(restored.plot_line_width, 1.5)

    def test_zero_line_width_is_clamped(self) -> None:
        """A width of 0 renders nothing, with no hint in the dialog why."""
        data = Channel("cDAQ1Mod1/ai0", "Force").to_dict()
        data["plot_line_width"] = 0.0

        self.assertGreater(Channel.from_dict(data).plot_line_width, 0.0)

    def test_survive_channel_table_round_trip(self) -> None:
        from gui.widgets.channel_table import ChannelTableWidget

        app = _app()
        table = ChannelTableWidget()
        table.set_channels(
            [
                Channel(
                    "cDAQ1Mod1/ai0",
                    "Force",
                    plot_show_grid=False,
                    plot_line_width=2.5,
                )
            ]
        )

        restored = table.get_channels()[0]
        self.assertFalse(restored.plot_show_grid)
        self.assertEqual(restored.plot_line_width, 2.5)

        table.deleteLater()
        app.processEvents()

    def test_sub_dialog_carries_both_back(self) -> None:
        from gui.live_view import ChannelPlotSettingsDialog

        app = _app()
        dialog = ChannelPlotSettingsDialog(
            "Force",
            {
                "plot_color": "#ff0000",
                "plot_background": "#ffffff",
                "plot_grid_color": "#cccccc",
                "plot_y_min": -10.0,
                "plot_y_max": 10.0,
                "plot_autoscale": True,
                "plot_time_window_seconds": 5.0,
                "plot_show_x_label": True,
                "plot_show_y_label": True,
                "plot_show_grid": False,
                "plot_line_width": 4.0,
            },
        )

        self.assertFalse(dialog._grid_check.isChecked())
        self.assertEqual(dialog._line_width_spin.value(), 4.0)

        result = dialog.results()
        self.assertIs(result["plot_show_grid"], False)
        self.assertEqual(result["plot_line_width"], 4.0)

        dialog.deleteLater()
        app.processEvents()


class DialogHeightTest(unittest.TestCase):
    """The dialog used to be `SetFixedSize` and grew by ~41px per
    channel: 32 channels came to ~1350px, so on a 1080p screen OK and
    Cancel ended up off screen with no way to scroll or resize to them.
    """

    def test_height_does_not_grow_without_bound(self) -> None:
        from gui.live_view import ChannelDisplayDialog

        app = _app()
        heights = []
        for count in (8, 32, 64):
            dialog = ChannelDisplayDialog(_channels(count), "#fff", "#ccc")
            heights.append(dialog.sizeHint().height())
            dialog.deleteLater()
        app.processEvents()

        self.assertLess(
            heights[-1],
            heights[0] * 2,
            f"height still scales with the channel count: {heights}",
        )

    def test_fits_on_a_1080p_screen_with_a_full_chassis(self) -> None:
        from gui.live_view import ChannelDisplayDialog

        app = _app()
        # 8 x NI9215 in a cDAQ-9189.
        dialog = ChannelDisplayDialog(_channels(32), "#fff", "#ccc")
        height = dialog.sizeHint().height()
        dialog.deleteLater()
        app.processEvents()

        self.assertLess(height, 1000, "would not fit a 1080p screen")


if __name__ == "__main__":
    unittest.main()


class ThemeColorFreezingTest(unittest.TestCase):
    """Confirming the plot dialog must not turn "follow the theme" into a
    hardcoded color.

    The dialog pre-fills missing colors with the current theme default so
    the swatches show something. It used to write those back on OK, so
    merely opening the dialog and pressing OK - without touching a color
    - froze the active theme's palette onto the channel. Switching theme
    afterwards left that channel behind: a configuration made in the
    light theme showed a black grid on the dark theme's dark background.
    """

    def setUp(self) -> None:
        from gui.theme import get_theme, set_theme

        _app()
        self._original_theme = get_theme()
        set_theme("light")

    def tearDown(self) -> None:
        from gui.theme import set_theme

        set_theme(self._original_theme)

    def _dialogs(self, channel: Channel):
        from gui.live_view import (
            ChannelDisplayDialog,
            ChannelPlotSettingsDialog,
            _channel_display_key,
        )
        from gui.theme import plot_background_color, plot_foreground_color

        parent = ChannelDisplayDialog(
            [channel], plot_background_color(), plot_foreground_color()
        )
        key = _channel_display_key(channel)
        sub = ChannelPlotSettingsDialog(
            channel.display_name,
            parent._plot_settings[key],
            channel_count=1,
            # The per-channel set, not the whole map - the curve default
            # depends on the channel's palette slot.
            color_defaults=parent._color_defaults[key],
        )
        return parent, sub

    def test_plain_ok_keeps_the_channel_following_the_theme(self) -> None:
        channel = Channel("cDAQ1Mod1/ai0", "Force")
        parent, sub = self._dialogs(channel)

        result = sub.results()

        for key in ("plot_color", "plot_background", "plot_grid_color"):
            with self.subTest(key=key):
                self.assertIsNone(result[key], "theme color was frozen onto the channel")

        sub.deleteLater()
        parent.deleteLater()
        _app().processEvents()

    def test_swatch_still_shows_the_theme_default(self) -> None:
        """The user must see a color even while none is stored."""
        from gui.theme import plot_foreground_color

        channel = Channel("cDAQ1Mod1/ai0", "Force")
        parent, sub = self._dialogs(channel)

        self.assertEqual(sub._effective("plot_grid_color"), plot_foreground_color())

        sub.deleteLater()
        parent.deleteLater()
        _app().processEvents()

    def test_a_picked_color_is_kept(self) -> None:
        channel = Channel("cDAQ1Mod1/ai0", "Force", plot_grid_color="#ff00ff")
        parent, sub = self._dialogs(channel)

        self.assertEqual(sub.results()["plot_grid_color"], "#ff00ff")

        sub.deleteLater()
        parent.deleteLater()
        _app().processEvents()

    def test_grid_color_follows_a_theme_change(self) -> None:
        from gui.live_view import _channel_grid_color
        from gui.theme import plot_foreground_color, set_theme

        channel = Channel("cDAQ1Mod1/ai0", "Force")

        seen = []
        for theme in ("light", "dark"):
            set_theme(theme)
            seen.append((_channel_grid_color(channel), plot_foreground_color()))

        self.assertEqual(seen[0][0], seen[0][1])
        self.assertEqual(seen[1][0], seen[1][1])
        self.assertNotEqual(seen[0][0], seen[1][0], "theme change had no effect")

    def test_reset_repairs_an_already_frozen_channel(self) -> None:
        """Existing configurations carry frozen colors, and without this
        there is no way back to theme-following from the dialog."""
        channel = Channel(
            "cDAQ1Mod1/ai0",
            "Force",
            plot_color="#1565c0",
            plot_background="#ffffff",
            plot_grid_color="#000000",
        )
        parent, sub = self._dialogs(channel)

        sub._reset_colors_to_theme()
        result = sub.results()

        for key in ("plot_color", "plot_background", "plot_grid_color"):
            with self.subTest(key=key):
                self.assertIsNone(result[key])

        sub.deleteLater()
        parent.deleteLater()
        _app().processEvents()
