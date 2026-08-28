"""
gui/widgets/crosshair.py

Snapping readout cursor for the analysis plots (see
`gui/analysis_view.py`).

One instance drives ONE plot widget: a vertical and a horizontal line
plus a small text box that follow the mouse but SNAP to the nearest
actually measured sample, and report its x/y values.

Why snapping rather than a free crosshair: a free one reports where the
pointer happens to be, which is a value that was never measured. Reading
a curve is almost always the question "what does THIS sample say", so
the cursor sits on samples only - the reason it also draws a marker dot
on the point it locked onto.

Nearest is measured in PIXELS, not in data units. The axes carry
different quantities (seconds against volts, say) and different scales,
so a distance mixing them would jump to a far-away sample whenever the
zoom level changed. Pixel distance is what the eye judges too.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor

# Samples on either side of the x-position hit to consider. The search
# starts from a binary search on x, so a handful of neighbours is enough
# to find the true nearest point - and it keeps a curve with millions of
# samples as cheap as one with a hundred.
_NEIGHBOURHOOD = 3

# How close the pointer has to be, in pixels, for a sample to be picked
# up at all. Without a limit the cursor would cling to a curve at the
# other end of an empty plot.
_MAX_PICK_DISTANCE_PX = 120.0


class SnappingCrosshair:
    """Crosshair for one plot widget, snapping to the nearest sample.

    Created up front but idle: the items only enter the plot on
    `set_enabled(True)`, so a disabled cursor costs nothing and the plot
    stays exactly as it was.
    """

    def __init__(
        self,
        plot_widget: pg.PlotWidget,
        color: str,
        text_color: str,
        background: str,
    ) -> None:
        self._plot_widget = plot_widget
        self._enabled = False

        # `movable=False`: the lines are a readout, not a handle - a
        # draggable one would let the cursor be pulled off its sample.
        self._v_line = pg.InfiniteLine(angle=90, movable=False)
        self._h_line = pg.InfiniteLine(angle=0, movable=False)
        self._marker = pg.ScatterPlotItem(size=9, brush=pg.mkBrush(None))
        self._label = pg.TextItem(anchor=(0, 1))
        self._label.setZValue(100)

        self._items = (self._v_line, self._h_line, self._marker, self._label)
        self.restyle(color, text_color, background)

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #

    def restyle(self, color: str, text_color: str, background: str) -> None:
        """Applies the current theme's colors.

        Called again after a theme change: every color here is captured at
        the moment it is set, so a cursor built under the dark theme would
        otherwise keep drawing light lines on a light plot.
        """
        pen = pg.mkPen(color=color, width=1, style=Qt.PenStyle.DashLine)
        self._v_line.setPen(pen)
        self._h_line.setPen(pen)
        self._marker.setPen(pg.mkPen(color=color, width=1.5))

        # Filled box behind the text: the readout sits right where the two
        # crosshair lines cross, and plain text on top of them - and on
        # top of the curve - is hard to read at a glance.
        backdrop = QColor(background)
        backdrop.setAlpha(220)
        self._label.setColor(text_color)
        self._label.fill = pg.mkBrush(backdrop)
        self._label.border = pg.mkPen(color=color, width=1)
        self._label.update()

    def set_enabled(self, enabled: bool) -> None:
        """Adds the cursor items to the plot, or takes them back out."""
        if enabled == self._enabled:
            return
        self._enabled = enabled
        plot_item = self._plot_widget.getPlotItem()
        for item in self._items:
            if enabled:
                # `ignoreBounds=True`: without it the crosshair lines,
                # being infinite, drag the plot's auto-range out to
                # nowhere the moment they are added.
                plot_item.addItem(item, ignoreBounds=True)
            else:
                plot_item.removeItem(item)
        if enabled:
            self._set_visible(False)

    def is_enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------ #
    # Movement
    # ------------------------------------------------------------------ #

    def move_to(self, scene_pos: QPointF) -> bool:
        """Snaps the cursor to the sample nearest `scene_pos`.

        Args:
            scene_pos: Mouse position in SCENE coordinates, as delivered
                by `plot_widget.scene().sigMouseMoved`.

        Returns:
            Whether a sample was found and the cursor is showing. `False`
            also hides it - a pointer outside the plot, an empty plot and
            a pointer too far from any curve are all "nothing to read
            here".
        """
        if not self._enabled:
            return False
        if not self._plot_widget.sceneBoundingRect().contains(scene_pos):
            self._set_visible(False)
            return False

        hit = self._nearest_sample(scene_pos)
        if hit is None:
            self._set_visible(False)
            return False

        x_value, y_value, name = hit
        self._v_line.setPos(x_value)
        self._h_line.setPos(y_value)
        self._marker.setData([x_value], [y_value])
        self._label.setText(self._format(x_value, y_value, name))
        self._label.setPos(x_value, y_value)
        self._place_label(x_value, y_value)
        self._set_visible(True)
        return True

    def _nearest_sample(self, scene_pos: QPointF):
        """The sample closest to `scene_pos` across all curves of this
        plot, as `(x, y, name)` - or `None` if nothing is close enough.

        Curves are taken from the plot item rather than from a list kept
        alongside: that way a curve added or removed elsewhere cannot
        leave this cursor pointing at something that is no longer drawn.
        """
        view_box = self._plot_widget.getPlotItem().getViewBox()
        best = None
        best_distance = _MAX_PICK_DISTANCE_PX

        for item in self._plot_widget.getPlotItem().listDataItems():
            if item is self._marker:
                continue
            xs, ys = item.getData()
            if xs is None or ys is None or len(xs) == 0:
                continue

            for index in self._candidate_indices(xs, view_box, scene_pos):
                point = view_box.mapViewToScene(QPointF(float(xs[index]), float(ys[index])))
                distance = float(np.hypot(point.x() - scene_pos.x(), point.y() - scene_pos.y()))
                if distance < best_distance:
                    best_distance = distance
                    best = (float(xs[index]), float(ys[index]), item.name() or "")

        return best

    @staticmethod
    def _candidate_indices(xs, view_box, scene_pos) -> range:
        """Indices worth measuring, found by binary search on x.

        Measuring every sample would be linear in the dataset on every
        mouse move - unusable on the multi-million-sample files this view
        is built for.
        """
        target_x = view_box.mapSceneToView(scene_pos).x()
        # `searchsorted` needs x ascending, which a time axis is. For
        # anything else it merely picks a worse starting point, so the
        # result stays correct - just less precise for that curve.
        center = int(np.searchsorted(xs, target_x))
        return range(
            max(0, center - _NEIGHBOURHOOD),
            min(len(xs), center + _NEIGHBOURHOOD + 1),
        )

    # ------------------------------------------------------------------ #
    # Presentation
    # ------------------------------------------------------------------ #

    @staticmethod
    def _format(x_value: float, y_value: float, name: str) -> str:
        """`%g` with six significant digits, not a fixed decimal count:
        the same view shows microvolts and kilonewtons, and a fixed
        format would print either noise or nothing."""
        lines = []
        if name:
            lines.append(name)
        lines.append(f"x = {x_value:.6g}")
        lines.append(f"y = {y_value:.6g}")
        return "\n".join(lines)

    def _place_label(self, x_value: float, y_value: float) -> None:
        """Flips the label to whichever side of the point has room.

        Anchored bottom-left by default, so near the right or top edge it
        would be drawn outside the plot and clipped away.
        """
        view_range = self._plot_widget.getPlotItem().getViewBox().viewRange()
        (x_min, x_max), (y_min, y_max) = view_range
        x_span = x_max - x_min or 1.0
        y_span = y_max - y_min or 1.0

        right_of_point = (x_value - x_min) / x_span < 0.75
        above_point = (y_value - y_min) / y_span < 0.8
        self._label.setAnchor(
            QPointF(0.0 if right_of_point else 1.0, 1.0 if above_point else 0.0)
        )

    def _set_visible(self, visible: bool) -> None:
        for item in self._items:
            item.setVisible(visible)
