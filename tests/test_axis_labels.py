"""
tests/test_axis_labels.py

Tests for the per-channel, per-axis switchable axis TITLES of the live
view (`Channel.plot_show_x_label`/`plot_show_y_label`, set in
`gui/live_view.py::ChannelPlotSettingsDialog`).

Covers three things that are easy to get wrong:
    - The flags survive the whole persistence round trip, and a
      configuration written before this feature existed still loads with
      both titles on.
    - Switching a title off actually gives its space back to the plot
      area - `AxisItem.showLabel(False)` rather than an empty
      `setLabel("")`, which would leave the space reserved.
    - `AxisItem.setLabel()` ends with `showLabel(bool(text))` internally
      and therefore RESURRECTS a hidden title. Every code path that
      re-sets a label (notably a language change) has to re-apply the
      visibility afterwards - the test pins that behavior down so the
      ordering requirement doesn't get lost.
"""

from __future__ import annotations

import unittest

from PyQt6.QtWidgets import QApplication

from data.models import Channel


# Module-level reference on purpose: without one, the QApplication
# created here is garbage-collected once the creating test returns, and
# Qt then tears down every remaining QObject with it - including the
# lazily built i18n signal singleton (`gui/i18n.py::_get_signals`),
# whose Python object survives while its C++ side is gone. The next test
# file to call `connect_language_changed()` would then fail with
# "wrapped C/C++ object ... has been deleted".
_APP: QApplication | None = None


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


class ChannelAxisLabelPersistenceTest(unittest.TestCase):
    def test_defaults_are_on(self) -> None:
        """Previous appearance stays the default - the feature is opt-out."""
        channel = Channel("cDAQ1Mod1/ai0", "Force")

        self.assertTrue(channel.plot_show_x_label)
        self.assertTrue(channel.plot_show_y_label)

    def test_survives_dict_round_trip(self) -> None:
        channel = Channel(
            "cDAQ1Mod1/ai0",
            "Force",
            plot_show_x_label=False,
            plot_show_y_label=True,
        )

        restored = Channel.from_dict(channel.to_dict())

        self.assertFalse(restored.plot_show_x_label)
        self.assertTrue(restored.plot_show_y_label)

    def test_configuration_without_the_keys_loads_with_labels_on(self) -> None:
        """A config file written before this feature existed."""
        data = Channel("cDAQ1Mod1/ai0", "Force").to_dict()
        del data["plot_show_x_label"]
        del data["plot_show_y_label"]

        restored = Channel.from_dict(data)

        self.assertTrue(restored.plot_show_x_label)
        self.assertTrue(restored.plot_show_y_label)

    def test_survives_channel_table_round_trip(self) -> None:
        """The flags travel through `ChannelTableWidget` as a plain dict
        (the table has no columns for them, see `_display_settings`) - a
        key missing on either side would silently reset them whenever the
        channel list is read back."""
        from gui.widgets.channel_table import ChannelTableWidget

        app = _app()
        table = ChannelTableWidget()
        table.set_channels(
            [
                Channel(
                    "cDAQ1Mod1/ai0",
                    "Force",
                    plot_show_x_label=False,
                    plot_show_y_label=True,
                )
            ]
        )

        restored = table.get_channels()[0]
        self.assertFalse(restored.plot_show_x_label)
        self.assertTrue(restored.plot_show_y_label)

        table.deleteLater()
        app.processEvents()


