"""
tests/test_crosshair.py

Tests for the analysis readout cursor
(`gui/widgets/crosshair.py::SnappingCrosshair`).

The behaviour worth pinning down is the snapping: the cursor must land
on samples that were actually measured, never between them. A free
crosshair would report a value nobody recorded, which in a measurement
application is worse than no readout at all.
"""

from __future__ import annotations

import unittest

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QPointF
from PyQt6.QtWidgets import QApplication

# Module-level reference, see tests/test_axis_labels.py for why.
_APP: QApplication | None = None


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
        from gui.theme import init_theme

        init_theme(_APP)
    return _APP


class _Fixture:
    """A shown plot with one curve, plus a cursor for it."""

    def __init__(self, xs, ys, name: str = "Kanal A") -> None:
        from gui.theme import plot_background_color, plot_foreground_color
        from gui.widgets.crosshair import SnappingCrosshair

        self.app = _app()
        self.xs = np.asarray(xs, dtype=float)
        self.ys = np.asarray(ys, dtype=float)
        self.plot = pg.PlotWidget()
        self.plot.plot(self.xs, self.ys, name=name)
        self.plot.resize(600, 400)
        # Shown on purpose: scene coordinates only mean anything once the
        # widget has a real geometry.
        self.plot.show()
        self.app.processEvents()

        self.crosshair = SnappingCrosshair(
            self.plot,
            plot_foreground_color(),
            plot_foreground_color(),
            plot_background_color(),
        )

    def scene_pos_for(self, x: float, y: float) -> QPointF:
        view_box = self.plot.getPlotItem().getViewBox()
        return view_box.mapViewToScene(QPointF(float(x), float(y)))

    def close(self) -> None:
        self.plot.close()
        self.plot.deleteLater()
        self.app.processEvents()


class SnappingTest(unittest.TestCase):
    def setUp(self) -> None:
        # Samples every 0.5 - wide enough apart that "snapped" and "did
        # not snap" cannot be confused.
        xs = np.arange(0.0, 10.0, 0.5)
        self.fixture = _Fixture(xs, np.sin(xs))
        self.fixture.crosshair.set_enabled(True)

    def tearDown(self) -> None:
        self.fixture.close()

    def _snapped(self) -> tuple[float, float]:
        return (
            self.fixture.crosshair._v_line.value(),
            self.fixture.crosshair._h_line.value(),
        )

    def test_lands_on_a_measured_sample(self) -> None:
        for target in (2.3, 4.9, 7.14):
            with self.subTest(target=target):
                pos = self.fixture.scene_pos_for(target, float(np.sin(target)))

                self.assertTrue(self.fixture.crosshair.move_to(pos))

                snapped_x, _ = self._snapped()
                self.assertIn(
                    snapped_x,
                    set(self.fixture.xs),
                    "cursor sits between samples - it reports a value nobody measured",
                )

    def test_picks_the_nearest_sample(self) -> None:
        for target in (2.3, 4.9, 7.14):
            with self.subTest(target=target):
                pos = self.fixture.scene_pos_for(target, float(np.sin(target)))
                self.fixture.crosshair.move_to(pos)

                nearest = self.fixture.xs[np.argmin(np.abs(self.fixture.xs - target))]
                self.assertAlmostEqual(self._snapped()[0], float(nearest))

    def test_y_comes_from_the_sample_not_the_pointer(self) -> None:
        """The horizontal line marks the measured value, so it must not
        follow the mouse vertically."""
        target_x = 3.0
        # Pointer well above the curve, but still inside the plot.
        pos = self.fixture.scene_pos_for(target_x, float(np.sin(target_x)) + 0.15)
        self.fixture.crosshair.move_to(pos)

        _, snapped_y = self._snapped()
        self.assertAlmostEqual(snapped_y, float(np.sin(3.0)), places=9)


