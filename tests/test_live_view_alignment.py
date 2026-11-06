"""
tests/test_live_view_alignment.py

The plots of one grid column in the live view must start exactly below
each other (`gui/live_view.py::_rebuild_plots`/`_align_y_axes`):

    - a plot WITHOUT value readout next to plots WITH one used to
      stretch over the empty readout columns and start further left,
    - Y axes with wider tick labels ("-1000" vs. "10") or with/without
      title used to shift the plot areas against each other.

Real widgets (`GraphicsLayoutWidget`), no NI driver needed.
"""

from __future__ import annotations

import unittest

from PyQt6.QtWidgets import QApplication

from data.models import Channel

_APP: QApplication | None = None


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def _view():
    from config.configuration_manager import ConfigurationManager
    from core.controller import MeasurementController
    from gui.live_view import LiveView

    _app()
    view = LiveView(MeasurementController(ConfigurationManager()))
    view.resize(1200, 900)
    view.show()
    return view


def _plot_columns(view) -> list[int]:
    """Layout column each plot starts in."""
    return [view._plot_widget.ci.items[item][0][1] for item in view._plot_items]


class AlignmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.view = _view()

    def tearDown(self) -> None:
        self.view.deleteLater()
        _app().processEvents()

    def test_plot_without_readout_starts_where_the_others_start(self) -> None:
        self.view._channels = [
            Channel("ai0", "A", plot_show_value=True),
            Channel("ai1", "B", plot_show_value=False),
            Channel("ai2", "C", plot_show_value=True),
        ]
        self.view._rebuild_plots()
        self.assertEqual(_plot_columns(self.view), [2, 2, 2])

    def test_without_any_readout_the_plots_use_the_full_width(self) -> None:
        self.view._channels = [Channel("ai0", "A"), Channel("ai1", "B")]
        self.view._rebuild_plots()
        self.assertEqual(_plot_columns(self.view), [0, 0])

    def test_per_grid_column(self) -> None:
        # Two columns: only the LEFT one has a readout.
        self.view._plot_columns = 2
        self.view._channels = [
            Channel("ai0", "A", plot_show_value=True),
            Channel("ai1", "B"),
            Channel("ai2", "C"),
            Channel("ai3", "D"),
        ]
        self.view._rebuild_plots()
        self.assertEqual(_plot_columns(self.view), [2, 3, 2, 3])

    def test_y_axes_get_the_same_width(self) -> None:
        self.view._channels = [
            Channel("ai0", "Kraft", unit="N"),
            Channel("ai1", "Weg", unit="mm", plot_show_y_label=False),
        ]
        self.view._rebuild_plots()
        axes = [item.getAxis("left") for item in self.view._plot_items]
        # Wider tick labels on the first plot (as after "-1000").
        axes[0].textWidth = 60
        self.view._align_y_axes()
        _app().processEvents()
        self.assertEqual(axes[0].width(), axes[1].width())
        self.assertGreaterEqual(axes[1].width(), 60)

        # Narrower again -> both shrink together.
        axes[0].textWidth = 20
        self.view._align_y_axes()
        _app().processEvents()
        self.assertEqual(axes[0].width(), axes[1].width())
        self.assertLess(axes[0].width(), 60)


if __name__ == "__main__":
    unittest.main()
