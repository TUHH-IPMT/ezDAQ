"""
tests/test_live_view_render_cache.py

The live view lets Qt cache axes, title and legend of each plot as a
pixmap (`gui/live_view.py::_cache_static_plot_parts`) instead of
painting them every frame. That is only correct as long as the cache is
thrown away whenever they DO change - otherwise the plot would keep
showing old tick labels after autoscaling or after the time axis moved
on to the next sweep. These tests render the real widget and check that
the axis area changes when the range does.
"""

from __future__ import annotations

import unittest

import pyqtgraph as pg
from PyQt6.QtCore import QRectF
from PyQt6.QtWidgets import QApplication, QGraphicsItem

_APP: QApplication | None = None


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


class RenderCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        from gui.live_view import _cache_static_plot_parts

        _app()
        # Importing gui.live_view switches PyQtGraph to OpenGL, and
        # `QWidget.grab()` cannot capture an OpenGL surface (it comes back
        # empty) - this widget is drawn in software, the caching is the same.
        self._previous_opengl = pg.getConfigOption("useOpenGL")
        pg.setConfigOptions(useOpenGL=False)
        self.addCleanup(lambda: pg.setConfigOptions(useOpenGL=self._previous_opengl))
        self.widget = pg.PlotWidget()
        self.widget.resize(500, 300)
        self.plot_item = self.widget.getPlotItem()
        self.plot_item.addLegend()
        self.plot_item.plot([0, 1, 2], [0, 1, 0], name="A")
        self.plot_item.showGrid(x=True, y=True)
        # Wide enough for every tick label from the start - PyQtGraph only
        # widens an axis one frame AFTER labels did not fit (and skips
        # them until then), which would make the comparison below flaky.
        self.plot_item.getAxis("left").setWidth(60)
        _cache_static_plot_parts(self.plot_item)
        self.widget.show()
        _app().processEvents()

    def tearDown(self) -> None:
        self.widget.close()
        self.widget.deleteLater()
        _app().processEvents()

    def _axis_pixels(self, name: str):
        """The rendered tick-label strip of an axis (its layout geometry,
        not its bounding rect - with the grid on, that spans the whole
        plot area including the curve), in widget coordinates."""
        _app().processEvents()
        axis = self.plot_item.getAxis(name)
        strip = QRectF(0, 0, axis.width(), axis.height())
        rect = self.widget.mapFromScene(axis.mapToScene(strip)).boundingRect()
        return self.widget.grab(rect).toImage()

    def test_strip_comparison_is_stable(self) -> None:
        """Guards the two tests below: rendering the same range twice
        gives the same pixels, so a difference really means a redraw."""
        self.plot_item.setYRange(0, 1, padding=0)
        self.assertEqual(self._axis_pixels("left"), self._axis_pixels("left"))

    def test_axes_title_and_legend_are_cached(self) -> None:
        cache = QGraphicsItem.CacheMode.DeviceCoordinateCache
        for name in ("left", "bottom"):
            self.assertEqual(self.plot_item.getAxis(name).cacheMode(), cache)
        self.assertEqual(self.plot_item.titleLabel.cacheMode(), cache)
        self.assertEqual(self.plot_item.legend.cacheMode(), cache)

    def test_y_axis_is_redrawn_when_the_range_changes(self) -> None:
        self.plot_item.setYRange(0, 1, padding=0)
        before = self._axis_pixels("left")
        self.plot_item.setYRange(-5000, 5000, padding=0)
        after = self._axis_pixels("left")
        self.assertNotEqual(before, after)

    def test_time_axis_is_redrawn_for_the_next_sweep(self) -> None:
        self.plot_item.setXRange(0, 5, padding=0)
        before = self._axis_pixels("bottom")
        self.plot_item.setXRange(5, 10, padding=0)
        after = self._axis_pixels("bottom")
        self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()