class EnableStateTest(unittest.TestCase):
    def setUp(self) -> None:
        xs = np.arange(0.0, 5.0, 0.5)
        self.fixture = _Fixture(xs, xs * 2)

    def tearDown(self) -> None:
        self.fixture.close()

    def test_disabled_cursor_does_nothing(self) -> None:
        """The scene signal fires on every mouse move, also while the
        tool is off."""
        pos = self.fixture.scene_pos_for(2.0, 4.0)

        self.assertFalse(self.fixture.crosshair.move_to(pos))

    def test_enabling_adds_and_disabling_removes_the_items(self) -> None:
        """Left behind, the infinite lines would keep showing on a plot
        whose cursor is switched off."""
        plot_item = self.fixture.plot.getPlotItem()
        items = self.fixture.crosshair._items

        self.fixture.crosshair.set_enabled(True)
        self.assertTrue(all(item in plot_item.items for item in items))

        self.fixture.crosshair.set_enabled(False)
        self.assertFalse(any(item in plot_item.items for item in items))

    def test_enabling_twice_does_not_add_the_items_twice(self) -> None:
        plot_item = self.fixture.plot.getPlotItem()

        self.fixture.crosshair.set_enabled(True)
        self.fixture.crosshair.set_enabled(True)

        self.assertEqual(plot_item.items.count(self.fixture.crosshair._v_line), 1)

    def test_cursor_does_not_change_the_visible_range(self) -> None:
        """The crosshair lines are infinite - added without
        `ignoreBounds` they drag the auto-range out to nowhere."""
        self.fixture.plot.getPlotItem().getViewBox().autoRange()
        self.fixture.app.processEvents()
        before = self.fixture.plot.getPlotItem().getViewBox().viewRange()

        self.fixture.crosshair.set_enabled(True)
        self.fixture.crosshair.move_to(self.fixture.scene_pos_for(2.0, 4.0))
        self.fixture.app.processEvents()
        after = self.fixture.plot.getPlotItem().getViewBox().viewRange()

        self.assertEqual(before, after)


class OutsideAndEmptyTest(unittest.TestCase):
    def test_pointer_outside_the_plot_hides_the_cursor(self) -> None:
        xs = np.arange(0.0, 5.0, 0.5)
        fixture = _Fixture(xs, xs)
        fixture.crosshair.set_enabled(True)
        fixture.crosshair.move_to(fixture.scene_pos_for(2.0, 2.0))

        self.assertFalse(fixture.crosshair.move_to(QPointF(-500.0, -500.0)))
        self.assertFalse(fixture.crosshair._v_line.isVisible())

        fixture.close()

    def test_plot_without_curves_reports_nothing(self) -> None:
        from gui.theme import plot_background_color, plot_foreground_color
        from gui.widgets.crosshair import SnappingCrosshair

        app = _app()
        plot = pg.PlotWidget()
        plot.resize(400, 300)
        plot.show()
        app.processEvents()
        crosshair = SnappingCrosshair(
            plot,
            plot_foreground_color(),
            plot_foreground_color(),
            plot_background_color(),
        )
        crosshair.set_enabled(True)

        center = plot.sceneBoundingRect().center()
        self.assertFalse(crosshair.move_to(center))

        plot.close()
        plot.deleteLater()
        app.processEvents()


class LabelTest(unittest.TestCase):
    def test_label_names_the_channel_and_both_values(self) -> None:
        xs = np.arange(0.0, 5.0, 0.5)
        fixture = _Fixture(xs, xs * 3.0, name="Kraft")
        fixture.crosshair.set_enabled(True)
        fixture.crosshair.move_to(fixture.scene_pos_for(2.0, 6.0))

        text = fixture.crosshair._label.toPlainText()
        self.assertIn("Kraft", text)
        self.assertIn("x = 2", text)
        self.assertIn("y = 6", text)

        fixture.close()

    def test_unnamed_curve_omits_the_name_line(self) -> None:
        """`name=None` would otherwise print an empty first line."""
        from gui.widgets.crosshair import SnappingCrosshair

        text = SnappingCrosshair._format(1.5, -2.25, "")
        self.assertEqual(text.splitlines()[0], "x = 1.5")


if __name__ == "__main__":
    unittest.main()