class AxisLabelVisibilityTest(unittest.TestCase):
    """`gui/live_view.py::_apply_axis_label_visibility` against a real
    PyQtGraph plot - the interesting part is the layout effect, which a
    fake cannot show."""

    def setUp(self) -> None:
        import pyqtgraph as pg
        from gui.live_view import _channel_axis_label

        self.app = _app()
        self.channel = Channel("cDAQ1Mod1/ai0", "Force", unit="N")
        self.widget = pg.PlotWidget()
        self.plot_item = self.widget.getPlotItem()
        self.plot_item.setLabel("bottom", "Time [s]")
        self.plot_item.setLabel("left", _channel_axis_label(self.channel))
        # Titles only take up (and give back) space once the item has
        # actually been laid out.
        self.widget.resize(600, 400)
        self.widget.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.widget.close()
        self.widget.deleteLater()
        self.app.processEvents()

    def _apply(self) -> None:
        from gui.live_view import _apply_axis_label_visibility

        _apply_axis_label_visibility(self.plot_item, self.channel)
        self.app.processEvents()

    def _label_visible(self) -> tuple[bool, bool]:
        return (
            self.plot_item.getAxis("bottom").label.isVisible(),
            self.plot_item.getAxis("left").label.isVisible(),
        )

    def test_axes_can_be_switched_independently(self) -> None:
        self.channel.plot_show_x_label = False
        self._apply()
        self.assertEqual(self._label_visible(), (False, True))

        self.channel.plot_show_x_label = True
        self.channel.plot_show_y_label = False
        self._apply()
        self.assertEqual(self._label_visible(), (True, False))

    def test_hidden_title_gives_its_space_back(self) -> None:
        """The whole point of the switch - `setLabel("")` would keep the
        space reserved."""
        height_before = self.plot_item.getAxis("bottom").height()
        width_before = self.plot_item.getAxis("left").width()

        self.channel.plot_show_x_label = False
        self.channel.plot_show_y_label = False
        self._apply()

        self.assertLess(self.plot_item.getAxis("bottom").height(), height_before)
        self.assertLess(self.plot_item.getAxis("left").width(), width_before)

    def test_switching_back_on_restores_the_title(self) -> None:
        self.channel.plot_show_x_label = False
        self.channel.plot_show_y_label = False
        self._apply()
        self.assertEqual(self._label_visible(), (False, False))

        self.channel.plot_show_x_label = True
        self.channel.plot_show_y_label = True
        self._apply()
        self.assertEqual(self._label_visible(), (True, True))

    def test_set_label_resurrects_a_hidden_title(self) -> None:
        """Pins down WHY every `setLabel()` call site has to re-apply the
        visibility afterwards (see `LiveView.retranslate_ui`)."""
        self.channel.plot_show_x_label = False
        self._apply()
        self.assertFalse(self._label_visible()[0])

        # What a language change does.
        self.plot_item.setLabel("bottom", "Zeit [s]")
        self.app.processEvents()
        self.assertTrue(
            self._label_visible()[0],
            "setLabel() no longer re-shows the label - the re-apply in "
            "retranslate_ui may have become unnecessary",
        )

        self._apply()
        self.assertFalse(self._label_visible()[0])


class PlotSettingsDialogAxisLabelTest(unittest.TestCase):
    """The two checkboxes in `ChannelPlotSettingsDialog` read from and
    write back to the settings dict that `ChannelDisplayDialog` carries."""

    def _settings(self, show_x: bool, show_y: bool) -> dict:
        return {
            "plot_color": "#ff0000",
            "plot_background": "#ffffff",
            "plot_grid_color": "#cccccc",
            "plot_y_min": -10.0,
            "plot_y_max": 10.0,
            "plot_autoscale": True,
            "plot_time_window_seconds": 5.0,
            "plot_show_x_label": show_x,
            "plot_show_y_label": show_y,
        }

    def test_checkboxes_reflect_the_incoming_settings(self) -> None:
        from gui.live_view import ChannelPlotSettingsDialog

        app = _app()
        dialog = ChannelPlotSettingsDialog("Force", self._settings(False, True))

        self.assertFalse(dialog._x_label_check.isChecked())
        self.assertTrue(dialog._y_label_check.isChecked())

        dialog.deleteLater()
        app.processEvents()

    def test_results_carry_the_flags_back(self) -> None:
        from gui.live_view import ChannelPlotSettingsDialog

        app = _app()
        dialog = ChannelPlotSettingsDialog("Force", self._settings(True, True))
        dialog._x_label_check.setChecked(False)

        results = dialog.results()
        self.assertIs(results["plot_show_x_label"], False)
        self.assertIs(results["plot_show_y_label"], True)

        dialog.deleteLater()
        app.processEvents()

    def test_missing_keys_default_to_on(self) -> None:
        """A settings dict from an older stored configuration."""
        from gui.live_view import ChannelPlotSettingsDialog

        app = _app()
        settings = self._settings(False, False)
        del settings["plot_show_x_label"]
        del settings["plot_show_y_label"]
        dialog = ChannelPlotSettingsDialog("Force", settings)

        self.assertTrue(dialog._x_label_check.isChecked())
        self.assertTrue(dialog._y_label_check.isChecked())

        dialog.deleteLater()
        app.processEvents()


if __name__ == "__main__":
    unittest.main()
