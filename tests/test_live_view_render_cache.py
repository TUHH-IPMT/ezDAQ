"""
tests/test_live_view_render_cache.py

The live view must NOT let Qt cache axes, title or legend as pixmaps
(`QGraphicsItem.setCacheMode`). That was tried for performance and made
the live view update visibly less often on the lab PC with the real
hardware - new data appeared in large jumps (see the comment above
`_style_legend` in `gui/live_view.py`). This test keeps it from coming
back unnoticed, in the main grid and in an own window.
"""

from __future__ import annotations

import unittest

from PyQt6.QtWidgets import QApplication, QGraphicsItem

from data.models import Channel

_APP: QApplication | None = None


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def _cached_parts(plot_item) -> list[str]:
    """Names of the plot parts that have any item cache switched on."""
    parts = {name: plot_item.getAxis(name) for name in ("left", "bottom", "top", "right")}
    parts["title"] = plot_item.titleLabel
    legend = getattr(plot_item, "legend", None)
    if legend is not None:
        parts["legend"] = legend
    return [
        name
        for name, item in parts.items()
        if item is not None and item.cacheMode() != QGraphicsItem.CacheMode.NoCache
    ]


class NoRenderCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        from config.configuration_manager import ConfigurationManager
        from core.controller import MeasurementController
        from gui.live_view import LiveView

        _app()
        self.view = LiveView(MeasurementController(ConfigurationManager()))
        # Plot 1: a group of two channels (has a legend), plot 2: single.
        self.channels = [
            Channel("cDAQ1Mod1/ai0", "Acc X", unit="g"),
            Channel("cDAQ1Mod1/ai1", "Acc Y", unit="g"),
            Channel("cDAQ1Mod1/ai2", "Force", unit="N"),
        ]
        for channel, number in zip(self.channels, (1, 1, 2)):
            channel.plot_number = number
        self.view.preview_channels(self.channels)

    def tearDown(self) -> None:
        for key in list(self.view._popout_windows):
            self.view._close_popout_window(key)
        self.view.deleteLater()
        _app().processEvents()

    def test_main_grid_plots_are_not_cached(self) -> None:
        self.assertEqual(len(self.view._plot_items), 2)
        for plot_item in self.view._plot_items:
            self.assertEqual(_cached_parts(plot_item), [])

    def test_own_window_plot_is_not_cached(self) -> None:
        self.view._open_popout_window(self.view._plot_slots[0])
        windows = list(self.view._popout_windows.values())
        self.assertEqual(len(windows), 1)
        self.assertEqual(_cached_parts(windows[0].plot_item), [])


if __name__ == "__main__":
    unittest.main()
