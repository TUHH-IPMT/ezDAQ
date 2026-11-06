"""
gui/live_view.py

Live view: real-time display of measurement data during a running measurement.

Features (see spec):
    * Real-time plots of multiple channels simultaneously (PyQtGraph)
    * Channel legend (one subplot per channel, since different channels
      can have different physical units - e.g. plotting force in N and
      acceleration in g together in one chart would be misleading)
    * Zoom/pan (native via PyQtGraph)
    * Start/stop, measurement duration, sampling rate

Display mode (sweep, like an oscilloscope):
    The X axis of each subplot is window-wide (default 5 s) and does NOT
    scroll continuously. The curve draws left to right within this fixed
    window; once the window is full, a new sweep starts immediately and
    the old curve disappears completely (see
    `_write_to_display_buffer`/`_get_channel_display_view`). This is
    deliberately different from a scrolling ring buffer, where old data
    slowly scrolls out at the left edge.

    The axis label shows the actual measurement time (e.g. "40-45s" on
    the 9th sweep of a 5s window), not always "0-5s" - the X range jumps
    to the next absolute time span on every new sweep (see
    `_cycle_start_seconds`), even though the curve itself keeps starting
    over at x=window start.

Architecture note (deliberately NO reveal pacing):
    The DAQ thread delivers new data in blocks of ~25ms (hardware read
    granularity, see `gui/setup_view.py::_calculate_samples_per_read` -
    deliberately not smaller, otherwise there's a buffer overrun risk on
    real hardware). As a result, a slightly "blocky" curve growth is
    visible to the naked eye. An attempt to smooth this out via an
    artificial, time-based reveal delay on the display was deliberately
    reverted: in a direct stimulus-response test (tapping an
    accelerometer while the app is running), it introduced noticeable
    additional latency. For a live measurement instrument, latency
    matters more than display smoothness - `_get_channel_display_view()`
    therefore ALWAYS immediately shows the full currently-arrived state.

Architecture note (performance):
    The display buffer for the current sweep window is a NumPy array
    allocated once up front (`_ensure_display_buffer`) - no allocations
    per data block. That's sufficiently performant for "normal" lab
    sample rates (up to a few kHz across several channels). At very high
    sample rates (e.g. 100 kHz across many channels) over long display
    windows, downsampling the display data (e.g. min/max decimation per
    pixel) would further reduce the drawing load - that's planned as a
    later optimization and deliberately not yet implemented here
    (version 1).
"""

from __future__ import annotations

import logging
import math
import time
import weakref
from dataclasses import dataclass

import numpy as np
import pyqtgraph as pg
from PyQt6 import sip
from PyQt6.QtCore import QPoint, QRegularExpression, QSize, QTimer, pyqtSignal, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QIcon, QRegularExpressionValidator
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.controller import MeasurementController
from core.measurement import apply_scaling
from data.exporter import StorageWriter
from data.models import Channel, RateGroup, TriggerConfig, TriggerDirection, TriggerKind
from gui.i18n import connect_language_changed, get_language, t
from gui.theme import (
    PLAY_ICON_COLOR,
    RECORD_ICON_COLOR,
    action_button_style,
    axis_tick_point_size,
    connect_theme_changed,
    channel_curve_color,
    curve_color,
    draw_ellipsis_icon,
    draw_play_icon,
    draw_record_icon,
    draw_stop_icon,
    draw_trigger_icon,
    fix_toggle_button_width,
    is_position_on_screen,
    is_theme_default_plot_background,
    plot_background_color,
    plot_container_background_color,
    plot_foreground_color,
    repolish,
    style_plot_container,
    style_plot_item,
    trigger_arm_button_style,
)
from gui.widgets.spinbox import (
    NoWheelDoubleSpinBox,
    NoWheelSpinBox,
    PrecisionDoubleSpinBox,
)

logger = logging.getLogger(__name__)

# A diagnostic log showed: the actual per-tick data processing takes
# under 1.5ms, but the gap between ticks was consistently ~100-130ms
# instead of the configured ~33ms. Isolated tests (QTimer alone,
# QTimer + DAQ thread, QTimer + DAQ thread + visible plot) ruled out
# the DAQ thread and antialiasing as the cause and identified PyQtGraph's
# actual SOFTWARE rendering (QGraphicsView/GraphicsLayoutWidget without
# GPU acceleration) as the bottleneck - `useOpenGL=True` (requires
# PyOpenGL, see requirements.txt) reduced the tick gap in testing from
# an average of ~89ms to ~34ms.
pg.setConfigOptions(antialias=True, useOpenGL=True)

_DEFAULT_DISPLAY_WINDOW_SECONDS = 5.0
_UI_UPDATE_INTERVAL_MS = 15  # ~66 Hz; with useOpenGL=True, rendering is no longer the bottleneck
_STORAGE_UPDATE_INTERVAL_MS = 1000  # file access (stat) less often than the plot update
# Upper bound for `Channel.plot_value_refresh_hz`: refreshing the readout
# more often than the view ticks is impossible, so the spin box stops
# exactly there instead of offering a rate that silently caps.
_MAX_VALUE_REFRESH_HZ = 1000.0 / _UI_UPDATE_INTERVAL_MS

# Upper bound for the number of channels the main grid places side by
# side. Not a technical limit - beyond this the individual plot is
# narrower than the value readout beside it, so the layout stops being
# useful before it stops being possible.
_MAX_PLOT_COLUMNS = 4

# Grid line opacity, previously hardwired at three call sites.
_GRID_ALPHA = 0.3
# Upper bound on the point count passed to `curve.setData()` (see
# `_downsample_for_display`) - well more than any screen has horizontal
# pixels, so the curve looks visually unchanged.
_MAX_DISPLAY_POINTS_PER_CURVE = 2000
# Large value display next to the subplot (see `Channel.plot_show_value`)
# in the main grid column and popout window, each with its OWN
# column/label for the number and the unit. The number is rendered in a
# fixed format made of `Channel.plot_value_integer_digits` integer digits
# plus `_VALUE_DECIMALS` decimal digits (see `_format_channel_value`) -
# ALWAYS exactly the same length (sign space reserved, zero-padded),
# otherwise the display would jitter slightly on every new value, or get
# truncated as the digit count grows. If a value does NOT fit the
# configured format, hash marks appear instead of a misleadingly
# truncated number (like on a DIAdem/LabVIEW digital readout) - the
# field width is therefore computed directly from the format (see
# `_number_field_width_px`), not guessed. The unit additionally gets its
# OWN label, which is set once at build time and never updated per tick
# again - if it were instead part of the same text as the number, it
# would "jitter" with every new value, even within an overall
# fixed-width field.
_VALUE_DECIMALS = 3
_VALUE_NUMBER_POINT_SIZE = 18
_VALUE_UNIT_POINT_SIZE = 18
_VALUE_UNIT_WIDTH = 70

_STORAGE_WARN_PERCENT = 70.0
_STORAGE_CRITICAL_PERCENT = 90.0


def _format_bytes(num_bytes: float) -> str:
    """Formats a byte count in a human-readable way (e.g. "12.3 MB")."""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < 1024.0:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def _channel_background_color(channel: Channel) -> str:
    """Background color of ONE channel for the actual plot area
    (ViewBox) - theme default if no custom color is configured. Applies
    deliberately ONLY to the plot area itself, NOT to the surrounding
    value/unit display (see
    `gui/theme.py::plot_container_background_color`) - a custom channel
    color should only be visible where data actually lands."""
    return (
        plot_background_color()
        if is_theme_default_plot_background(channel.plot_background)
        else channel.plot_background or plot_background_color()
    )


def _channel_grid_color(channel: Channel) -> str:
    """Grid line color of ONE channel - theme default (foreground color)
    if no custom color is configured. Set via `AxisItem.setTickPen()`
    (see `_apply_channel_curve_style`) - by default, PyQtGraph derives
    grid lines from the axis pen (`setPen()`, axis text/tick color); a
    separate `tickPen` allows an independent color without changing the
    axis text/tick mark color itself (see `style_plot_item`)."""
    return channel.plot_grid_color or plot_foreground_color()


def _axis_label_style() -> dict[str, str]:
    """CSS style kwargs for `AxisItem.setLabel()` - same point size as
    the axis tick label (see `gui/theme.py::axis_tick_point_size`),
    instead of PyQtGraph's smaller default for axis titles. MUST be set
    before `axis.setPen()`/`style_plot_item()`: `setLabel(**kwargs)`
    REPLACES `labelStyle` entirely, whereas `setPen()` only adds the
    color to it (`labelStyle['color']`) - in the reverse order, the
    color would be lost again."""
    return {"font-size": f"{axis_tick_point_size()}pt"}


def _channel_axis_label(channel: Channel) -> str:
    """Y axis label of a channel: display name, plus unit in square
    brackets if present (see the `axis_time` time axis label for the
    same bracket convention) - the same combination as the plot title
    (see `_rebuild_plots`/`ChannelPopoutWindow.__init__`), here
    additionally shown directly on the axis."""
    unit_suffix = f" [{channel.unit}]" if channel.unit else ""
    return f"{channel.display_name}{unit_suffix}"


def _apply_axis_label_visibility(plot_item, channel: Channel) -> None:
    """Shows/hides the X and Y axis TITLES of `plot_item` according to
    `Channel.plot_show_x_label`/`plot_show_y_label`.

    `AxisItem.showLabel()` rather than clearing the text via
    `setLabel("")`: it also gives the reserved space back
    (`_updateWidth()`/`_updateHeight()` internally), so the plot area
    actually grows - which is the point of switching a title off. Tick
    marks, tick numbers and the grid are untouched.

    MUST be called AFTER every `setLabel()` on the same axis:
    `setLabel()` internally ends with `showLabel(bool(text))` and would
    otherwise silently switch a hidden title back on - which is exactly
    what happens on a language change (see `LiveView.retranslate_ui`).
    """
    for axis_name, show in (
        ("bottom", channel.plot_show_x_label),
        ("left", channel.plot_show_y_label),
    ):
        axis = plot_item.getAxis(axis_name)
        if axis is not None:
            axis.showLabel(show)


def _channel_display_key(channel: Channel) -> tuple[str, str]:
    """Unique key for display-related dicts/caches (dialog rows, popout
    window tracking, Y range cache) - NOT just `hardware_channel` alone.

    The live view can deliberately also be configured and previewed
    WITHOUT connected hardware (see `LiveView.preview_channels`) -
    several not-yet-assigned channels would then all share the same
    empty `hardware_channel` value and would overwrite each other in
    every dict indexed by it (e.g. several "own window" checkboxes that
    end up sharing the same window). The additional `display_name` makes
    the key unique in this case too - newly created channels are
    already automatically numbered ("Channel 1", "Channel 2", ...), see
    `gui/widgets/channel_table.py::_on_add_clicked`.
    """
    return (channel.hardware_channel, channel.display_name)


# Plot-wide settings: with several channels in ONE plot (see
# `Channel.plot_number`/`_plot_slots`) these exist only once per plot - one
# X axis means one time window, one plot area one background/grid/Y range.
# They are therefore kept identical across the visible members of a group
# (see `sync_plot_groups`); everything else (curve color/width, value
# readout, visibility) stays per channel.
PLOT_LEVEL_FIELDS = (
    "plot_background",
    "plot_grid_color",
    "plot_y_min",
    "plot_y_max",
    "plot_autoscale",
    "plot_time_window_seconds",
    "plot_show_x_label",
    "plot_show_y_label",
    "plot_show_grid",
    "plot_show_graph",
    "plot_popout",
    "plot_popout_x",
    "plot_popout_y",
    "plot_popout_width",
    "plot_popout_height",
)

# Highest plot number selectable in the channel display dialog.
MAX_PLOT_NUMBER = 99


@dataclass(frozen=True)
class _PlotSlot:
    """ONE plot of the live view (main grid cell or own window): the
    indices into `LiveView._channels` drawn into it, in channel order.
    The first one ("lead") supplies the plot-wide settings (see
    `PLOT_LEVEL_FIELDS`) - after `sync_plot_groups` they are identical
    for all members anyway."""

    indices: tuple[int, ...]

    @property
    def lead(self) -> int:
        return self.indices[0]

    @property
    def is_group(self) -> bool:
        return len(self.indices) > 1


def effective_plot_numbers(channels: list[Channel]) -> list[int]:
    """Plot number of every channel (see `Channel.plot_number`): the set
    one, or - for channels without one - the next free numbers after all
    set ones, in channel order. With no numbers set at all that is
    simply 1, 2, 3, ... i.e. one plot per channel, as before. Counting on
    from the HIGHEST set number (instead of filling gaps) means a new
    channel can never silently land in an existing shared plot."""
    numbers = [c.plot_number for c in channels]
    next_free = max((n for n in numbers if n is not None), default=0) + 1
    result: list[int] = []
    for number in numbers:
        if number is None:
            number = next_free
            next_free += 1
        result.append(number)
    return result


def _plot_slots(channels: list[Channel]) -> list[_PlotSlot]:
    """Groups the VISIBLE channels (`Channel.plot_visible`) into plots by
    plot number (see `effective_plot_numbers`): same number = one shared
    plot, members in channel order. The plots are ordered by number."""
    by_number: dict[int, list[int]] = {}
    for index, (channel, number) in enumerate(zip(channels, effective_plot_numbers(channels))):
        if channel.plot_visible:
            by_number.setdefault(number, []).append(index)
    return [_PlotSlot(tuple(by_number[number])) for number in sorted(by_number)]


def sync_plot_groups(channels: list[Channel]) -> None:
    """Copies the plot-wide settings (`PLOT_LEVEL_FIELDS`) of each group's
    first visible channel to its other visible members, in place - so a
    group can never end up with e.g. two different time windows (the
    sweep buffer is sized per channel by `plot_time_window_seconds`, see
    `LiveView._channel_display_capacity`). Hidden members are left alone;
    they are synced once they become visible again."""
    for slot in _plot_slots(channels):
        if not slot.is_group:
            continue
        lead = channels[slot.lead]
        for index in slot.indices[1:]:
            for field in PLOT_LEVEL_FIELDS:
                setattr(channels[index], field, getattr(lead, field))


def _plot_title(channels: list[Channel]) -> str:
    """Plot/window title: "Name [unit]" for a single channel (as before),
    the channel names for a group (units are on the Y axis/legend)."""
    if len(channels) == 1:
        unit_suffix = f" [{channels[0].unit}]" if channels[0].unit else ""
        return f"{channels[0].display_name}{unit_suffix}"
    return ", ".join(channel.display_name for channel in channels)


def _plot_axis_label(channels: list[Channel]) -> str:
    """Y axis title of a plot. Single channel: as before (see
    `_channel_axis_label`). Group with one common unit: just that unit -
    the names are in the legend. Group with different units: every
    channel with its unit, e.g. "Kraft [N], Weg [mm]" (ONE shared Y axis,
    so the title has to say what is on it)."""
    if len(channels) == 1:
        return _channel_axis_label(channels[0])
    units = {channel.unit for channel in channels}
    if len(units) == 1:
        unit = next(iter(units))
        return f"[{unit}]" if unit else ""
    return ", ".join(_channel_axis_label(channel) for channel in channels)


def _union_value_range(
    ranges: list[tuple[float, float] | None],
) -> tuple[float, float] | None:
    """Combined min/max of several curves for the shared Y axis of a
    group (see `LiveView._apply_channel_y_range`). NaN of one curve is
    ignored as long as another curve has a real value; for a single
    curve the range passes through unchanged."""
    present = [r for r in ranges if r is not None]
    if not present:
        return None
    if len(present) == 1:
        return present[0]
    return (
        float(np.fmin.reduce([r[0] for r in present])),
        float(np.fmax.reduce([r[1] for r in present])),
    )


def _space_width_px(font: QFont) -> float:
    """Width of a space character in pixels for `font` - fixed gap
    between the value and the unit (see `ChannelPopoutWindow`/
    `LiveView._make_value_box`), instead of a "random" gap whose size
    would otherwise vary with layout/alignment."""
    return QFontMetrics(font).horizontalAdvance(" ")


def _style_legend(plot_item) -> None:
    """Legend of a plot group in the current theme: text in the theme
    foreground color (PyQtGraph's default is a fixed light gray,
    unreadable on the light theme) on a semi-transparent plot background,
    so the legend stays readable where it overlaps the curves. Also
    recolors entries that already exist (theme change) - PyQtGraph's
    `setLabelTextColor` alone only affects entries added afterwards.
    No-op for plots without a legend (single channel)."""
    legend = getattr(plot_item, "legend", None)
    if legend is None:
        return
    foreground = plot_foreground_color()
    background = QColor(plot_background_color())
    background.setAlpha(230)
    border = QColor(foreground)
    border.setAlpha(60)
    legend.setLabelTextColor(foreground)
    legend.setBrush(pg.mkBrush(background))
    legend.setPen(pg.mkPen(border))
    for _sample, label in legend.items:
        label.setText(label.text, color=foreground)


def _downsample_for_display(
    times: np.ndarray,
    values: np.ndarray,
    capacity: int,
    max_points: int = _MAX_DISPLAY_POINTS_PER_CURVE,
) -> tuple[np.ndarray, np.ndarray]:
    """Reduces `times`/`values` to at most `2 * max_points` points via a
    min/max envelope (like PyQtGraph's own `downsampleMethod='peak'`),
    BEFORE they're handed to PyQtGraph.

    Necessary at very high sample rates (e.g. NI9234 at 51200 Hz across
    several channels at once): PyQtGraph's own `autoDownsample` also
    reduces the data, but recomputes on EVERY `setData()` call (up to
    ~66x/s, see `_UI_UPDATE_INTERVAL_MS`) over the entire sweep window
    (up to sample rate * time span points PER CHANNEL) - that made the
    live plot noticeably stutter with several high-rate channels. This
    simple, fixed reduction BEFORE `setData()` is considerably cheaper
    than PyQtGraph's more generic, view-range-dependent variant and
    makes it unnecessary (see `curve.setDownsampling(auto=False)` when
    the curve is created).

    DELIBERATELY a min/max envelope instead of an average: a fast-decaying
    oscillation (e.g. the typical ringdown after tapping an accelerometer)
    would be almost completely smoothed away by averaging under heavy
    compression (often >100x here) - the envelope (min AND max per bin
    instead of their average) keeps the amplitude visible, even though
    individual oscillation cycles are no longer resolved.

    `capacity` (see `LiveView._channel_display_capacity`) is deliberately
    NOT `len(values)`: `values` keeps growing within a sweep on every
    tick, so a factor derived from it would shift the bin assignment
    slightly on EVERY tick - a single peak value would then sometimes
    land isolated in its own bin, sometimes averaged in with neighboring
    values, visible as the peak "wobbling" between a low and a high
    display value, even though the underlying raw data at that point
    isn't changing anymore. With a factor derived from the (fixed)
    window capacity, the bin assignment stays stable for the entire
    sweep - bins that are already fully filled no longer change, only
    the currently-filling last bin keeps moving (as expected).

    Deliberately intended ONLY for the DISPLAY (curve rendering) -
    autoscaling (`LiveView._apply_channel_y_range`) and the large value
    display (`_format_channel_value`, `values[-1]`) continue to use the
    full resolution from `LiveView._get_channel_display_view`.
    """
    n = values.shape[0]
    if n <= 2 * max_points:
        return times, values
    factor = max(1, capacity // max_points)
    if factor < 2 or n < factor:
        return times, values
    bin_count = n // factor
    usable = bin_count * factor
    binned_values = values[:usable].reshape(bin_count, factor)

    # TWO points per bin (max, min) instead of one average - see
    # docstring. `stx` centers the bin's x value somewhat (instead of
    # always taking its first sample), as in PyQtGraph's `peak` method.
    stx = factor // 2
    downsampled_times = np.repeat(times[stx:stx + usable:factor], 2)
    downsampled_values = np.empty(bin_count * 2, dtype=values.dtype)
    downsampled_values[0::2] = binned_values.max(axis=1)
    downsampled_values[1::2] = binned_values.min(axis=1)

    # Do NOT discard the remaining, not-yet-full bin: otherwise the
    # curve tip would lag up to `factor` samples behind the actual write
    # position and jump forward abruptly each time a bin fills up,
    # instead of visibly growing with every tick (see the class docstring
    # "deliberately NO reveal pacing" - this here is not additional
    # smoothing/delaying, just not discarding data that has already
    # arrived).
    remainder = n - usable
    if remainder > 0:
        tail = values[usable:]
        tail_time = times[usable + remainder // 2]
        downsampled_times = np.append(downsampled_times, (tail_time, tail_time))
        downsampled_values = np.append(downsampled_values, (tail.max(), tail.min()))
    return downsampled_times, downsampled_values


class _SweepCache:
    """Incrementally maintained derived data of ONE channel's current
    sweep: time axis, min/max envelope (see `_downsample_for_display`)
    and the running value range (for `LiveView._apply_channel_y_range`).

    Why: within a sweep, the display buffer only ever grows at the end
    (see `LiveView._write_to_display_buffer`) - but the time axis, the
    envelope and min/max used to be recomputed over the WHOLE sweep on
    every tick (~66x/s). At e.g. 16 channels x 51.2 kHz x 5 s that is
    ~256k samples per channel per tick, and made up most of the per-tick
    Python time - noticeable as a stuttering live view on older PCs.
    Here, every tick only processes the samples added since the last
    one; the result is identical to the full recomputation (same bins,
    same values, same NaN propagation), which
    `tests/test_live_view_cache.py` checks against
    `_downsample_for_display`.

    Becomes invalid (and is rebuilt by `LiveView._sweep_cache`) as soon
    as a new sweep starts, the position moves backwards or capacity/rate
    change.
    """

    __slots__ = (
        "cycle_start", "capacity", "rate", "position", "times",
        "factor", "bins", "env_max", "env_min", "value_min", "value_max",
    )

    def __init__(self, cycle_start: float, capacity: int, rate: float) -> None:
        self.cycle_start = cycle_start
        self.capacity = capacity
        self.rate = rate
        self.position = 0
        # Same values as `cycle_start + np.arange(position) / rate`
        # (element-wise identical), computed ONCE per sweep.
        self.times = cycle_start + (np.arange(capacity) / rate)
        self.factor = max(1, capacity // _MAX_DISPLAY_POINTS_PER_CURVE)
        self.bins = 0
        self.env_max = np.empty(capacity // self.factor + 1)
        self.env_min = np.empty(capacity // self.factor + 1)
        self.value_min: float | None = None
        self.value_max: float | None = None

    def matches(self, cycle_start: float, capacity: int, rate: float, position: int) -> bool:
        return (
            self.cycle_start == cycle_start
            and self.capacity == capacity
            and self.rate == rate
            and position >= self.position
        )

    def advance(self, values: np.ndarray) -> None:
        """Takes in `values` = the sweep's samples so far (only the part
        beyond `self.position` is new)."""
        position = values.shape[0]
        new = values[self.position:position]
        if new.size:
            # `np.minimum`/`np.maximum` propagate NaN like `np.min` over
            # the whole sweep did.
            new_min, new_max = new.min(), new.max()
            self.value_min = new_min if self.value_min is None else np.minimum(self.value_min, new_min)
            self.value_max = new_max if self.value_max is None else np.maximum(self.value_max, new_max)
        complete_bins = position // self.factor
        if complete_bins > self.bins:
            block = values[self.bins * self.factor:complete_bins * self.factor].reshape(
                -1, self.factor
            )
            self.env_max[self.bins:complete_bins] = block.max(axis=1)
            self.env_min[self.bins:complete_bins] = block.min(axis=1)
            self.bins = complete_bins
        self.position = position

    def value_range(self) -> tuple[float, float] | None:
        if self.value_min is None:
            return None
        return float(self.value_min), float(self.value_max)

    def display_data(self, values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Same result as `_downsample_for_display(times, values,
        capacity)`, from the cached envelope."""
        n = values.shape[0]
        times = self.times[:n]
        factor = self.factor
        if n <= 2 * _MAX_DISPLAY_POINTS_PER_CURVE or factor < 2 or n < factor:
            return times, values
        bin_count = n // factor
        usable = bin_count * factor
        stx = factor // 2
        downsampled_times = np.repeat(times[stx:stx + usable:factor], 2)
        downsampled_values = np.empty(bin_count * 2, dtype=values.dtype)
        downsampled_values[0::2] = self.env_max[:bin_count]
        downsampled_values[1::2] = self.env_min[:bin_count]
        remainder = n - usable
        if remainder > 0:
            tail = values[usable:]
            tail_time = times[usable + remainder // 2]
            downsampled_times = np.append(downsampled_times, (tail_time, tail_time))
            downsampled_values = np.append(downsampled_values, (tail.max(), tail.min()))
        return downsampled_times, downsampled_values


def _format_channel_value(
    value: float, integer_digits: int, decimals: int = _VALUE_DECIMALS
) -> str:
    """Formats `value` using a fixed number format with `integer_digits`
    integer digits and `decimals` decimal digits (see
    `Channel.plot_value_integer_digits`).

    ALWAYS exactly the same length - sign space reserved (a space for
    positive values instead of a missing character) and integer digits
    zero-padded - otherwise the display would visibly jitter on every
    new value (sign change, growing digit count), see
    `_rebuild_plots`/`ChannelPopoutWindow`.

    If the value does NOT fit the format (more integer digits than
    provided for), hash-mark placeholders are shown instead of a
    misleadingly truncated number - like on a DIAdem/LabVIEW digital
    readout, whose digit width is likewise fixed.
    """
    sign = "-" if value < 0 else " "
    text = f"{abs(value):.{decimals}f}"
    int_part, _, frac_part = text.partition(".")
    if len(int_part) > integer_digits:
        int_part = "#" * integer_digits
        frac_part = "#" * decimals
    else:
        int_part = int_part.zfill(integer_digits)
    return f"{sign}{int_part}.{frac_part}" if decimals else f"{sign}{int_part}"


def _number_field_width_px(
    font: QFont, integer_digits: int, decimals: int = _VALUE_DECIMALS
) -> int:
    """Maximum pixel width `_format_channel_value` needs for
    `font`/`integer_digits` (plus a small safety margin) - the fixed
    field width is thus computed directly from the configured number
    format instead of guessed (see the comment at `_VALUE_DECIMALS`)."""
    mask = "-" + ("0" * integer_digits) + ("." + "0" * decimals if decimals else "")
    return QFontMetrics(font).horizontalAdvance(mask) + 10


_VALUE_FORMAT_PATTERN = QRegularExpression(r"0{1,6}(\.0{0,6})?")


def _value_format_text(integer_digits: int, decimal_digits: int) -> str:
    """Builds the format pattern editable in the dialog (e.g. "000.0000")
    from integer/decimal digit counts - inverse of `_parse_value_format`."""
    text = "0" * integer_digits
    if decimal_digits:
        text += "." + "0" * decimal_digits
    return text


def _parse_value_format(text: str) -> tuple[int, int]:
    """Parses a format pattern like "000.0000" (see `_value_format_text`)
    back into (integer digits, decimal digits) - tolerates empty/not
    exactly matching input (falls back to at least 1 integer digit); the
    `QRegularExpressionValidator` on the input field already prevents
    most invalid input while typing anyway."""
    int_part, _, dec_part = text.partition(".")
    integer_digits = max(1, min(6, len(int_part)))
    decimal_digits = max(0, min(6, len(dec_part)))
    return integer_digits, decimal_digits


class ChannelDisplayDialog(QDialog):
    """Dialog for the PER-CHANNEL live view display: visibility, own
    window, plot on/off, value display on/off.

    Opened via Options -> "Set live view display..." (see
    `gui/main_window.py::_build_menu`). Already usable before a
    measurement starts (channels come from the setup configuration in
    that case, see `gui/main_window.py::_on_open_channel_display_dialog`).

    The actual detail settings (colors/Y range/time span for the plot,
    number format for the value) no longer live directly in this row -
    with the number of options by now, the row would otherwise be
    unreadably long. Instead, the "Plot"/"Value" buttons (ellipsis icon,
    like the selection buttons in `gui/widgets/channel_table.py`) each
    open their own dialog (`ChannelPlotSettingsDialog`/
    `ChannelValueSettingsDialog`) - the row itself stays compact: active,
    own window, plot (checkbox + button), value (checkbox + button).
    This also makes e.g. "value only, no chart" possible (plot checkbox
    off, value checkbox on).

    The "Plot" number puts channels with the same number into ONE shared
    plot (see `Channel.plot_number`); plots are ordered by number. The plot-wide settings (`PLOT_LEVEL_FIELDS`:
    own window, plot on/off, background/grid, Y range, time span, axis
    titles) are kept identical within a group right here in the dialog:
    changing them for one member changes them for all, and a channel
    joining a group takes over the group's current values. Curve
    color/width and the value readout stay per channel.

    The "own window" checkbox (like every other field here) only takes
    effect after OK - unlike earlier versions of this dialog, clicking
    the checkbox does NOT immediately open a window. The actual
    opening/closing is handled by `LiveView._rebuild_plots()` based on
    `Channel.plot_popout`, once `results()` has been applied via OK (see
    `LiveView._apply_display_settings_to_live_channels`).
    """

    def __init__(
        self,
        channels: list[Channel],
        default_background: str,
        default_grid_color: str,
        plot_columns: int = 1,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("channel_display_dialog_title"))

        # Detail settings live as plain values (not as permanently
        # visible widgets) - see class docstring. Pre-filled from the
        # channel, updated only when the respective sub-dialog is closed
        # via OK (see `_open_plot_settings`/`_open_value_settings`).
        self._plot_settings: dict[tuple[str, str], dict] = {}
        self._value_settings: dict[tuple[str, str], dict] = {}
        self._rows: dict[tuple[str, str], dict[str, QWidget]] = {}
        # Channel order (= `_plot_slots` order) - decides which member of
        # a group is its lead when the plot-wide values are synced.
        self._order: list[tuple[str, str]] = []
        # Guards against the group propagation below re-triggering itself.
        self._syncing_group = False
        # Per CHANNEL, not one shared set: the curve color a channel falls
        # back to depends on its position in the palette (see
        # `gui/theme.py::channel_curve_color`), so a single shared default
        # would show every swatch in the same color while the plots are
        # drawn in different ones. Filled in per channel below.
        self._color_defaults: dict[tuple[str, str], dict[str, str]] = {}

        outer_layout = QVBoxLayout(self)

        # How many channels the main grid places side by side. A VIEW
        # setting, not a channel property - hence once at the top instead
        # of in every channel row (see `LiveView.set_plot_columns`).
        columns_row = QHBoxLayout()
        self._columns_spin = NoWheelSpinBox()
        self._columns_spin.setRange(1, _MAX_PLOT_COLUMNS)
        self._columns_spin.setValue(max(1, min(_MAX_PLOT_COLUMNS, int(plot_columns))))
        self._columns_spin.setToolTip(t("plot_columns_tooltip"))
        columns_row.addWidget(QLabel(f"{t('plot_columns')}:"))
        columns_row.addWidget(self._columns_spin)
        columns_row.addStretch(1)
        outer_layout.addLayout(columns_row)

        separator_top = QFrame()
        separator_top.setFrameShape(QFrame.Shape.HLine)
        separator_top.setFrameShadow(QFrame.Shadow.Sunken)
        outer_layout.addWidget(separator_top)

        # The channel rows sit in a scroll area, the buttons stay outside
        # it. The dialog used to be `SetFixedSize` and grew by ~41px per
        # channel: a fully populated chassis (8 x NI9215 = 32 channels)
        # came to ~1350px, so on a 1080p screen the OK button ended up off
        # the screen with no way to scroll or resize to it. Same reasoning
        # and same construction as `gui/setup_view.py`.
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        scroll_area.setWidget(content)
        outer_layout.addWidget(scroll_area, stretch=1)
        layout = QVBoxLayout(content)
        form = QFormLayout()
        layout.addLayout(form)

        plot_numbers = effective_plot_numbers(channels)
        for index, channel in enumerate(channels):
            if index > 0:
                # Subtle separator line between channels - `addRow()`
                # with only one widget spans it across both form columns
                # (name + row), not just the second one.
                separator = QFrame()
                separator.setFrameShape(QFrame.Shape.HLine)
                separator.setFrameShadow(QFrame.Shadow.Sunken)
                form.addRow(separator)

            key = _channel_display_key(channel)
            hw_default_min = channel.min_range if channel.min_range is not None else -10.0
            hw_default_max = channel.max_range if channel.max_range is not None else 10.0
            self._plot_settings[key] = {
                # RAW values, deliberately not filled in with the theme
                # default: `None` means "follow the theme". Filling it in here
                # and writing it back on OK froze the colors of whichever theme
                # happened to be active - after that the channel never followed
                # a theme change again, and there was no way back. The plot
                # sub-dialog SHOWS the theme default for a `None`, it just no
                # longer stores it.
                "plot_color": channel.plot_color,
                "plot_background": channel.plot_background,
                "plot_grid_color": channel.plot_grid_color,
                "plot_y_min": channel.plot_y_min if channel.plot_y_min is not None else hw_default_min,
                "plot_y_max": channel.plot_y_max if channel.plot_y_max is not None else hw_default_max,
                "plot_autoscale": channel.plot_autoscale,
                "plot_time_window_seconds": channel.plot_time_window_seconds,
                "plot_show_x_label": channel.plot_show_x_label,
                "plot_show_y_label": channel.plot_show_y_label,
                "plot_show_grid": channel.plot_show_grid,
                "plot_line_width": channel.plot_line_width,
            }
            self._color_defaults[key] = {
                "plot_color": channel_curve_color(index),
                "plot_background": default_background,
                "plot_grid_color": default_grid_color,
            }
            self._value_settings[key] = {
                "plot_value_integer_digits": channel.plot_value_integer_digits,
                "plot_value_decimal_digits": channel.plot_value_decimal_digits,
                "plot_value_refresh_hz": channel.plot_value_refresh_hz,
            }

            row = QHBoxLayout()

            # Affects ONLY whether the channel appears as a subplot in
            # the main grid (see `LiveView._rebuild_plots`) - acquisition/
            # storage keep running unchanged regardless. Placed at the
            # far left (instead of at the end with the other checkboxes):
            # if the channel is inactive, the entire rest of the row is
            # grayed out (see `_on_visible_toggled` below) - the ordering
            # is meant to reflect that ("on/off first, then details").
            visible_check = QCheckBox(t("plot_visible_checkbox"))
            visible_check.setToolTip(t("plot_visible_checkbox_tooltip"))
            visible_check.setChecked(channel.plot_visible)
            row.addWidget(visible_check)

            # Plot number (see `Channel.plot_number`) - right after
            # "active": like "own window", it decides WHERE the channel
            # appears, not what of it. Pre-filled with the EFFECTIVE number
            # (1, 2, 3, ... if none is set yet), so after OK every channel
            # has an explicit one.
            row.addWidget(QLabel(f"{t('plot_number_label')}:"))
            group_spin = NoWheelSpinBox()
            group_spin.setRange(1, MAX_PLOT_NUMBER)
            group_spin.setToolTip(t("plot_number_tooltip"))
            group_spin.setValue(min(MAX_PLOT_NUMBER, plot_numbers[index]))
            # Only on Enter/focus loss/arrow click, not per keystroke:
            # typing "12" must not first join the channel to plot 1 and
            # take over that plot's settings on the way.
            group_spin.setKeyboardTracking(False)
            group_spin.valueChanged.connect(lambda _value, k=key: self._on_group_changed(k))
            row.addWidget(group_spin)

            # Takes effect (like "active") only after OK via `results()`
            # - see class docstring above. Right after "active", BEFORE
            # the plot/value options - affects WHERE the channel appears,
            # not WHAT of it is visible.
            popout_check = QCheckBox(t("popout_button"))
            popout_check.setToolTip(t("popout_button_tooltip"))
            popout_check.setChecked(channel.plot_visible and channel.plot_popout)
            row.addWidget(popout_check)

            # Plot on/off (checkbox WITHOUT its own text) + button
            # (ellipsis icon, opens `ChannelPlotSettingsDialog`) -
            # together they replace the color/range/time-span fields
            # that used to be embedded directly here.
            graph_check = QCheckBox()
            graph_check.setToolTip(t("plot_show_graph_checkbox_tooltip"))
            graph_check.setChecked(channel.plot_show_graph)
            row.addWidget(graph_check)

            graph_button = QPushButton(f" {t('plot_settings_button')}")
            graph_button.setIcon(QIcon(draw_ellipsis_icon(14)))
            graph_button.setIconSize(QSize(14, 14))
            graph_button.setToolTip(t("plot_settings_button_tooltip"))
            graph_button.setEnabled(graph_check.isChecked())
            graph_button.clicked.connect(
                lambda _checked, k=key, name=channel.display_name: self._open_plot_settings(k, name)
            )
            # Grayed out while its own checkbox is off - independent of
            # the "active" checkbox (see `_on_visible_toggled` below).
            graph_check.toggled.connect(graph_button.setEnabled)
            row.addWidget(graph_button)

            # Value on/off + button, analogous to plot above.
            value_check = QCheckBox()
            value_check.setToolTip(t("plot_show_value_checkbox_tooltip"))
            value_check.setChecked(channel.plot_show_value)
            row.addWidget(value_check)

            value_button = QPushButton(f" {t('value_settings_button')}")
            value_button.setIcon(QIcon(draw_ellipsis_icon(14)))
            value_button.setIconSize(QSize(14, 14))
            value_button.setToolTip(t("value_settings_button_tooltip"))
            value_button.setEnabled(value_check.isChecked())
            value_button.clicked.connect(
                lambda _checked, k=key, name=channel.display_name: self._open_value_settings(k, name)
            )
            value_check.toggled.connect(value_button.setEnabled)
            row.addWidget(value_button)
            # WITHOUT this stretch, Qt would distribute the extra space
            # from widening the dialog across ALL gaps in the row (even
            # between a checkbox and its button) when the dialog is
            # widened - this way the row always stays compact at the
            # left edge, regardless of window width.
            row.addStretch(1)

            # If the channel is inactive, the entire rest of the row is
            # meaningless (none of it has any visible effect) - instead
            # of only disabling the popout checkbox (previous behavior),
            # now the ENTIRE remaining row is grayed out.
            row_widgets = [
                group_spin, popout_check, graph_check, graph_button, value_check, value_button
            ]

            def _on_visible_toggled(
                checked: bool,
                widgets: list[QWidget] = row_widgets,
                popout_checkbox: QCheckBox = popout_check,
                graph_checkbox: QCheckBox = graph_check,
                graph_settings_button: QPushButton = graph_button,
                value_checkbox: QCheckBox = value_check,
                value_settings_button: QPushButton = value_button,
            ) -> None:
                for widget in widgets:
                    widget.setEnabled(checked)
                if not checked:
                    popout_checkbox.setChecked(False)
                else:
                    # When switching back on, the plot/value buttons
                    # should keep following their OWN checkbox, not
                    # simply stay enabled from the loop above.
                    graph_settings_button.setEnabled(graph_checkbox.isChecked())
                    value_settings_button.setEnabled(value_checkbox.isChecked())

            visible_check.toggled.connect(_on_visible_toggled)
            _on_visible_toggled(channel.plot_visible)

            form.addRow(channel.display_name, row)
            self._rows[key] = {
                "visible": visible_check,
                "group": group_spin,
                "popout": popout_check,
                "graph": graph_check,
                "value": value_check,
            }
            self._order.append(key)
            # `clicked`, not `toggled`: only a USER click is passed on to
            # the rest of the group - unchecking "own window" because the
            # channel was just deactivated (see `_on_visible_toggled`)
            # must not take the popout away from the other members.
            popout_check.clicked.connect(
                lambda checked, k=key: self._propagate_checkbox(k, "popout", checked)
            )
            graph_check.clicked.connect(
                lambda checked, k=key: self._propagate_checkbox(k, "graph", checked)
            )

        button_box = QDialogButtonBox()
        ok_button = button_box.addButton(t("ok"), QDialogButtonBox.ButtonRole.AcceptRole)
        cancel_button = button_box.addButton(t("cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        ok_button.clicked.connect(self.accept)
        cancel_button.clicked.connect(self.reject)
        # OUTSIDE the scroll area, so the buttons stay reachable however
        # many channels there are (see the scroll area above).
        outer_layout.addWidget(button_box)

    def _group_of(self, key: tuple[str, str]) -> int:
        return self._rows[key]["group"].value()

    def _group_members(self, key: tuple[str, str]) -> list[tuple[str, str]]:
        """Other channels with the same plot number (in channel order)."""
        group = self._group_of(key)
        return [k for k in self._order if k != key and self._group_of(k) == group]

    def _propagate_checkbox(self, key: tuple[str, str], name: str, checked: bool) -> None:
        """Plot-wide checkbox ("own window"/"plot") clicked for one member
        -> same state for the whole group."""
        if self._syncing_group:
            return
        self._syncing_group = True
        try:
            for other in self._group_members(key):
                if self._rows[other]["visible"].isChecked():
                    self._rows[other][name].setChecked(checked)
        finally:
            self._syncing_group = False

    def _copy_plot_level_settings(self, source: tuple[str, str], target: tuple[str, str]) -> None:
        for field in PLOT_LEVEL_FIELDS:
            if field in self._plot_settings[source]:
                self._plot_settings[target][field] = self._plot_settings[source][field]

    def _on_group_changed(self, key: tuple[str, str]) -> None:
        """A channel joining a group takes over the group's current
        plot-wide values (time span, Y range, ..., own window, plot
        on/off) - otherwise the first member's values would silently win
        on OK anyway (see `results()`), unseen by the user."""
        members = [k for k in self._group_members(key) if self._rows[k]["visible"].isChecked()]
        if not members:
            return
        source = members[0]
        self._copy_plot_level_settings(source, key)
        self._syncing_group = True
        try:
            for name in ("popout", "graph"):
                self._rows[key][name].setChecked(self._rows[source][name].isChecked())
        finally:
            self._syncing_group = False

    def column_count(self) -> int:
        """Channels placed side by side in the main grid, as chosen at the
        top of the dialog.

        Deliberately NOT part of `results()`: that is keyed per channel and
        flows into `Channel.plot_*`, while this is a property of the view
        (see `LiveView.set_plot_columns`).
        """
        return self._columns_spin.value()

    def _open_plot_settings(self, key: tuple[str, str], channel_name: str) -> None:
        dialog = ChannelPlotSettingsDialog(
            channel_name,
            self._plot_settings[key],
            self,
            channel_count=len(self._plot_settings),
            color_defaults=self._color_defaults.get(key),
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._apply_sub_dialog_result(self._plot_settings, key, dialog)
            # The plot-wide part (everything but curve color/width) holds
            # for the whole group (see `PLOT_LEVEL_FIELDS`).
            for other in self._group_members(key):
                self._copy_plot_level_settings(key, other)

    def _open_value_settings(self, key: tuple[str, str], channel_name: str) -> None:
        dialog = ChannelValueSettingsDialog(
            channel_name,
            self._value_settings[key],
            self,
            channel_count=len(self._value_settings),
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._apply_sub_dialog_result(self._value_settings, key, dialog)

    @staticmethod
    def _apply_sub_dialog_result(store: dict, key: tuple[str, str], dialog) -> None:
        """Writes a sub-dialog result to one channel, or to all of them.

        A COPY per channel, not the same dict object: the stores are
        mutated per key elsewhere, and sharing one instance would make an
        edit to a single channel silently change every other one too.
        """
        result = dialog.results()
        if dialog.apply_to_all():
            for other_key in store:
                store[other_key] = dict(result)
        else:
            store[key] = result

    def results(self) -> dict[tuple[str, str], dict]:
        """Returns the values set per channel (only valid on OK).

        The key is `_channel_display_key(channel)` (see there), NOT just
        `hardware_channel` - format per channel matches
        `Channel.plot_*`/`ChannelTableWidget.apply_display_settings`/
        `LiveView._apply_display_settings_to_live_channels`.
        """
        results: dict[tuple[str, str], dict] = {}
        for key, row in self._rows.items():
            results[key] = {
                **self._plot_settings[key],
                **self._value_settings[key],
                "plot_show_graph": row["graph"].isChecked(),
                "plot_show_value": row["value"].isChecked(),
                "plot_visible": row["visible"].isChecked(),
                "plot_popout": row["popout"].isChecked(),
                "plot_number": row["group"].value(),
            }
        # Safety net on top of the live syncing above: the first ACTIVE
        # member of each group (= the plot's lead, see `_plot_slots`)
        # decides the plot-wide values for all active members.
        leads: dict[int, tuple[str, str]] = {}
        for key in self._order:
            values = results[key]
            group = values["plot_number"]
            if not values["plot_visible"]:
                continue
            lead = leads.setdefault(group, key)
            if lead != key:
                for field in PLOT_LEVEL_FIELDS:
                    if field in results[lead]:
                        values[field] = results[lead][field]
        return results


class ChannelPlotSettingsDialog(QDialog):
    """Fine-grained settings for the plot area of ONE channel (curve/
    background/grid line color, Y range, autoscaling, time span, axis
    titles) - opened via the "Plot" button in `ChannelDisplayDialog`, to
    keep its row compact given the now-large number of options."""

    def __init__(
        self,
        channel_name: str,
        settings: dict,
        parent: QWidget | None = None,
        channel_count: int = 1,
        color_defaults: dict | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{t('plot_settings_dialog_title')} - {channel_name}")
        self._apply_to_all = False
        # Only for the confirmation prompt in `_on_apply_to_all`,
        # which names how many channels are about to be overwritten.
        self._channel_count = channel_count

        # Each may be `None` = "follow the theme". The swatches show
        # `_effective()` so a color is still visible, but only a color
        # actually PICKED is stored (see `_pick_color`/`results`).
        self._color = settings.get("plot_color")
        self._background = settings.get("plot_background")
        self._grid_color = settings.get("plot_grid_color")
        self._color_defaults = color_defaults or {}

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self._color_button = QPushButton()
        self._color_button.setFixedSize(24, 24)
        self._update_swatch(self._color_button, self._effective("plot_color"))
        self._color_button.clicked.connect(lambda: self._pick_color("color"))
        form.addRow(f"{t('plot_color')}:", self._color_button)

        self._bg_button = QPushButton()
        self._bg_button.setFixedSize(24, 24)
        self._update_swatch(self._bg_button, self._effective("plot_background"))
        self._bg_button.clicked.connect(lambda: self._pick_color("background"))
        form.addRow(f"{t('plot_background')}:", self._bg_button)

        self._grid_button = QPushButton()
        self._grid_button.setFixedSize(24, 24)
        self._update_swatch(self._grid_button, self._effective("plot_grid_color"))
        self._grid_button.clicked.connect(lambda: self._pick_color("grid"))
        form.addRow(f"{t('plot_grid_color')}:", self._grid_button)
        self._reset_colors_button = QPushButton(t("reset_colors_to_theme"))
        self._reset_colors_button.setToolTip(t("reset_colors_to_theme_tooltip"))
        self._reset_colors_button.clicked.connect(self._reset_colors_to_theme)
        form.addRow("", self._reset_colors_button)
        # Directly below the grid COLOR, which it belongs with: the color
        # was configurable while the grid itself used to be hardwired on.
        self._grid_check = QCheckBox(t("show_grid_checkbox"))
        self._grid_check.setToolTip(t("show_grid_checkbox_tooltip"))
        self._grid_check.setChecked(settings.get("plot_show_grid", True))
        form.addRow("", self._grid_check)

        self._min_spin = PrecisionDoubleSpinBox()
        self._min_spin.setRange(-1e9, 1e9)
        self._min_spin.setValue(settings["plot_y_min"])
        form.addRow(f"{t('min')}:", self._min_spin)

        self._max_spin = PrecisionDoubleSpinBox()
        self._max_spin.setRange(-1e9, 1e9)
        self._max_spin.setValue(settings["plot_y_max"])
        form.addRow(f"{t('max')}:", self._max_spin)

        self._autoscale_check = QCheckBox(t("autoscale_checkbox"))
        self._autoscale_check.setToolTip(t("autoscale_checkbox_tooltip"))
        self._autoscale_check.setChecked(settings["plot_autoscale"])
        form.addRow("", self._autoscale_check)

        self._time_window_spin = NoWheelDoubleSpinBox()
        self._time_window_spin.setRange(0.1, 3600.0)
        self._time_window_spin.setDecimals(1)
        self._time_window_spin.setSingleStep(0.5)
        self._time_window_spin.setValue(settings["plot_time_window_seconds"])
        form.addRow(f"{t('plot_time_window_seconds')}:", self._time_window_spin)

        # Axis TITLES only, switchable per axis - ticks/numbers/grid are
        # deliberately NOT affected (see `Channel.plot_show_x_label`).
        # Separate checkboxes rather than one shared "axis labels": with
        # several subplots stacked in one grid it is usually the X title
        # ("Time [s]", identical on every subplot) that is redundant,
        # while the Y title still names the channel.
        self._x_label_check = QCheckBox(t("show_x_axis_label_checkbox"))
        self._x_label_check.setToolTip(t("axis_label_checkbox_tooltip"))
        self._x_label_check.setChecked(settings.get("plot_show_x_label", True))
        form.addRow("", self._x_label_check)

        self._y_label_check = QCheckBox(t("show_y_axis_label_checkbox"))
        self._y_label_check.setToolTip(t("axis_label_checkbox_tooltip"))
        self._y_label_check.setChecked(settings.get("plot_show_y_label", True))
        form.addRow("", self._y_label_check)

        self._line_width_spin = NoWheelDoubleSpinBox()
        self._line_width_spin.setRange(0.5, 10.0)
        self._line_width_spin.setDecimals(1)
        self._line_width_spin.setSingleStep(0.5)
        self._line_width_spin.setValue(
            max(0.5, float(settings.get("plot_line_width", 1.5)))
        )
        self._line_width_spin.setToolTip(t("plot_line_width_tooltip"))
        form.addRow(f"{t('plot_line_width')}:", self._line_width_spin)

        button_box = QDialogButtonBox()
        ok_button = button_box.addButton(t("ok"), QDialogButtonBox.ButtonRole.AcceptRole)
        # Applies these settings to EVERY channel instead of only the
        # edited one - with a fully populated chassis, configuring 32
        # channels one dialog at a time is not realistic. Deliberately a
        # button rather than a checkbox: it accepts the dialog in the same
        # click, so the scope of the action is decided at the moment of
        # confirming rather than remembered as hidden state.
        # `ResetRole` only for placement (left of OK/Cancel), the button
        # does not reset anything.
        apply_all_button = button_box.addButton(
            t("apply_to_all_channels"), QDialogButtonBox.ButtonRole.ResetRole
        )
        apply_all_button.setToolTip(t("apply_to_all_channels_tooltip"))
        cancel_button = button_box.addButton(t("cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        ok_button.clicked.connect(self.accept)
        apply_all_button.clicked.connect(self._on_apply_to_all)
        cancel_button.clicked.connect(self.reject)
        layout.addWidget(button_box)

    def _effective(self, key: str) -> str:
        """The color to SHOW for `key`: the channel's own if it has one,
        otherwise the current theme default.

        Kept apart from what `results()` stores, so a swatch can display
        a concrete color while the channel keeps following the theme."""
        stored = {
            "plot_color": self._color,
            "plot_background": self._background,
            "plot_grid_color": self._grid_color,
        }[key]
        return stored or self._color_defaults.get(key, "#808080")

    def _reset_colors_to_theme(self) -> None:
        """Drops this channel's own colors so it follows the theme again.

        The only way back: once a color was set it stayed, and a
        configuration carrying the other theme's colors - a black grid on
        a dark background, say - could not be repaired from the dialog at
        all."""
        self._color = None
        self._background = None
        self._grid_color = None
        self._update_swatch(self._color_button, self._effective("plot_color"))
        self._update_swatch(self._bg_button, self._effective("plot_background"))
        self._update_swatch(self._grid_button, self._effective("plot_grid_color"))

    def _pick_color(self, which: str) -> None:
        current = {
            "color": self._color,
            "background": self._background,
            "grid": self._grid_color,
        }[which]
        color = QColorDialog.getColor(QColor(current), self)
        if not color.isValid():
            return
        if which == "color":
            self._color = color.name()
            self._update_swatch(self._color_button, self._color)
        elif which == "background":
            self._background = color.name()
            self._update_swatch(self._bg_button, self._background)
        else:
            self._grid_color = color.name()
            self._update_swatch(self._grid_button, self._grid_color)

    @staticmethod
    def _update_swatch(button: QPushButton, hex_color: str) -> None:
        button.setStyleSheet(f"background-color: {hex_color}; border: 1px solid #888888;")

    def _on_apply_to_all(self) -> None:
        """Asks for confirmation, then marks the result as "for every
        channel" and accepts.

        `ChannelDisplayDialog` reads `apply_to_all()` after `exec()` and
        then writes `results()` into every channel rather than one.

        Confirmed first because the action silently discards whatever was
        configured on all the OTHER channels, which are not visible from
        here - and it sits right next to OK, where a misclick is easy.
        """
        answer = QMessageBox.question(
            self,
            t("apply_to_all_confirm_title"),
            t("apply_to_all_confirm_body", count=self._channel_count),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._apply_to_all = True
        self.accept()

    def apply_to_all(self) -> bool:
        """Whether the user confirmed via "apply to all" instead of plain
        OK (see `_on_apply_to_all`)."""
        return self._apply_to_all

    def results(self) -> dict:
        return {
            # May each be `None` = follow the theme. Deliberately NOT filled
            # in with the current theme default - that is exactly what used
            # to freeze the colors on a plain OK (see `__init__`).
            "plot_color": self._color,
            "plot_background": self._background,
            "plot_grid_color": self._grid_color,
            "plot_y_min": self._min_spin.value(),
            "plot_y_max": self._max_spin.value(),
            "plot_autoscale": self._autoscale_check.isChecked(),
            "plot_time_window_seconds": self._time_window_spin.value(),
            "plot_show_x_label": self._x_label_check.isChecked(),
            "plot_show_y_label": self._y_label_check.isChecked(),
            "plot_show_grid": self._grid_check.isChecked(),
            "plot_line_width": self._line_width_spin.value(),
        }


class ChannelValueSettingsDialog(QDialog):
    """Fine-grained settings for the value display of ONE channel (number
    format, refresh rate) - opened via the "Value" button in
    `ChannelDisplayDialog`."""

    def __init__(
        self,
        channel_name: str,
        settings: dict,
        parent: QWidget | None = None,
        channel_count: int = 1,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{t('value_settings_dialog_title')} - {channel_name}")
        self._apply_to_all = False
        # Only for the confirmation prompt in `_on_apply_to_all`,
        # which names how many channels are about to be overwritten.
        self._channel_count = channel_count

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        # Format pattern instead of a plain integer-digit count (e.g.
        # "000.0000") - zeros before the dot = integer digits, zeros
        # after = decimal digits (optional, can be omitted entirely for
        # a pure integer display). See `_parse_value_format`.
        self._value_format_edit = QLineEdit(
            _value_format_text(
                settings["plot_value_integer_digits"], settings["plot_value_decimal_digits"]
            )
        )
        self._value_format_edit.setValidator(QRegularExpressionValidator(_VALUE_FORMAT_PATTERN))
        self._value_format_edit.setMaximumWidth(80)
        self._value_format_edit.setToolTip(t("plot_value_integer_digits_tooltip"))
        form.addRow(f"{t('plot_value_integer_digits')}:", self._value_format_edit)

        # Refresh rate of the READOUT, decoupled from the view's tick
        # rate (see `Channel.plot_value_refresh_hz`). Capped at the tick
        # rate itself - anything above that could not be honored.
        self._refresh_hz_spin = NoWheelDoubleSpinBox()
        # Upper bound rounded DOWN to the spin box's own precision: with
        # one decimal, a maximum of 66.666... would be displayed - and
        # returned - as 66.7, i.e. minimally ABOVE the tick rate it is
        # supposed to cap.
        self._refresh_hz_spin.setRange(0.1, math.floor(_MAX_VALUE_REFRESH_HZ * 10.0) / 10.0)
        self._refresh_hz_spin.setDecimals(1)
        self._refresh_hz_spin.setSingleStep(1.0)
        self._refresh_hz_spin.setValue(
            min(
                _MAX_VALUE_REFRESH_HZ,
                max(0.1, float(settings.get("plot_value_refresh_hz", 30.0))),
            )
        )
        self._refresh_hz_spin.setToolTip(t("plot_value_refresh_hz_tooltip"))
        form.addRow(f"{t('plot_value_refresh_hz')}:", self._refresh_hz_spin)

        button_box = QDialogButtonBox()
        ok_button = button_box.addButton(t("ok"), QDialogButtonBox.ButtonRole.AcceptRole)
        # Applies these settings to EVERY channel instead of only the
        # edited one - with a fully populated chassis, configuring 32
        # channels one dialog at a time is not realistic. Deliberately a
        # button rather than a checkbox: it accepts the dialog in the same
        # click, so the scope of the action is decided at the moment of
        # confirming rather than remembered as hidden state.
        # `ResetRole` only for placement (left of OK/Cancel), the button
        # does not reset anything.
        apply_all_button = button_box.addButton(
            t("apply_to_all_channels"), QDialogButtonBox.ButtonRole.ResetRole
        )
        apply_all_button.setToolTip(t("apply_to_all_channels_tooltip"))
        cancel_button = button_box.addButton(t("cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        ok_button.clicked.connect(self.accept)
        apply_all_button.clicked.connect(self._on_apply_to_all)
        cancel_button.clicked.connect(self.reject)
        layout.addWidget(button_box)

    def _on_apply_to_all(self) -> None:
        """Asks for confirmation, then marks the result as "for every
        channel" and accepts.

        `ChannelDisplayDialog` reads `apply_to_all()` after `exec()` and
        then writes `results()` into every channel rather than one.

        Confirmed first because the action silently discards whatever was
        configured on all the OTHER channels, which are not visible from
        here - and it sits right next to OK, where a misclick is easy.
        """
        answer = QMessageBox.question(
            self,
            t("apply_to_all_confirm_title"),
            t("apply_to_all_confirm_body", count=self._channel_count),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._apply_to_all = True
        self.accept()

    def apply_to_all(self) -> bool:
        """Whether the user confirmed via "apply to all" instead of plain
        OK (see `_on_apply_to_all`)."""
        return self._apply_to_all

    def results(self) -> dict:
        integer_digits, decimal_digits = _parse_value_format(self._value_format_edit.text())
        return {
            "plot_value_integer_digits": integer_digits,
            "plot_value_decimal_digits": decimal_digits,
            "plot_value_refresh_hz": self._refresh_hz_spin.value(),
        }


class ChannelPopoutWindow(QWidget):
    """Standalone window with the live plot of ONE plot slot - a single
    channel or a shared plot (see `Channel.plot_number`/`_PlotSlot`).

    Opened when the "own window" checkbox in the channel display dialog
    was applied via OK (see `LiveView._rebuild_plots`/
    `_open_popout_window`). Deliberately holds NO timer of its own and
    doesn't poll the ring buffer itself - the curves and Y range are
    updated by the same timer tick as the main plots (see
    `LiveView._on_timer_tick`), so the ring buffer isn't read twice.

    `curves`/`value_labels`/`unit_labels` are aligned with `channels`
    (the first one is the lead, see `_PlotSlot`); `curve`/`value_label`/
    `unit_label` are the lead's, for the single-channel case.
    """

    def __init__(
        self, channels: list[Channel] | Channel, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        if isinstance(channels, Channel):
            channels = [channels]
        self.channels: list[Channel] = list(channels)
        channel = self.channels[0]
        self.hardware_channel = channel.hardware_channel
        # Key for `LiveView._popout_windows` - the LEAD channel's key, NOT
        # `hardware_channel` alone (see `_channel_display_key`).
        self.display_key = _channel_display_key(channel)
        # Which channels this window was built for - `LiveView._rebuild_plots`
        # rebuilds the window if the group membership changes.
        self.member_keys = tuple(_channel_display_key(c) for c in self.channels)
        # Live reference to the same Channel object as `LiveView` (see
        # `LiveView._open_popout_window`) - NOT copied, so that values
        # changed later (e.g. `plot_background` via the channel display
        # dialog) are visible here without extra effort (see
        # `_style_value_labels`).
        self._channel = channel
        # Text colors of the value readouts (aligned with `channels`),
        # set by `LiveView._apply_channel_appearance`: the curve color in
        # a group (so a number can be matched to its curve), `None` =
        # theme foreground (single channel, as before).
        self.value_colors: list[str | None] = [None] * len(self.channels)
        # If the user closes the window, the C++/Qt object should
        # actually be destroyed (not just hidden) - `LiveView.
        # _on_popout_window_closed` reacts to this via the `destroyed`
        # signal, to clean up its own tracking AND (if the user closes
        # the window directly instead of using the checkbox in the
        # dialog) make the channel reappear in the main grid.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        title = _plot_title(self.channels)
        self.setWindowTitle(title)
        self.resize(640, 400)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(6, 6, 6, 6)
        row = QHBoxLayout()
        # NO default spacing between the value display and the plot: the
        # gap that QHBoxLayout's default spacing would otherwise leave
        # belongs to the WINDOW background (self), not the channel
        # color - visible as an extra, differently colored bar between
        # the unit and the plot (see `_value_container` for the same
        # effect between the number and the unit).
        row.setSpacing(0)
        outer_layout.addLayout(row)

        # Large, current value display to the left of the plot (see
        # `Channel.plot_show_value`/`LiveView._on_timer_tick`) - one line
        # per channel (stacked for a group), number and unit in TWO
        # separate, fixed-width labels (width computed directly from
        # `Channel.plot_value_integer_digits`, see
        # `_number_field_width_px`), otherwise the unit would visibly
        # shift with every new value. The unit labels are set once here
        # and never updated per tick again.
        #
        # All labels sit inside a SHARED container widget
        # (`self._value_container`), NOT directly in the `row` layout:
        # the gap that `QHBoxLayout.setSpacing()` would otherwise leave
        # between them belongs to the WINDOW background (not the channel
        # color) and appears as a visible, differently colored bar
        # between the number and the unit - the container (and every
        # line) therefore gets the same background color as the labels
        # in `_style_value_labels()`, so the gap is included in the color.
        self._value_container = QWidget()
        value_column = QVBoxLayout(self._value_container)
        value_column.setContentsMargins(0, 0, 0, 0)
        value_column.addStretch(1)

        number_font = QFont()
        number_font.setPointSize(_VALUE_NUMBER_POINT_SIZE)
        number_font.setBold(True)

        self._value_rows: list[QWidget] = []
        self.value_labels: list[QLabel] = []
        self.unit_labels: list[QLabel] = []
        for member in self.channels:
            line = QWidget()
            value_row = QHBoxLayout(line)
            value_row.setContentsMargins(0, 0, 0, 0)

            value_label = QLabel("--")
            value_label.setContentsMargins(0, 0, 0, 0)
            # RIGHT-aligned, not centered: the gap to the unit should
            # correspond exactly to one space width (see
            # `value_row.setSpacing` below) - centered, the actual gap
            # would depend on the surrounding whitespace of the
            # (fixed-format, see `_format_channel_value`) number in the
            # field.
            value_label.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            value_row.addWidget(value_label)

            unit_label = QLabel(member.unit)
            unit_label.setFixedWidth(_VALUE_UNIT_WIDTH)
            unit_label.setContentsMargins(0, 0, 0, 0)
            unit_label.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            value_row.addWidget(unit_label)
            value_row.setSpacing(round(_space_width_px(number_font)))

            value_column.addWidget(line)
            self._value_rows.append(line)
            self.value_labels.append(value_label)
            self.unit_labels.append(unit_label)
        value_column.addStretch(1)
        self.value_label = self.value_labels[0]
        self.unit_label = self.unit_labels[0]
        self._apply_number_width()
        self.apply_value_visibility()
        row.addWidget(self._value_container)

        self.plot_widget = pg.PlotWidget()
        self.plot_item = self.plot_widget.getPlotItem()
        self.plot_item.setTitle(title)
        self.plot_item.showGrid(x=channel.plot_show_grid, y=channel.plot_show_grid, alpha=_GRID_ALPHA)
        # `units=` NOT used: PyQtGraph always renders that internally in
        # round brackets - "[s]" is hardcoded in the text itself instead,
        # so the time unit is consistently shown in square brackets
        # everywhere.
        self.plot_item.setLabel("bottom", f"{t('axis_time')} [s]", **_axis_label_style())
        self.plot_item.setLabel("left", _plot_axis_label(self.channels), **_axis_label_style())
        style_plot_container(self.plot_widget)
        style_plot_item(self.plot_item)
        if len(self.channels) > 1:
            # Created BEFORE the curves: PyQtGraph only adds curves with a
            # `name` to a legend that already exists.
            self.plot_item.addLegend(offset=(10, 10))
            _style_legend(self.plot_item)

        self.curves = []
        for member in self.channels:
            curve = self.plot_item.plot(
                pen=pg.mkPen(color=curve_color(), width=member.plot_line_width),
                name=member.display_name if len(self.channels) > 1 else None,
            )
            # NO `autoDownsample`: the data already arrives pre-compressed
            # via `_downsample_for_display()` - PyQtGraph's own,
            # view-range-dependent variant would needlessly run over the
            # (now small) point set again on every tick (see there).
            curve.setClipToView(True)
            self.curves.append(curve)
        self.curve = self.curves[0]
        self.plot_item.enableAutoRange(x=False)
        # See `Channel.plot_show_graph` - a normal Qt widget (not a
        # PyQtGraph `GraphicsLayout`), so `setVisible()` cleanly returns
        # the space to the value display next to it.
        self.plot_widget.setVisible(channel.plot_show_graph)

        row.addWidget(self.plot_widget, stretch=1)

        self._retheme()
        connect_theme_changed(self._retheme)

    def set_channels(self, channels: list[Channel]) -> None:
        """Points the window at the current Channel objects (same members,
        e.g. after the live view's channel list was replaced) - so
        geometry tracking and styling use the live objects."""
        self.channels = list(channels)
        self._channel = self.channels[0]

    def apply_value_visibility(self) -> None:
        """One readout line per channel with `plot_show_value`; the whole
        container only if at least one of them is on."""
        any_visible = False
        for line, member in zip(self._value_rows, self.channels):
            line.setVisible(member.plot_show_value)
            any_visible = any_visible or member.plot_show_value
        self._value_container.setVisible(any_visible)

    def _apply_number_width(self) -> None:
        """Adjusts the fixed width of the number labels to
        `Channel.plot_value_integer_digits` - separate from
        `_style_value_labels`, since a change in integer digits (unlike
        color/theme) affects the field width itself (see
        `LiveView._apply_display_settings_to_live_channels`). ONE width
        for all lines (the widest format), so the units line up."""
        font = QFont()
        font.setPointSize(_VALUE_NUMBER_POINT_SIZE)
        font.setBold(True)
        width = _number_field_width_px(
            font,
            max(c.plot_value_integer_digits for c in self.channels),
            max(c.plot_value_decimal_digits for c in self.channels),
        )
        for label in self.value_labels:
            label.setFixedWidth(width)

    def _style_value_labels(self) -> None:
        """Colors both the text AND background of the numbers/units -
        shared by `_retheme` (theme change) AND
        `LiveView._apply_channel_appearance` (channel display dialog
        changed live).

        The background is deliberately ALWAYS the window background
        color (`plot_container_background_color()`), NOT the individual
        channel color (`_channel_background_color`) - the latter only
        applies to the actual plot area itself (see
        `LiveView._rebuild_plots`).

        ALSO colors `self._value_container` and every line (not just the
        labels themselves): the gap that `QHBoxLayout.setSpacing()`
        leaves between the number and the unit belongs to the container,
        not the labels - without its own background color, a visible,
        differently colored bar would remain there."""
        foreground = plot_foreground_color()
        background = plot_container_background_color()
        self._value_container.setStyleSheet(f"background-color: {background};")
        for line, value_label, unit_label, color in zip(
            self._value_rows, self.value_labels, self.unit_labels, self.value_colors
        ):
            text_color = color or foreground
            line.setStyleSheet(f"background-color: {background};")
            value_label.setStyleSheet(
                f"color: {text_color}; background-color: {background}; "
                f"font-size: {_VALUE_NUMBER_POINT_SIZE}pt; font-weight: bold;"
            )
            unit_label.setStyleSheet(
                f"color: {text_color}; background-color: {background}; "
                f"font-size: {_VALUE_UNIT_POINT_SIZE}pt;"
            )

    def _retheme(self) -> None:
        style_plot_container(self.plot_widget)
        style_plot_item(self.plot_item)
        _style_legend(self.plot_item)
        self._style_value_labels()

    def moveEvent(self, event) -> None:  # noqa: N802 - Qt-API
        # Keeps `Channel.plot_popout_x/y` continuously in sync with the
        # actual window position (not just on close) - `self.channels`
        # are the same live references as in `LiveView` (see class
        # docstring), so changes are immediately visible there too. Is
        # later (e.g. on app exit, see `gui/main_window.py`) carried
        # over into the setup channel table and thus persisted. ALL
        # members of a group, since the geometry is a plot-wide setting
        # (see `PLOT_LEVEL_FIELDS`).
        super().moveEvent(event)
        for member in self.channels:
            member.plot_popout_x = self.x()
            member.plot_popout_y = self.y()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt-API
        super().resizeEvent(event)
        for member in self.channels:
            member.plot_popout_width = self.width()
            member.plot_popout_height = self.height()


class LiveView(QWidget):
    """Displays measurement data of a running measurement in real time.

    Signals:
        start_requested: The user clicked Play (live view only) or
            Record (with storage) - bool = `live_only`.
            `gui/main_window.py` then starts the measurement with the
            currently configured setup configuration and
            `MeasurementConfig.save_to_disk` set accordingly.
        stop_requested: The user clicked Stop (only clickable while
            something is actually running, see `set_start_enabled`).
            `gui/main_window.py` is responsible for actually stopping
            the measurement via the `MeasurementController`.
        trigger_fired: An armed threshold trigger (see
            `enter_armed_state`) has fired - `gui/main_window.py` then
            creates the StorageWriter (possibly retroactively, see
            `_on_trigger_fired`).
        trigger_arm_toggled: The user clicked the arm button (see
            `gui/setup_view.py::trigger_arm_toggled` - identical
            counterpart here in the live view, so both buttons can be
            operated at the same time).
    """

    start_requested = pyqtSignal(bool)  # live_only
    stop_requested = pyqtSignal()
    trigger_fired = pyqtSignal()
    trigger_arm_toggled = pyqtSignal(bool)
    # Number of channels placed side by side changed in the display
    # dialog - `gui/main_window.py` persists it (see
    # `config/settings.py::AppSettings.live_view_plot_columns`); the
    # live view itself has no access to the configuration manager.
    plot_columns_changed = pyqtSignal(int)

    def __init__(self, controller: MeasurementController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller

        self._reader_id: int | None = None
        self._channels: list[Channel] = []
        self._sample_rate_hz: float = 1.0
        self._rate_groups: list[RateGroup] = []
        # Native sample rate per channel index (Hz) - derived ONCE in
        # `start_display` from `rate_groups` (not recomputed per tick).
        # For most channels == `self._sample_rate_hz` (the same fast
        # rate group); only a channel with a hardware-fixed, deviating
        # rate (e.g. NI9210 @ 14 S/s) gets its own, smaller value here -
        # drives the sweep buffer size/fill rate for exactly that
        # channel (see
        # `_channel_display_capacity`/`_write_to_display_buffer`).
        self._channel_native_rates: dict[int, float] = {}
        self._display_window_seconds = _DEFAULT_DISPLAY_WINDOW_SECONDS

        # Sweep display buffer for the CURRENT sweep (oscilloscope-style:
        # the curve draws left to right through the time window; a new
        # sweep starts at x=0 at the right edge, the old curve
        # disappears entirely). `_channel_buffer_positions` is also the
        # number of valid samples in the current sweep per channel (see
        # `_write_to_display_buffer`/`_get_channel_display_view`).
        self._display_buffer: np.ndarray | None = None
        self._display_capacity_samples: int = 0
        self._buffer_write_pos: int = 0
        self._channel_buffer_positions: dict[int, int] = {}
        # Cumulative count of raw (tick-rate-clocked) rows seen in total
        # for this channel since measurement start - NOT reset on every
        # sweep wraparound (unlike `_channel_buffer_positions`), see
        # `_write_to_display_buffer`: only needed for channels with
        # their own, slower native rate, to independently replicate the
        # same time-/tick-based due() counting as
        # `core/rate_merge.py::RateMerger` (deliberately NOT value-based
        # - a real but coincidentally unchanged 14-S/s sample, e.g. from
        # a stable thermocouple reading, must not be mistaken for a ZOH
        # repeat, otherwise the sweep time axis would drift apart from
        # the real measurement time).
        self._channel_total_ticks_seen: dict[int, int] = {}
        # Absolute measurement time (seconds since measurement start) at
        # which the CURRENT sweep of the respective channel started -
        # the axis label should show the real measurement time (e.g.
        # "40-45s" instead of always "0-5s"), even though the sweep
        # itself keeps resetting on every cycle. Per channel, since the
        # window length (`Channel.plot_time_window_seconds`) can differ
        # per channel and the sweeps therefore end independently of each
        # other (see `_write_to_display_buffer`).
        self._channel_cycle_starts: dict[int, float] = {}
        # Last `_channel_cycle_starts` value applied to the plots per
        # channel (see `_on_timer_tick`) - the X range is only reset on
        # an actual cycle change, not on every tick.
        self._channel_x_range_applied: dict[int, float | None] = {}
        # Incremental time axis/envelope/value range per channel (see
        # `_SweepCache`) - cleared wherever the sweep state is reset.
        self._sweep_caches: dict[int, _SweepCache] = {}

        # Per-channel display (curve color, background, Y range,
        # autoscaling behavior) lives directly on the `Channel` objects
        # themselves (`plot_color`/`plot_background`/`plot_y_min`/
        # `plot_y_max`/`plot_autoscale`, see `data/models.py`) - this way
        # every `Channel` automatically carries its display along (also
        # when saving/loading the configuration), without the live view
        # needing its own, separately maintained mapping dicts. See
        # `open_channel_display_dialog`, menu item in
        # `gui/main_window.py::_build_menu`.

        # Per channel, whether the Y axis is CURRENTLY (this tick) in
        # autoscale mode or using the fixed range - only to avoid
        # unnecessary `setYRange`/`enableAutoRange` calls when the
        # effective mode hasn't changed (see `_apply_channel_y_range`).
        self._channel_y_auto_active: dict[tuple[str, str], bool] = {}

        self._plot_widget = pg.GraphicsLayoutWidget()
        # One entry per PLOT of the main grid: `self._plot_items[p]` shows
        # the channels `self._plot_slots[p].indices` (one, or several for
        # a shared plot, see `Channel.plot_number`/`_plot_slots`).
        self._plot_items: list = []
        self._plot_slots: list[_PlotSlot] = []
        # One entry per CURVE (= per displayed channel):
        # `self._curves[i]`/`self._value_labels[i]` belong to channel
        # `self._channels[self._curve_channel_indices[i]]` and sit in plot
        # `self._plot_items[self._curve_plot_positions[i]]` - NO LONGER
        # necessarily `self._channels[i]`, since invisible channels
        # (`Channel.plot_visible=False`) get no curve and grouped
        # channels share a plot (see `_rebuild_plots`).
        self._curves: list = []
        self._curve_channel_indices: list[int] = []
        self._curve_plot_positions: list[int] = []
        # Large, current value display next to each subplot in the main
        # grid (see `_make_value_box`/`_rebuild_plots`/`_on_timer_tick`)
        # - own windows (`ChannelPopoutWindow`) have their own,
        # identically named attributes on the window instance itself.
        # `_value_boxes[i]`/`_value_unit_boxes[i]` are the `ViewBox`es
        # (background color/visibility), `_value_labels[i]`/
        # `_value_unit_labels[i]` are the `TextItem`s centered inside
        # them (text content) - `_value_unit_labels[i]` is set ONLY at
        # build time and never rewritten per tick (see
        # `_VALUE_UNIT_WIDTH`).
        self._value_boxes: list = []
        self._value_labels: list = []
        self._value_unit_boxes: list = []
        self._value_unit_labels: list = []

        # Averaging accumulator for the numeric readout, per CHANNEL
        # INDEX (not per subplot position): main grid and own window show
        # the same channel and must therefore show the same number at the
        # same moment. Between two refreshes every incoming sample is
        # summed here; on refresh the mean is displayed and the
        # accumulator reset - see `Channel.plot_value_refresh_hz` and
        # `_update_value_readouts`.
        self._value_sum: dict[int, float] = {}
        self._value_count: dict[int, int] = {}
        # Monotonic deadline per channel index for the next refresh.
        self._value_next_refresh_s: dict[int, float] = {}

        # Own windows of individual channels (see `ChannelPopoutWindow`,
        # `_on_popout_requested`), keyed by `_channel_display_key()`
        # (NOT `hardware_channel` alone - see there) - independent of
        # `plot_visible`: a channel can be hidden in the main grid AND
        # still visible in its own window. Its own autoscale state cache
        # (see `_apply_channel_y_range`), so that a channel's popout and
        # its main-grid subplot don't "cache away" each other's scaling.
        self._popout_windows: dict[tuple[str, str], ChannelPopoutWindow] = {}
        self._popout_y_auto_active: dict[tuple[str, str], bool] = {}
        # Which channels each own window shows (same key as
        # `_popout_windows`, = the lead channel's key, see `_PlotSlot`).
        self._popout_slots: dict[tuple[str, str], _PlotSlot] = {}
        # `id()`s of windows the view itself is closing (group changed,
        # popout switched off) - their `destroyed` must not be mistaken
        # for the user closing the window (see `_on_popout_window_closed`).
        self._popout_programmatic_closes: set[int] = set()

        # Channels per row in the main grid (see `_rebuild_plots`). Set from
        # the persisted setting at startup, see `set_plot_columns`.
        self._plot_columns = 1

        # StorageWriter of the running measurement (None for "live view
        # only" AND during the armed phase of a threshold/serial
        # trigger, before it has fired - see `attach_storage_writer`).
        self._storage_writer: StorageWriter | None = None

        # State for automatic measurement triggers (see
        # `data/models.py::TriggerConfig`, `enter_armed_state`). Hardware
        # acquisition + display are already running during the armed
        # phase, only the StorageWriter is still missing (see
        # `gui/main_window.py::_on_start_measurement`). Start AND stop
        # are independently configurable (see `TriggerConfig`) - hence
        # separate channel indices/edge detectors for both sides.
        self._trigger_config: TriggerConfig | None = None
        self._armed: bool = False
        self._start_trigger_channel_index: int | None = None
        self._stop_trigger_channel_index: int | None = None
        # None = no tick observed yet since the respective reset point -
        # prevents an immediate fire if the channel is already beyond
        # the threshold at that point in time (see
        # `_check_threshold_trigger`/`_check_stop_threshold_trigger`).
        # The start side is reset in `start_display()`, the stop side
        # additionally in `mark_recording_started()` (recording can
        # actually start LATER than `start_display()` for a
        # series/manual start).
        self._start_trigger_last_condition: bool | None = None
        self._stop_trigger_last_condition: bool | None = None
        # Zero point for the recording limit (see
        # `data/models.py::MeasurementConfig.is_recording_limit_reached`)
        # - for triggered measurements NOT the start of hardware
        # acquisition (that would be the arm time), but the actual
        # trigger time (see `mark_recording_started`). Stays 0 for a
        # manual start, i.e. unchanged behavior.
        self._recording_baseline_samples: int = 0

        self._timer = QTimer(self)
        # PreciseTimer instead of Qt's default (CoarseTimer, aligned to
        # Windows' ~15.6ms system tick, +- deviation possible) - at such
        # a short interval (see `_UI_UPDATE_INTERVAL_MS`) the coarse
        # default resolution would otherwise show up as additional
        # timing jitter.
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(_UI_UPDATE_INTERVAL_MS)
        self._timer.timeout.connect(self._on_timer_tick)

        self._storage_timer = QTimer(self)
        self._storage_timer.setInterval(_STORAGE_UPDATE_INTERVAL_MS)
        self._storage_timer.timeout.connect(self._on_storage_timer_tick)

        self._build_ui()
        style_plot_container(self._plot_widget)
        connect_language_changed(self.retranslate_ui)
        connect_theme_changed(self.retheme_plots)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 0, 9, 9)

        info_row = QHBoxLayout()
        info_row.setContentsMargins(0, 0, 0, 8)
        info_row.setSpacing(10)
        self._duration_label = QLabel(t("duration_value", value="-"))
        self._sample_rate_label = QLabel(t("sample_rate_value", value="-"))

        # Arm button: identical counterpart to
        # `gui/setup_view.py::_trigger_arm_button` (same style, same
        # meaning) - to the left of the start button, so both buttons
        # can be operated from here without switching to the setup view.
        # Only visible if a trigger is actually configured (see
        # `set_trigger_arm_available`).
        self._trigger_arm_button = QPushButton()
        self._trigger_arm_button.setCheckable(True)
        self._trigger_arm_button.setIconSize(QSize(24, 24))
        self._trigger_arm_button.setStyleSheet(trigger_arm_button_style())
        self._trigger_arm_button.setVisible(False)
        self._trigger_arm_button.toggled.connect(self._on_trigger_arm_button_toggled)

        # Play (green icon, live view only)/Record (red circle icon,
        # with storage)/Stop - identical counterpart to
        # `gui/setup_view.py` (see there for the rationale behind the
        # three-button design instead of the former single start button
        # + "live view only" checkbox). `ACTION_BUTTON_STYLE`
        # deliberately sets NO `background-color` in the normal state
        # (unlike `_trigger_arm_button`) - they normally follow the
        # QPalette/current theme, only the play/record icon color is
        # fixed (see `_retheme_action_button_icons`); only hover/press
        # get a subtle palette-based effect.
        self._play_button = QPushButton()
        self._play_button.setIconSize(QSize(24, 24))
        self._play_button.setStyleSheet(action_button_style())
        self._play_button.clicked.connect(lambda: self.start_requested.emit(True))

        self._record_button = QPushButton()
        self._record_button.setIconSize(QSize(24, 24))
        self._record_button.setStyleSheet(action_button_style())
        self._record_button.clicked.connect(lambda: self.start_requested.emit(False))

        self._stop_button = QPushButton()
        self._stop_button.setIconSize(QSize(24, 24))
        self._stop_button.setStyleSheet(action_button_style())
        self._stop_button.setEnabled(False)
        self._stop_button.clicked.connect(self.stop_requested.emit)

        self._retheme_action_button_icons()
        self._update_action_button_labels()
        # ONLY AFTER setting icon/stylesheet: `_set_trigger_arm_button_text()`
        # fixes the button width via `fix_toggle_button_width()` based on
        # `sizeHint()`, which needs the icon AND stylesheet padding to
        # measure correctly.
        self._set_trigger_arm_button_text()

        # Play/Record/Stop (+ arm button) on the left, the running
        # readouts (duration/sample rate) right next to them - previously
        # to the right of the stretch, now grouped with the buttons on
        # the left.
        info_row.addWidget(self._trigger_arm_button, 0, Qt.AlignmentFlag.AlignVCenter)
        info_row.addWidget(self._play_button, 0, Qt.AlignmentFlag.AlignVCenter)
        info_row.addWidget(self._record_button, 0, Qt.AlignmentFlag.AlignVCenter)
        info_row.addWidget(self._stop_button, 0, Qt.AlignmentFlag.AlignVCenter)
        info_row.addWidget(self._duration_label, 0, Qt.AlignmentFlag.AlignVCenter)
        info_row.addWidget(self._sample_rate_label, 0, Qt.AlignmentFlag.AlignVCenter)
        info_row.addStretch(1)
        layout.addLayout(info_row)

        # "Armed, waiting for trigger" banner (see `enter_armed_state`) -
        # clearly highlighted, hidden by default. The existing stop
        # button continues to serve unchanged as the cancel function
        # during this time (see `gui/main_window.py::_on_stop_measurement`).
        self._armed_banner = QLabel()
        self._armed_banner.setWordWrap(True)
        self._armed_banner.setStyleSheet(
            "QLabel { background-color: #fd7e14; color: #1a1a1a; padding: 6px 10px;"
            " border-radius: 4px; font-weight: 600; }"
        )
        self._armed_banner.setVisible(False)
        layout.addWidget(self._armed_banner)

        layout.addWidget(self._plot_widget, stretch=1)

        # Buffer utilization of the storage writer (write backlog vs. the DAQ thread).
        self._storage_group = QGroupBox(t("storage_buffer_group"))
        storage_layout = QHBoxLayout(self._storage_group)
        self._storage_progress = QProgressBar()
        self._storage_progress.setRange(0, 100)
        self._storage_progress.setTextVisible(True)
        self._storage_progress.setFormat("%p%")
        self._storage_detail_label = QLabel("-")
        storage_layout.addWidget(self._storage_progress, stretch=1)
        storage_layout.addWidget(self._storage_detail_label)
        layout.addWidget(self._storage_group)
        self._storage_group.setVisible(False)

    # ------------------------------------------------------------------ #
    # Public API (called from main_window.py)
    # ------------------------------------------------------------------ #

    def preview_channels(self, channels: list[Channel]) -> None:
        """Builds the plot layout from the channels configured in setup
        already, BEFORE a measurement is started - this way the live
        view doesn't have to stay empty until a measurement is actually
        started.

        Only takes effect while no measurement is running
        (`self._reader_id is None`); a running measurement must not have
        its own, actually acquired plots replaced by a preview of the
        (possibly since-changed) setup configuration.

        Only rebuilds if the channel configuration has actually changed
        compared to the last one displayed (`Channel` is a `@dataclass`,
        so list comparison is element-wise by content) - otherwise every
        click on the live view tile would needlessly rebuild the plots,
        even if nothing changed in the setup.
        """
        if self._reader_id is not None or channels == self._channels:
            return
        sync_plot_groups(channels)
        self._channels = channels
        self._rebuild_plots()

    def start_display(
        self,
        channels: list[Channel],
        sample_rate_hz: float,
        storage_writer: StorageWriter | None = None,
        trigger_config: TriggerConfig | None = None,
        rate_groups: list[RateGroup] | None = None,
    ) -> None:
        """Starts the live display for a newly started measurement.

        Registers its own, independent ring buffer reader (see
        `MeasurementController.register_reader`) - the live view is
        allowed to lose/skip samples without affecting the storage
        writer (see `core/ringbuffer.py`).

        ALWAYS called, even on a manual start (not just for a
        threshold/serial start trigger) - the stop trigger (see
        `TriggerConfig.stop`) must be monitorable independent of how the
        measurement was started. Resolves BOTH channel indices here
        (start and stop side) and resets both edge detectors.

        Args:
            sample_rate_hz: The ACTUAL tick rate of the ring buffer
                (= fastest rate group, see
                `data/models.py::resolve_rate_groups`) - NOT necessarily
                the target rate configured by the user. Drives the
                entire display buffer/X axis math below.
            storage_writer: The `StorageWriter` of the running
                measurement, if storing. `None` for "live view only" -
                in that case the storage buffer display stays hidden.
            trigger_config: Current start/stop trigger configuration
                (see `data/models.py::TriggerConfig`). `None`
                corresponds to an empty configuration (no trigger).
            rate_groups: Resolved rate groups of this measurement (see
                `resolve_rate_groups`). Drives two independent things:
                (1) still the textual display in the rate label
                (`_sample_rate_label`, see
                `_format_sample_rate_label_value`) and (2) the native
                rate per channel (see `_channel_native_rates`, built
                directly below) - a channel with a fixed rate that
                deviates from the tick rate (e.g. NI9210 @ 14 S/s) gets
                its sweep display buffer sized/filled according to ITS
                OWN, real data rate instead of the much faster ZOH tick
                rate `sample_rate_hz` (see `_write_to_display_buffer` -
                otherwise the downsampling would cost a multiple per
                tick). `None`/a single group -> all channels get
                `sample_rate_hz` as their native rate, unchanged
                behavior as before.
        """
        sync_plot_groups(channels)
        self._channels = channels
        self._sample_rate_hz = sample_rate_hz
        self._rate_groups = rate_groups or []
        # Native rate per channel index, derived ONCE here (not
        # recomputed per tick, see `_write_to_display_buffer`) - exactly
        # the same construction as
        # `data/metadata.py::build_measurement_metadata`'s
        # `rate_by_hw_channel` (key deliberately `hardware_channel`, NOT
        # object identity/`==` on `Channel` - it's a mutable dataclass
        # whose fields get changed live elsewhere). Fallback
        # `sample_rate_hz`: channels without their own fixed rate (the
        # normal case) run at the tick rate itself.
        rate_by_hw_channel = {
            channel.hardware_channel: group.resolved_sample_rate_hz
            for group in self._rate_groups
            for channel in group.channels
        }
        self._channel_native_rates = {
            index: rate_by_hw_channel.get(channel.hardware_channel, sample_rate_hz)
            for index, channel in enumerate(channels)
        }
        self._reader_id = self._controller.register_reader()

        self._trigger_config = trigger_config or TriggerConfig()
        self._start_trigger_channel_index = None
        self._stop_trigger_channel_index = None
        for index, channel in enumerate(channels):
            if channel.hardware_channel == self._trigger_config.start.threshold_channel_hardware_id:
                self._start_trigger_channel_index = index
            if channel.hardware_channel == self._trigger_config.stop.threshold_channel_hardware_id:
                self._stop_trigger_channel_index = index
        self._start_trigger_last_condition = None
        self._stop_trigger_last_condition = None

        self._rebuild_plots()
        self._ensure_display_buffer(len(channels))
        # Explicitly reset (not just implicitly via a shape change in
        # `_ensure_display_buffer`): otherwise, with the same channel
        # count/sample rate as the previous measurement, the old write
        # position (and thus a remnant of old measurement data) would
        # visibly carry over into the new sweep.
        self._buffer_write_pos = 0
        self._channel_buffer_positions = {index: 0 for index in range(len(channels))}
        self._channel_cycle_starts = {index: 0.0 for index in range(len(channels))}
        self._channel_x_range_applied = {index: None for index in range(len(channels))}
        self._sweep_caches = {}
        self._channel_total_ticks_seen = {index: 0 for index in range(len(channels))}
        self._reset_value_readout_state()
        self._timer.start()

        self.attach_storage_writer(storage_writer)

        logger.info(
            "Live View gestartet für %d Kanäle bei %.1f Hz", len(channels), sample_rate_hz
        )

    def _format_sample_rate_label_value(self) -> str:
        """Builds the display text for `_sample_rate_label`.

        Normal case (`self._rate_groups` empty or exactly one group):
        just the plain rate, as before. With multiple groups (a current
        rate conflict, e.g. NI9210 + a faster module), also shows the
        rate(s) the slower group(s) are actually running at - unlike
        DIAdem/NI-MAX, the deviation from the target rate stays visible
        here instead of being silently clipped.
        """
        if len(self._rate_groups) <= 1:
            return f"{self._sample_rate_hz:.1f} Hz"

        fast_group = max(self._rate_groups, key=lambda g: g.resolved_sample_rate_hz)
        extra_parts = []
        for group in self._rate_groups:
            if group is fast_group:
                continue
            module_names = "/".join(sorted({ch.module_type.value for ch in group.channels}))
            extra_parts.append(f"{module_names} @ {group.resolved_sample_rate_hz:.1f} Hz")
        return f"{fast_group.resolved_sample_rate_hz:.1f} Hz (+ {', '.join(extra_parts)})"

    def attach_storage_writer(self, storage_writer: StorageWriter | None) -> None:
        """Sets (or removes) the StorageWriter of the running measurement.

        On a manual start, `start_display()` calls this directly with the
        already-ready StorageWriter (or `None` for "live view only").
        With an automatic trigger (see `enter_armed_state`), NO
        StorageWriter exists yet during the armed phase -
        `gui/main_window.py::_on_trigger_fired` calls this method
        AFTERWARDS, once the trigger has actually fired.
        """
        self._storage_writer = storage_writer
        self._storage_group.setVisible(storage_writer is not None)
        if storage_writer is not None:
            self._on_storage_timer_tick()
            self._storage_timer.start()
        else:
            self._storage_timer.stop()

    def mark_recording_started(self, baseline_samples: int) -> None:
        """Sets the zero point for the recording limit to the actual
        start of recording (see `_recording_baseline_samples`,
        `_on_timer_tick`) and is the universal reset point for the
        stop-trigger edge detector (see `_check_stop_threshold_trigger`)
        - a stop condition that's already satisfied right at the actual
        start of recording must not fire immediately (incorrectly).

        Called with `0` on a manual start (zero point = start of
        acquisition, unchanged behavior) - MUST also be called there,
        otherwise the stop edge detector would never get reset. On a
        start trigger, `gui/main_window.py::_on_trigger_fired` calls this
        with the sample count at which the trigger actually fired.
        """
        self._recording_baseline_samples = baseline_samples
        self._stop_trigger_last_condition = None

    def enter_armed_state(self) -> None:
        """Puts the live view into the "armed, waiting for trigger" state.

        Channel resolution and edge-detector reset are already done by
        `start_display()` (ALWAYS called, even on a manual start) - only
        state + banner remain here. Hardware acquisition and display are
        already running at this point (see
        `gui/main_window.py::_on_start_measurement`) - only the
        StorageWriter is still missing. With a threshold trigger,
        `_on_timer_tick`/`_check_threshold_trigger` checks the configured
        channel on every tick from now on; with a serial trigger, the
        actual monitoring happens externally (see
        `gui/serial_trigger.py::SerialTriggerListener`), this state only
        controls the banner display here.
        """
        self._armed = True
        self._update_armed_banner()

    def exit_armed_state(self) -> None:
        """Ends the "armed, waiting for trigger" state (trigger fired OR
        measurement canceled in the meantime). Idempotent."""
        self._armed = False
        self._armed_banner.setVisible(False)

    def _update_armed_banner(self) -> None:
        if self._trigger_config is None:
            return
        start = self._trigger_config.start
        if start.kind == TriggerKind.SERIAL:
            text = t("armed_waiting_serial", port=start.serial_port)
        else:
            channel_name = (
                self._channels[self._start_trigger_channel_index].display_name
                if self._start_trigger_channel_index is not None
                else start.threshold_channel_hardware_id
            )
            text = t(
                "armed_waiting_threshold",
                channel=channel_name,
                threshold=start.threshold_value,
            )
        self._armed_banner.setText(text)
        self._armed_banner.setVisible(True)

    @staticmethod
    def _evaluate_threshold_condition(latest: float, condition) -> bool:
        """Evaluates a `TriggerCondition` (threshold kind) for a single
        reading - shared by the start and stop checks."""
        threshold = condition.threshold_value
        direction = condition.threshold_direction
        if direction == TriggerDirection.RISES_ABOVE:
            return latest > threshold
        if direction == TriggerDirection.FALLS_BELOW:
            return latest < threshold
        return abs(latest) > threshold  # ABS_EXCEEDS

    def _check_threshold_trigger(self, scaled: np.ndarray) -> None:
        """Checks the configured start channel against the threshold on
        every tick (see `enter_armed_state`) and emits `trigger_fired` as
        soon as the condition occurs EDGE-LIKE (i.e. on the transition
        from "not met" to "met", not on every tick while it remains met).
        `_start_trigger_last_condition` deliberately starts at `None` on
        every arming, so a channel that's already past the threshold at
        the moment of arming does NOT fire immediately - the user must
        see an actual crossing, like with an oscilloscope trigger.

        Deliberate simplification: only the last sample value of each
        tick is checked (~15ms granularity), not the whole data block -
        given the required "~5s" pre-roll tolerance, that's immaterial,
        the same precision level as the existing recording limit (see
        `_on_timer_tick`).
        """
        if (
            not self._armed
            or self._trigger_config is None
            or self._trigger_config.start.kind != TriggerKind.THRESHOLD
            or self._start_trigger_channel_index is None
        ):
            return
        values = scaled[self._start_trigger_channel_index]
        if values.size == 0:
            return
        latest = float(values[-1])
        condition = self._evaluate_threshold_condition(latest, self._trigger_config.start)

        fired = self._start_trigger_last_condition is False and condition is True
        self._start_trigger_last_condition = condition
        if fired:
            self._armed = False
            self._armed_banner.setVisible(False)
            self.trigger_fired.emit()

    def _check_stop_threshold_trigger(self, scaled: np.ndarray) -> None:
        """Checks the configured stop channel against the threshold on
        every tick, as long as recording is actually happening (`not
        self._armed`) - same edge logic as `_check_threshold_trigger`,
        but fires via `stop_requested` (the same path as the recording
        limit and the manual stop button), instead of via
        `trigger_fired`. `_stop_trigger_last_condition` is reset by
        `mark_recording_started()` - the point at which recording
        actually begins (not necessarily `start_display()`, e.g. with a
        start trigger).
        """
        if (
            self._armed
            or self._trigger_config is None
            or self._trigger_config.stop.kind != TriggerKind.THRESHOLD
            or self._stop_trigger_channel_index is None
        ):
            return
        values = scaled[self._stop_trigger_channel_index]
        if values.size == 0:
            return
        latest = float(values[-1])
        condition = self._evaluate_threshold_condition(latest, self._trigger_config.stop)

        fired = self._stop_trigger_last_condition is False and condition is True
        self._stop_trigger_last_condition = condition
        if fired:
            self.stop_requested.emit()

    def stop_display(self) -> None:
        """Ends the live display (after the measurement ends)."""
        self._timer.stop()
        self._storage_timer.stop()
        self.exit_armed_state()
        if self._reader_id is not None:
            self._controller.unregister_reader(self._reader_id)
            self._reader_id = None
        self._rate_groups = []
        self._channel_native_rates = {}
        logger.info("Live View gestoppt")

    def retranslate_ui(self) -> None:
        """Updates all static texts after a language change."""
        self._update_action_button_labels()
        self._set_trigger_arm_button_text()
        self._storage_group.setTitle(t("storage_buffer_group"))

        # The X axis title is the ONLY translatable text on a plot -
        # plot title, Y axis title and unit are all user data (channel
        # name/unit, see `_channel_axis_label`).
        #
        # Deliberately WITHOUT `**_axis_label_style()`: `setLabel()`
        # replaces `labelStyle` wholesale ONLY when style kwargs are
        # passed (`if kwargs: self.labelStyle = kwargs`). Omitting them
        # keeps both the enlarged font size AND the color that
        # `style_plot_item()` put there at creation - passing them would
        # drop the color (see `_axis_label_style`).
        for pos, plot_item in enumerate(self._plot_items):
            # `units=` NOT used (see `ChannelPopoutWindow.__init__`) - time
            # unit consistently in square brackets everywhere.
            plot_item.setLabel("bottom", f"{t('axis_time')} [s]")
            # `setLabel()` re-shows the title unconditionally (see
            # `_apply_axis_label_visibility`) - without this, switching
            # the language would bring back an X title turned off per
            # channel.
            if pos < len(self._plot_slots):
                channel = self._channels[self._plot_slots[pos].lead]
                _apply_axis_label_visibility(plot_item, channel)

        # Own windows (see `ChannelPopoutWindow`) are NOT part of
        # `self._plot_items` - without this loop their X title would keep
        # the language the window was opened in until it is closed and
        # reopened, while the main grid next to it already switched.
        for key, window in self._popout_windows.items():
            window.plot_item.setLabel("bottom", f"{t('axis_time')} [s]")
            channel = self._find_channel_by_key(key)
            if channel is not None:
                _apply_axis_label_visibility(window.plot_item, channel)

        # Running duration/sample rate correct themselves on the next
        # timer tick - only the idle placeholder would otherwise stay
        # stuck in the old language permanently.
        if self._reader_id is None:
            self._duration_label.setText(t("duration_value", value="-"))
            self._sample_rate_label.setText(t("sample_rate_value", value="-"))

    def retheme_plots(self) -> None:
        """Recolors plot background/axes/curves after a theme change.

        PyQtGraph widgets don't automatically follow the `QApplication`
        palette (see `gui/theme.py`) - already-existing plots therefore
        have to be explicitly recolored.
        """
        style_plot_container(self._plot_widget)
        self._retheme_action_button_icons()
        for pos, plot_item in enumerate(self._plot_items):
            style_plot_item(plot_item)
            _style_legend(plot_item)
            channel = self._channels[self._plot_slots[pos].lead]
            plot_item.getViewBox().setBackgroundColor(_channel_background_color(channel))
        for pos in range(len(self._curves)):
            # Boxes/labels only exist if `channel.plot_show_value` is set
            # (see `_rebuild_plots`) - otherwise the plot reclaims their
            # column space instead of just occupying invisible empty
            # space. Deliberately ALWAYS the window background, NOT the
            # individual channel color (see `_rebuild_plots` for the
            # reasoning) - the channel's own color only applies to the
            # plot area itself. Text colors are (re)applied by
            # `_apply_channel_appearance()` below (curve color in a group).
            if self._value_boxes[pos] is not None:
                self._value_boxes[pos].setBackgroundColor(plot_container_background_color())
            if self._value_unit_boxes[pos] is not None:
                self._value_unit_boxes[pos].setBackgroundColor(plot_container_background_color())
            if self._value_labels[pos] is not None:
                self._value_labels[pos].setColor(plot_foreground_color())
            if self._value_unit_labels[pos] is not None:
                self._value_unit_labels[pos].setColor(plot_foreground_color())
        # Do NOT unconditionally reset curve color/background to the
        # theme default - individually configured channel colors (see
        # `open_channel_display_dialog`) should survive a theme change;
        # `_apply_channel_appearance()` already applies the (now new)
        # theme default for channels WITHOUT their own color anyway.
        self._apply_channel_appearance()

    def _retheme_action_button_icons(self) -> None:
        # Play/record have fixed, theme-independent icon colors (see
        # `gui/theme.py::PLAY_ICON_COLOR`/`RECORD_ICON_COLOR`). Stop AND
        # the arm button no longer have a hardcoded background (see
        # `ACTION_BUTTON_STYLE`/`TRIGGER_ARM_BUTTON_STYLE`) and therefore
        # stay with the normal theme-dependent `nav_icon_color()` (no
        # `color=` passed).
        self._play_button.setIcon(QIcon(draw_play_icon(24, y_offset=0.6, color=PLAY_ICON_COLOR)))
        self._record_button.setIcon(
            QIcon(draw_record_icon(24, y_offset=0.6, color=RECORD_ICON_COLOR))
        )
        self._stop_button.setIcon(QIcon(draw_stop_icon(24, y_offset=0.6)))
        self._trigger_arm_button.setIcon(QIcon(draw_trigger_icon(24, y_offset=0.6)))
        # `ACTION_BUTTON_STYLE`/`TRIGGER_ARM_BUTTON_STYLE` reference
        # `palette(...)` - without a manual unpolish()/polish(),
        # border/background visibly stay stuck in the old theme after a
        # live theme change (same finding as with the navigation tiles,
        # see `gui/main_window.py::_retheme_nav_icons`).
        for button in (
            self._play_button,
            self._record_button,
            self._stop_button,
            self._trigger_arm_button,
        ):
            repolish(button)

    def _update_action_button_labels(self) -> None:
        # Short button text (see `play_button_label`/`record_button_label`/
        # `stop_button_label`) AND a more detailed tooltip (existing
        # `live_only`/`start_measurement`/`stop_measurement` keys).
        self._play_button.setText(f"  {t('play_button_label')}")
        self._play_button.setToolTip(t("live_only"))
        self._record_button.setText(f"  {t('record_button_label')}")
        self._record_button.setToolTip(t("start_measurement"))
        self._stop_button.setText(f"  {t('stop_button_label')}")
        self._stop_button.setToolTip(t("stop_measurement"))

    def _set_trigger_arm_button_text(self) -> None:
        key = "trigger_disarm_button" if self._trigger_arm_button.isChecked() else "trigger_arm_button"
        self._trigger_arm_button.setText(f"  {t(key)}")
        fix_toggle_button_width(
            self._trigger_arm_button,
            f"  {t('trigger_arm_button')}",
            f"  {t('trigger_disarm_button')}",
        )

    def _on_trigger_arm_button_toggled(self, checked: bool) -> None:
        self._set_trigger_arm_button_text()
        self.trigger_arm_toggled.emit(checked)

    def set_trigger_arm_available(self, available: bool) -> None:
        """See `gui/setup_view.py::SetupView.set_trigger_arm_available`
        - identical counterpart here in the live view."""
        self._trigger_arm_button.setVisible(available)
        if not available and self._trigger_arm_button.isChecked():
            self.set_trigger_armed(False)

    def set_trigger_armed(self, armed: bool) -> None:
        """See `gui/setup_view.py::SetupView.set_trigger_armed` -
        identical counterpart here in the live view."""
        self._trigger_arm_button.blockSignals(True)
        self._trigger_arm_button.setChecked(armed)
        self._trigger_arm_button.blockSignals(False)
        self._set_trigger_arm_button_text()

    def set_start_enabled(self, enabled: bool) -> None:
        """See `gui/setup_view.py::SetupView.set_start_enabled` -
        identical counterpart here: stop ALWAYS follows the inverse
        state."""
        self._play_button.setEnabled(enabled)
        self._record_button.setEnabled(enabled)
        self._stop_button.setEnabled(not enabled)
        # See `gui/setup_view.py::SetupView.set_start_enabled` - same
        # exception: while its own active cycle is running, the arm
        # button always stays clickable (so "disarming" is possible at
        # any time).
        if not self._trigger_arm_button.isChecked():
            self._trigger_arm_button.setEnabled(enabled)

    def set_plot_columns(self, columns: int) -> None:
        """Sets how many channels the main grid places side by side.

        Rebuilds the plots only on an actual change - `_rebuild_plots()`
        discards and recreates every subplot, which during a running
        measurement is visible as a brief flicker.
        """
        columns = max(1, min(_MAX_PLOT_COLUMNS, int(columns)))
        if columns == self._plot_columns:
            return
        self._plot_columns = columns
        self._rebuild_plots()
        self._apply_channel_appearance()
        self._apply_y_range_mode()

    def plot_columns(self) -> int:
        """Current channels-per-row setting (see `set_plot_columns`)."""
        return self._plot_columns

    def open_channel_display_dialog(
        self, channels: list[Channel] | None = None
    ) -> dict[tuple[str, str], dict] | None:
        """Opens the dialog for curve color/background/Y range/
        autoscaling per channel.

        Called from the Options menu item -> "Configure Channel
        Display..." (see `gui/main_window.py::_build_menu`).

        Args:
            channels: Channels offered in the dialog (their current
                `plot_*` fields are the initial values, see
                `data/models.py::Channel`). `None` (default) uses the
                currently live-displayed channels (`self._channels`,
                only populated during a running measurement).
                `gui/main_window.py` instead passes the channels from the
                setup configuration, so the display can already be
                configured BEFORE the measurement starts.

        Returns:
            The values set in the dialog per channel (see
            `ChannelDisplayDialog.results()`), or `None` on cancel/no
            channels. `gui/main_window.py` passes the result on to
            `SetupView.apply_channel_display_settings()` so the values
            are preserved when the configuration is saved - the live
            view itself only knows its own `self._channels` (see
            `_apply_display_settings_to_live_channels`).
        """
        channels = channels if channels is not None else self._channels
        if not channels:
            QMessageBox.information(
                self, t("channel_display_dialog_title"), t("channel_display_no_channels")
            )
            return None
        dialog = ChannelDisplayDialog(
            channels,
            plot_background_color(),
            plot_foreground_color(),
            plot_columns=self._plot_columns,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        # Column count BEFORE the per-channel settings: it triggers a full
        # `_rebuild_plots()`, and applying the channel values first would
        # only have them thrown away and reapplied again.
        if dialog.column_count() != self._plot_columns:
            self.set_plot_columns(dialog.column_count())
            self.plot_columns_changed.emit(self._plot_columns)
        settings = dialog.results()
        self._apply_display_settings_to_live_channels(settings)
        return settings

    def _apply_display_settings_to_live_channels(
        self, settings: dict[tuple[str, str], dict]
    ) -> None:
        """Applies values set in the dialog to the CURRENTLY live-
        displayed channels (`self._channels`).

        Relevant if the dialog was opened with a different channel list
        (e.g. from the setup, see `open_channel_display_dialog`) while a
        measurement is currently running: the running display should
        update immediately, not only at the next measurement start.
        """
        if not self._channels:
            return
        changed = False
        visibility_changed = False
        time_window_changed = False
        time_windows_before = [c.plot_time_window_seconds for c in self._channels]
        integer_digits_changed = False
        refresh_rate_changed = False
        show_value_changed = False
        for channel in self._channels:
            values = settings.get(_channel_display_key(channel))
            if values is None:
                continue
            channel.plot_color = values.get("plot_color")
            channel.plot_background = values.get("plot_background")
            channel.plot_grid_color = values.get("plot_grid_color")
            channel.plot_y_min = values.get("plot_y_min")
            channel.plot_y_max = values.get("plot_y_max")
            channel.plot_autoscale = values.get("plot_autoscale", True)
            # Applied by `_apply_channel_appearance()` at the end of this
            # method (via `_apply_channel_curve_style`) - no rebuild
            # needed, showing/hiding an axis title only re-lays out the
            # plot item itself.
            channel.plot_show_x_label = values.get("plot_show_x_label", True)
            channel.plot_show_y_label = values.get("plot_show_y_label", True)
            channel.plot_show_grid = values.get("plot_show_grid", True)
            channel.plot_line_width = max(
                0.1, float(values.get("plot_line_width", 1.5))
            )
            new_time_window = max(
                0.1, float(values.get("plot_time_window_seconds", 5.0))
            )
            if new_time_window != channel.plot_time_window_seconds:
                time_window_changed = True
            channel.plot_time_window_seconds = new_time_window
            # Needs a rebuild (see below), not just a plain
            # `_apply_channel_appearance()` update: the number column in
            # the main grid has a FIXED width in the `GraphicsLayoutWidget`
            # (`setColumnFixedWidth`, see `_rebuild_plots()`) - just
            # `.setVisible()` on the box itself does NOT let the plot
            # reclaim the column, the column width has to be recomputed
            # for that.
            # Unlike `plot_show_value` above, `plot_show_graph` does NOT
            # need a full rebuild: a plain show/hide via
            # `_apply_channel_curve_style()`, which
            # `_apply_channel_appearance()` below (or at the end of this
            # method) always calls anyway.
            channel.plot_show_graph = values.get("plot_show_graph", True)
            new_show_value = values.get("plot_show_value", False)
            if new_show_value != channel.plot_show_value:
                show_value_changed = True
            channel.plot_show_value = new_show_value
            new_integer_digits = max(
                1, int(values.get("plot_value_integer_digits", 3))
            )
            new_decimal_digits = max(
                0, int(values.get("plot_value_decimal_digits", 3))
            )
            if (
                new_integer_digits != channel.plot_value_integer_digits
                or new_decimal_digits != channel.plot_value_decimal_digits
            ):
                integer_digits_changed = True
            channel.plot_value_integer_digits = new_integer_digits
            channel.plot_value_decimal_digits = new_decimal_digits
            new_refresh_hz = max(
                0.1, float(values.get("plot_value_refresh_hz", 30.0))
            )
            if new_refresh_hz != channel.plot_value_refresh_hz:
                refresh_rate_changed = True
            channel.plot_value_refresh_hz = new_refresh_hz
            new_visible = values.get("plot_visible", True)
            new_popout = values.get("plot_popout", False)
            if new_visible != channel.plot_visible or new_popout != channel.plot_popout:
                visibility_changed = True
            channel.plot_visible = new_visible
            channel.plot_popout = new_popout
            new_number = Channel._optional_plot_number(
                values.get("plot_number", channel.plot_number)
            )
            if new_number != channel.plot_number:
                visibility_changed = True
            channel.plot_number = new_number
            changed = True
        # Plot-wide settings identical within each group (see
        # `sync_plot_groups`) - may change a member's time window, which
        # is why that is compared only AFTER syncing.
        sync_plot_groups(self._channels)
        if [c.plot_time_window_seconds for c in self._channels] != time_windows_before:
            time_window_changed = True
        if refresh_rate_changed:
            # Otherwise a rate just lowered would keep the OLD, already
            # running deadline and the next refresh could come up to one
            # old interval late (see `_reset_value_readout_state`).
            self._reset_value_readout_state()
        if visibility_changed or time_window_changed or integer_digits_changed or show_value_changed:
            if time_window_changed:
                # The buffer is sized to the widest time window across
                # all channels (see `_ensure_display_buffer`) - if the
                # span is enlarged while a measurement is running, it
                # must be reallocated, otherwise `cap` in
                # `_write_to_display_buffer` would keep being capped to
                # the old, smaller capacity.
                self._ensure_display_buffer(len(self._channels))
            # Which channels get a subplot at all has changed - color/
            # range application for the main grid is part of
            # `_rebuild_plots()` and therefore doesn't need to happen
            # separately. Open own windows (see `ChannelPopoutWindow`) are
            # only touched by `_rebuild_plots()` on an actual visibility/
            # popout change - `integer_digits_changed` alone (window stays
            # open) would otherwise need NO field-width update, so it's
            # applied explicitly here.
            self._rebuild_plots()
            self._apply_channel_appearance()
        elif changed:
            self._apply_channel_appearance()
            self._apply_y_range_mode()

    def _reset_value_readout_state(self) -> None:
        """Clears the averaging accumulators and schedules the first
        refresh of every channel for the next tick.

        Called at measurement start and whenever the refresh rate is
        changed in the dialog - without it, a rate that was just lowered
        would keep the OLD deadline (up to the old interval too late),
        and a remnant of the previous measurement could still be part of
        the first mean shown.
        """
        self._value_sum = {index: 0.0 for index in range(len(self._channels))}
        self._value_count = {index: 0 for index in range(len(self._channels))}
        # 0.0 = due immediately, so the first reading appears on the very
        # next tick rather than only after one full interval.
        self._value_next_refresh_s = {index: 0.0 for index in range(len(self._channels))}

    def _update_value_readouts(self, scaled: np.ndarray) -> dict[int, float]:
        """Feeds the block just read into the per-channel accumulators
        and returns, for every channel whose refresh is due NOW, the mean
        over the elapsed interval.

        Channels not in the returned dict keep their currently displayed
        number - deliberately not rewritten with an interim value, that
        is the whole point of the setting (see
        `Channel.plot_value_refresh_hz`).

        The mean rather than the last sample: at 1000 S/s and 4 Hz, one
        interval covers ~250 readings: showing `values[-1]` would pick a
        single arbitrary one of them and keep jumping, just less often.
        """
        now = time.monotonic()
        due: dict[int, float] = {}
        for index in range(len(self._channels)):
            block = scaled[index]
            if block.size:
                self._value_sum[index] = self._value_sum.get(index, 0.0) + float(block.sum())
                self._value_count[index] = self._value_count.get(index, 0) + int(block.size)
            if now < self._value_next_refresh_s.get(index, 0.0):
                continue
            count = self._value_count.get(index, 0)
            if count:
                due[index] = self._value_sum[index] / count
                self._value_sum[index] = 0.0
                self._value_count[index] = 0
            # Deadline advanced even without new data: otherwise a
            # channel idle for a while would fire on every tick as soon
            # as data resumes, until it has caught up.
            #
            # Advanced by a fixed grid step instead of `now + interval`:
            # a refresh can only ever happen ON a tick (~66 Hz), so
            # `now` already carries the overshoot past the deadline, and
            # re-basing on it would accumulate that overshoot and
            # quantize the rate DOWN - a requested 30 Hz would really
            # run at ~22 Hz. Stepping the grid keeps the long-run
            # average at the requested rate.
            # Clamped on BOTH sides at the point of use, not just in
            # the dialog: `Channel.plot_value_refresh_hz` also arrives
            # from a stored configuration, which `Channel.from_dict`
            # only guards against zero/negative (a division by zero
            # here) - it cannot know the view's tick rate. An absurdly
            # high stored rate would otherwise be capped only
            # incidentally, by the re-base branch below; capping it here
            # makes "never faster than the view ticks" the stated rule.
            refresh_hz = min(
                _MAX_VALUE_REFRESH_HZ,
                max(0.1, self._channels[index].plot_value_refresh_hz),
            )
            interval = 1.0 / refresh_hz
            deadline = self._value_next_refresh_s.get(index, 0.0) + interval
            if deadline <= now:
                # More than a full interval behind - the view was
                # stopped, or the rate is faster than the tick rate.
                # Re-base instead of firing on every tick to catch up.
                deadline = now + interval
            self._value_next_refresh_s[index] = deadline
        return due

    def _find_channel_by_key(self, key: tuple[str, str]) -> Channel | None:
        """Finds a channel via `_channel_display_key()` - for popout-
        related lookups (see there)."""
        return next((c for c in self._channels if _channel_display_key(c) == key), None)

    def _open_popout_window(self, target: "_PlotSlot | Channel") -> None:
        """Opens a standalone window with the live plot of ONE plot slot
        (single channel or plot group, see `ChannelPopoutWindow`), or
        activates an already-open window for it instead of opening a
        second one. `target` may also be a single `Channel` (its slot is
        looked up)."""
        if isinstance(target, Channel):
            index = self._channel_index(target)
            slot = next(
                (s for s in _plot_slots(self._channels) if index in s.indices),
                _PlotSlot((index,)),
            )
        else:
            slot = target
        members = [self._channels[i] for i in slot.indices]
        channel = members[0]
        key = _channel_display_key(channel)
        existing = self._popout_windows.get(key)
        if existing is not None:
            existing.raise_()
            existing.activateWindow()
            return

        window = ChannelPopoutWindow(members, self)
        x_min = self._channel_cycle_starts.get(slot.lead, 0.0)
        window.plot_item.setXRange(
            x_min,
            x_min + channel.plot_time_window_seconds,
            padding=0,
        )
        self._apply_plot_style(window.plot_item, channel)
        for curve, index in zip(window.curves, slot.indices):
            self._apply_curve_style(curve, self._channels[index], index)
        self._apply_channel_y_range(window.plot_item, channel, None, self._popout_y_auto_active)
        # IMPORTANT: the closure holds `self` (LiveView) ONLY as a
        # `weakref`, not directly - otherwise a real reference cycle
        # forms (LiveView -> self._popout_windows[hw] -> window -> Qt/sip
        # connection registry -> this closure -> self). Such a cycle is
        # only resolved by the cyclic GC, not by normal refcounting - and
        # because that GC pass "clears out" the involved objects via
        # `tp_clear`, `destroyed` can fire in the middle of that cleanup
        # and find `self` as an already-cleared closure cell
        # (`NameError: cannot access free variable 'self'`) - reproducible
        # via `profile_live_tick.py` (creating/discarding several LiveView
        # instances in quick succession). With `weakref`, no cycle forms
        # at all, plain refcounting is enough to clean up. The window
        # itself only as its `id()`, for the same reason.
        view_ref = weakref.ref(self)
        window_id = id(window)

        def _on_window_destroyed(
            _obj=None, key=key, view_ref=view_ref, window_id=window_id
        ) -> None:
            view = view_ref()
            if view is not None:
                view._on_popout_window_closed(key, window_id)

        window.destroyed.connect(_on_window_destroyed)

        # Reuse the last known position/size (see `Channel.plot_popout_x`
        # etc.), if present AND still on a currently connected screen
        # (e.g. NOT on a second monitor that has since been unplugged) -
        # otherwise cascade-place relative to the main window as before.
        has_saved_geometry = (
            channel.plot_popout_x is not None
            and channel.plot_popout_y is not None
            and channel.plot_popout_width is not None
            and channel.plot_popout_height is not None
        )
        if has_saved_geometry and is_position_on_screen(
            channel.plot_popout_x + channel.plot_popout_width // 2,
            channel.plot_popout_y + channel.plot_popout_height // 2,
        ):
            window.setGeometry(
                channel.plot_popout_x,
                channel.plot_popout_y,
                channel.plot_popout_width,
                channel.plot_popout_height,
            )
        else:
            # Cascaded position instead of Qt's default placement: if
            # several channels are set to "own window" at once via the
            # dialog (one OK click triggers several `_open_popout_window()`
            # calls right after each other, see caller), Qt would
            # otherwise place the new windows exactly on top of each
            # other - only the most recently opened one would be visible,
            # the others sitting invisibly behind it and only "appearing"
            # once the topmost one is closed. Offset relative to the main
            # window's position, so the windows appear near it (not e.g.
            # on a different screen).
            main_window = self.window()
            base = main_window.pos() if main_window is not None else QPoint(80, 80)
            cascade_offset = 32 * len(self._popout_windows)
            window.move(base.x() + 60 + cascade_offset, base.y() + 60 + cascade_offset)
        self._popout_windows[key] = window
        self._popout_slots[key] = slot
        window.show()

    def _close_popout_window(self, key: tuple[str, str]) -> None:
        """Closes an own window ON BEHALF OF THE VIEW (popout switched off,
        group membership changed) - removed from the tracking right away,
        so a replacement can be opened under the same key immediately,
        and marked so its (queued) `destroyed` doesn't reset
        `plot_popout` like a close by the user would (see
        `_on_popout_window_closed`)."""
        window = self._popout_windows.pop(key, None)
        self._popout_slots.pop(key, None)
        self._popout_y_auto_active.pop(key, None)
        if window is not None:
            self._popout_programmatic_closes.add(id(window))
            window.close()

    def _channel_index(self, channel: Channel) -> int:
        """Index of `channel` in `self._channels` - by identity first
        (two channels can compare equal as dataclasses), by value as a
        fallback."""
        for index, candidate in enumerate(self._channels):
            if candidate is channel:
                return index
        return self._channels.index(channel)

    def get_open_popout_geometries(self) -> dict[tuple[str, str], tuple[int, int, int, int]]:
        """Returns position/size (x, y, width, height) of all currently
        open own windows - for `gui/main_window.py`, to adopt them into
        the setup channel table when the app closes/explicitly saves
        (see `Channel.plot_popout_x` etc.). Actually redundant with the
        continuously synchronized `Channel` fields (see
        `ChannelPopoutWindow.moveEvent`/`resizeEvent`), but deliberately
        reads directly from the window instead of the channel object -
        regardless of whether this channel is currently even part of the
        live-displayed `self._channels`."""
        return {
            key: (window.x(), window.y(), window.width(), window.height())
            for key, window in self._popout_windows.items()
        }

    def _on_popout_window_closed(
        self, key: tuple[str, str], window_id: int | None = None
    ) -> None:
        """Cleans up tracking of a closed own window
        (`self._popout_windows`). If the window was closed directly by
        the user (e.g. via the X, instead of the checkbox in the
        dialog), the channel(s) shouldn't vanish without a trace but
        reappear in the main grid - hence `plot_popout` is also reset
        (for the whole plot group) and rebuilt here.

        `window_id` identifies the window that was destroyed: a window
        the view closed itself (see `_close_popout_window`) is ignored
        here, and so is a stale signal for a window that has meanwhile
        been replaced by a new one under the same key.

        `destroyed` is a QUEUED connection and can therefore still fire
        AFTER the live view itself has already been destroyed (e.g. when
        closing the application with an own window still open) -
        `sip.isdeleted` prevents accessing an already torn-down
        `self._plot_widget` in `_rebuild_plots()` in that case.
        """
        programmatic = self.__dict__.get("_popout_programmatic_closes", set())
        if window_id is not None and window_id in programmatic:
            programmatic.discard(window_id)
            return
        current = self._popout_windows.get(key)
        if window_id is not None and current is not None and id(current) != window_id:
            return
        self._popout_windows.pop(key, None)
        self._popout_y_auto_active.pop(key, None)
        slot = self.__dict__.get("_popout_slots", {}).pop(key, None)
        if sip.isdeleted(self):
            return
        channel = self._find_channel_by_key(key)
        if channel is not None and channel.plot_popout:
            indices = slot.indices if slot is not None else (self._channel_index(channel),)
            for index in indices:
                if index < len(self._channels):
                    self._channels[index].plot_popout = False
            self._rebuild_plots()

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _make_value_box(
        self,
        row: int,
        col: int,
        text: str,
        point_size: int,
        bold: bool,
        box_width_px: float,
        align: str = "center",
        margin_px: float = 0.0,
        layout=None,
    ) -> tuple[pg.ViewBox, pg.TextItem]:
        """Creates an axis-/interaction-free `ViewBox` with a `TextItem`
        for one cell of the value readout (see `_rebuild_plots`) -
        deliberately NOT a `LabelItem` (see comment there).

        `align="right"`/`"left"` with `margin_px`: positions the text not
        centered, but flush against one edge with `margin_px` spacing
        from it (in pixels, converted to the `ViewBox`'s own coordinates
        via `box_width_px`) - used for the number (right, `margin_px=0`)
        and the unit (left, `margin_px`=space-character width), so the
        gap between the two ALWAYS equals exactly one space-character
        width (see `_rebuild_plots`), instead of depending on the
        (variously wide) text itself.

        `layout`: nested `GraphicsLayout` to place the box in (stacked
        readouts of a plot group, see `_rebuild_plots`) - default is the
        main grid itself.
        """
        target = layout if layout is not None else self._plot_widget
        box = target.addViewBox(row=row, col=col, lockAspect=False)
        box.setMouseEnabled(x=False, y=False)
        box.setMenuEnabled(False)
        box.setRange(xRange=(-1, 1), yRange=(-1, 1), padding=0)
        margin_units = (2.0 * margin_px / box_width_px) if box_width_px else 0.0
        if align == "right":
            anchor = (1.0, 0.5)
            x = 1.0 - margin_units
        elif align == "left":
            anchor = (0.0, 0.5)
            x = -1.0 + margin_units
        else:
            anchor = (0.5, 0.5)
            x = 0.0
        text_item = pg.TextItem(text, color=plot_foreground_color(), anchor=anchor)
        font = QFont()
        font.setPointSize(point_size)
        font.setBold(bold)
        text_item.setFont(font)
        box.addItem(text_item)
        text_item.setPos(x, 0.0)
        return box, text_item

    def _rebuild_plots(self) -> None:
        """Creates one subplot per PLOT of the main grid (with an
        independent X axis, see `Channel.plot_time_window_seconds`) - a
        single channel, or several channels of the same plot group as
        several curves with a legend (see `Channel.plot_number`/
        `_plot_slots`).

        A channel does NOT appear here if it is either completely
        disabled (`Channel.plot_visible=False`) OR its plot is instead
        shown in its own window (`plot_popout=True` of the plot's lead
        channel, see `ChannelPopoutWindow`/`_open_popout_window`) - so
        every visible channel ends up in EXACTLY one place, never twice.

        Per plot: `self._plot_items[p]`/`self._plot_slots[p]`. Per curve:
        `self._curves[i]`/`self._curve_channel_indices[i]` (index into
        `self._channels`)/`self._curve_plot_positions[i]` (its plot) plus
        the value readout boxes - hidden/popped-out channels are skipped,
        so curve position and channel index are NOT necessarily
        identical (see `_on_timer_tick`).
        """
        self._plot_widget.clear()
        self._plot_items = []
        self._plot_slots = []
        self._curves = []
        self._curve_channel_indices = []
        self._curve_plot_positions = []
        self._value_boxes = []
        self._value_labels = []
        self._value_unit_boxes = []
        self._value_unit_labels = []
        self._channel_buffer_positions = {index: 0 for index in range(len(self._channels))}
        self._channel_cycle_starts = {index: 0.0 for index in range(len(self._channels))}
        self._channel_x_range_applied = {index: None for index in range(len(self._channels))}
        self._sweep_caches = {}
        self._channel_y_auto_active = {}

        # Width of the number column is based on the channel with the
        # MOST configured integer digits (see
        # `Channel.plot_value_integer_digits`/`_number_field_width_px`),
        # since the column width in the `GraphicsLayoutWidget` applies to
        # the whole column, i.e. to every plot in it.
        number_font = QFont()
        number_font.setPointSize(_VALUE_NUMBER_POINT_SIZE)
        number_font.setBold(True)
        number_field_width = _number_field_width_px(
            number_font,
            max((c.plot_value_integer_digits for c in self._channels), default=3),
            max((c.plot_value_decimal_digits for c in self._channels), default=3),
        )
        value_unit_gap = _space_width_px(number_font)

        slots = _plot_slots(self._channels)
        columns = max(1, self._plot_columns)
        position = 0
        for slot in slots:
            lead = self._channels[slot.lead]
            if lead.plot_popout:
                continue
            members = [self._channels[i] for i in slot.indices]
            background = _channel_background_color(lead)
            row, cell = divmod(position, columns)
            col_base = cell * 3

            # Value readouts: one per channel with `plot_show_value` -
            # for a group STACKED in a nested layout inside the number/
            # unit columns (one line per channel, in its curve color, see
            # `_apply_channel_appearance`).
            value_indices = [i for i in slot.indices if self._channels[i].plot_show_value]
            boxes: dict[int, tuple] = {}
            if value_indices:
                number_layout = unit_layout = None
                if slot.is_group:
                    number_layout = self._plot_widget.addLayout(row=row, col=col_base)
                    unit_layout = self._plot_widget.addLayout(row=row, col=col_base + 1)
                    for layout in (number_layout, unit_layout):
                        layout.setContentsMargins(0, 0, 0, 0)
                        layout.setSpacing(0)
                for line, index in enumerate(value_indices):
                    value_box, value_text = self._make_value_box(
                        line if slot.is_group else row,
                        0 if slot.is_group else col_base,
                        "--",
                        point_size=_VALUE_NUMBER_POINT_SIZE,
                        bold=True,
                        box_width_px=number_field_width,
                        align="right",
                        layout=number_layout,
                    )
                    value_box.setBackgroundColor(plot_container_background_color())
                    unit_box, unit_text = self._make_value_box(
                        line if slot.is_group else row,
                        0 if slot.is_group else col_base + 1,
                        self._channels[index].unit,
                        point_size=_VALUE_UNIT_POINT_SIZE,
                        bold=False,
                        box_width_px=_VALUE_UNIT_WIDTH,
                        align="left",
                        margin_px=value_unit_gap,
                        layout=unit_layout,
                    )
                    unit_box.setBackgroundColor(plot_container_background_color())
                    boxes[index] = (value_box, value_text, unit_box, unit_text)
                plot_col, plot_colspan = col_base + 2, 1
            else:
                plot_col, plot_colspan = col_base, 3

            plot_item = self._plot_widget.addPlot(
                row=row,
                col=plot_col,
                colspan=plot_colspan,
                title=_plot_title(members),
            )
            plot_item.showGrid(x=lead.plot_show_grid, y=lead.plot_show_grid, alpha=_GRID_ALPHA)
            plot_item.setLabel("bottom", f"{t('axis_time')} [s]", **_axis_label_style())
            plot_item.setLabel("left", _plot_axis_label(members), **_axis_label_style())
            style_plot_item(plot_item)
            if slot.is_group:
                # BEFORE the curves: PyQtGraph only adds curves with a
                # `name` to a legend that already exists.
                plot_item.addLegend(offset=(10, 10))
                _style_legend(plot_item)
            plot_pos = len(self._plot_items)
            for index in slot.indices:
                channel = self._channels[index]
                curve = plot_item.plot(
                    pen=pg.mkPen(
                        color=channel.plot_color or channel_curve_color(index),
                        width=channel.plot_line_width,
                    ),
                    name=channel.display_name if slot.is_group else None,
                )
                # NO `autoDownsample` - see comment in `ChannelPopoutWindow.__init__`.
                curve.setClipToView(True)
                value_box, value_text, unit_box, unit_text = boxes.get(index, (None,) * 4)
                self._curves.append(curve)
                self._curve_channel_indices.append(index)
                self._curve_plot_positions.append(plot_pos)
                self._value_boxes.append(value_box)
                self._value_labels.append(value_text)
                self._value_unit_boxes.append(unit_box)
                self._value_unit_labels.append(unit_text)
            plot_item.getViewBox().setBackgroundColor(background)
            plot_item.setVisible(lead.plot_show_graph)

            # Sweep display (oscilloscope-style, see class doc further
            # up): X range fixed at the window width, only shifted on a
            # cycle change (see `_on_timer_tick`).
            plot_item.enableAutoRange(x=False)
            plot_item.setXRange(0.0, lead.plot_time_window_seconds, padding=0)

            self._plot_items.append(plot_item)
            self._plot_slots.append(slot)
            position += 1

        if self._plot_items:
            for cell in range(columns):
                col_base = cell * 3
                self._plot_widget.ci.layout.setColumnFixedWidth(
                    col_base, number_field_width
                )
                self._plot_widget.ci.layout.setColumnFixedWidth(
                    col_base + 1, _VALUE_UNIT_WIDTH
                )
                self._plot_widget.ci.layout.setColumnStretchFactor(col_base + 2, 1)

        # Own windows: one per plot whose lead has `plot_popout`. A window
        # whose plot no longer exists - or whose group membership changed
        # - is closed (and, in the latter case, reopened below).
        wanted: dict[tuple[str, str], _PlotSlot] = {
            _channel_display_key(self._channels[slot.lead]): slot
            for slot in slots
            if self._channels[slot.lead].plot_popout
        }
        for key in list(self._popout_windows.keys()):
            slot = wanted.get(key)
            window = self._popout_windows[key]
            member_keys = (
                tuple(_channel_display_key(self._channels[i]) for i in slot.indices)
                if slot is not None
                else None
            )
            if slot is None or getattr(window, "member_keys", None) != member_keys:
                self._close_popout_window(key)
            else:
                self._popout_slots[key] = slot
                if hasattr(window, "set_channels"):
                    window.set_channels([self._channels[i] for i in slot.indices])
        for key, slot in wanted.items():
            if key not in self._popout_windows:
                self._open_popout_window(slot)

        self._apply_channel_appearance()
        self._apply_y_range_mode()

    @staticmethod
    def _apply_plot_style(plot_item, channel: Channel) -> None:
        """Plot-wide part of the styling (background, gridline color, grid
        and axis title visibility, plot on/off) - from the plot's lead
        channel (see `PLOT_LEVEL_FIELDS`). Shared by main-grid subplots
        (`_apply_channel_appearance`) and own windows
        (`_open_popout_window`)."""
        plot_item.getViewBox().setBackgroundColor(_channel_background_color(channel))
        grid_color = _channel_grid_color(channel)
        for axis_name in ("left", "bottom", "right", "top"):
            axis = plot_item.getAxis(axis_name)
            if axis is not None:
                axis.setTickPen(grid_color)
        # Axis titles on/off - here rather than only at creation time, so
        # a change in the dialog takes effect without a full rebuild
        # (this method is the shared update path for main-grid subplots
        # and own windows alike, see `_apply_channel_appearance`).
        _apply_axis_label_visibility(plot_item, channel)
        # Grid on/off, same reasoning - the grid COLOR above was already
        # per channel while the grid itself used to be hardwired on.
        # `alpha` stays at the previous fixed value: it is a rendering
        # detail, not something worth a second control next to the switch.
        plot_item.showGrid(
            x=channel.plot_show_grid, y=channel.plot_show_grid, alpha=_GRID_ALPHA
        )
        plot_item.setVisible(channel.plot_show_graph)

    @staticmethod
    def _apply_curve_style(curve, channel: Channel, index: int = 0) -> None:
        """Per-curve part of the styling: color and width of ONE channel."""
        # Falls back to the channel's PALETTE slot, not one shared color:
        # every channel used to be drawn in the same blue, which made a grid
        # of subplots hard to scan. Derived from the position on each
        # repaint, never stored - a stored color would stop following the
        # theme (see `channel_curve_color`).
        color = channel.plot_color or channel_curve_color(index)
        # Clamped like everywhere the width is read: a stored 0 would make
        # the curve invisible with nothing in the dialog explaining why.
        curve.setPen(pg.mkPen(color=color, width=max(0.1, channel.plot_line_width)))

    @staticmethod
    def _apply_channel_curve_style(
        plot_item, curve, channel: Channel, index: int = 0
    ) -> None:
        """Plot AND curve styling of a single-channel plot (see
        `_apply_plot_style`/`_apply_curve_style`)."""
        LiveView._apply_plot_style(plot_item, channel)
        LiveView._apply_curve_style(curve, channel, index)

    def _apply_channel_appearance(self) -> None:
        """Applies curve color, background color, and plot visibility per
        plot/channel (see `open_channel_display_dialog`), for main-grid
        subplots AND open own windows.

        `plot_show_value` itself is NO LONGER handled HERE - a change
        needs a full `_rebuild_plots()` (see
        `_apply_display_settings_to_live_channels`/`_rebuild_plots`), the
        boxes no longer even exist for main-grid channels without a value
        readout (`None`, see there). `plot_show_graph`, on the other
        hand, only needs a plain show/hide here (see
        `_apply_plot_style` for the main-grid part of that).

        Value readout text: theme foreground for a single channel (as
        before), the curve color in a group - with several numbers
        stacked next to one plot, the color is what says which is which.
        """
        for pos, plot_item in enumerate(self._plot_items):
            self._apply_plot_style(plot_item, self._channels[self._plot_slots[pos].lead])
        for pos, curve in enumerate(self._curves):
            index = self._curve_channel_indices[pos]
            channel = self._channels[index]
            self._apply_curve_style(curve, channel, index)
            # Window background color, not the individual channel color -
            # see `_rebuild_plots` for the reasoning.
            if self._value_boxes[pos] is not None:
                self._value_boxes[pos].setBackgroundColor(plot_container_background_color())
            if self._value_unit_boxes[pos] is not None:
                self._value_unit_boxes[pos].setBackgroundColor(plot_container_background_color())
            slot = self._plot_slots[self._curve_plot_positions[pos]]
            text_color = (
                channel.plot_color or channel_curve_color(index)
                if slot.is_group
                else plot_foreground_color()
            )
            for label in (self._value_labels[pos], self._value_unit_labels[pos]):
                if label is not None:
                    label.setColor(text_color)
        for key, window in self._popout_windows.items():
            slot = self._popout_slots.get(key)
            if slot is None:
                channel = self._find_channel_by_key(key)
                if channel is None:
                    continue
                slot = _PlotSlot((self._channel_index(channel),))
            lead = self._channels[slot.lead]
            self._apply_plot_style(window.plot_item, lead)
            for curve, index in zip(window.curves, slot.indices):
                self._apply_curve_style(curve, self._channels[index], index)
            window.value_colors = [
                (self._channels[index].plot_color or channel_curve_color(index))
                if slot.is_group
                else None
                for index in slot.indices
            ]
            # Own window is a normal Qt layout (not a PyQtGraph
            # `GraphicsLayout` with a fixed column width like in the
            # main grid) - `setVisible()` on the `PlotWidget` itself
            # is enough here, cleanly returns the space to the value
            # readout next to it.
            window.plot_widget.setVisible(lead.plot_show_graph)
            window._apply_number_width()
            window._style_value_labels()
            window.apply_value_visibility()

    def _apply_y_range_mode(self) -> None:
        """Applies the Y range (fixed, autoscale, or hybrid) to all
        subplots AND open own windows - without current readings (see
        `_apply_channel_y_range`), e.g. directly after `_rebuild_plots()`
        or after changing the settings in the dialog, before the next
        tick delivers new data. Per plot from its lead channel (see
        `PLOT_LEVEL_FIELDS`).
        """
        for pos, plot_item in enumerate(self._plot_items):
            channel = self._channels[self._plot_slots[pos].lead]
            self._apply_channel_y_range(plot_item, channel, None)
        for key, window in self._popout_windows.items():
            channel = self._find_channel_by_key(key)
            if channel is not None:
                self._apply_channel_y_range(
                    window.plot_item, channel, None, self._popout_y_auto_active
                )

    def _apply_channel_y_range(
        self,
        plot_item,
        channel: Channel,
        data_range: tuple[float, float] | None,
        auto_active_cache: dict[tuple[str, str], bool] | None = None,
    ) -> None:
        """Sets the Y axis of a single subplot according to the
        autoscaling configured per channel (see `ChannelDisplayDialog`).

        Not a plain on/off: if autoscaling is enabled for the channel
        (default), the configured fixed range is used AS LONG AS
        `data_range` (min/max of the currently displayed readings, see
        `_SweepCache.value_range`; `None` = none yet) lies within
        it - as soon as even one value exceeds/undershoots that range,
        PyQtGraph's autoscale takes over for the rest of the current
        sweep. If autoscaling is disabled, the fixed range always stays
        active, regardless of `data_range`.

        `auto_active_cache` (default `self._channel_y_auto_active`)
        prevents unnecessary `setYRange`/`enableAutoRange` calls when the
        effective mode hasn't changed since the last call. The main-grid
        subplot and own window (see `ChannelPopoutWindow`) of the same
        channel deliberately use DIFFERENT caches
        (`self._popout_y_auto_active`) - they have separate `plot_item`
        instances and must not skip each other when switching to
        autoscale.
        """
        if auto_active_cache is None:
            auto_active_cache = self._channel_y_auto_active
        key = _channel_display_key(channel)
        default_min = channel.min_range if channel.min_range is not None else -10.0
        default_max = channel.max_range if channel.max_range is not None else 10.0
        y_min = channel.plot_y_min if channel.plot_y_min is not None else default_min
        y_max = channel.plot_y_max if channel.plot_y_max is not None else default_max
        autoscale = channel.plot_autoscale

        if autoscale:
            use_auto = bool(
                data_range is not None
                and (data_range[0] < y_min or data_range[1] > y_max)
            )
        else:
            use_auto = False

        if auto_active_cache.get(key) == use_auto:
            return
        auto_active_cache[key] = use_auto

        if use_auto:
            plot_item.enableAutoRange(y=True)
        else:
            plot_item.enableAutoRange(y=False)
            plot_item.setYRange(y_min, y_max, padding=0)

    def _on_storage_timer_tick(self) -> None:
        """Updates the storage writer's storage-buffer display.

        The reference quantity ("Maximum") is the configured ring buffer
        capacity (`RingBuffer.capacity`, see
        `setup_view._calculate_dynamic_buffer_size`) - not the free disk
        space. Shows how many samples already written by the DAQ thread
        the storage writer has NOT yet transferred to disk
        (`StorageWriter.pending_samples`). If the disk can't keep up
        (e.g. because it's too slow or full), this backlog grows; once it
        reaches the capacity, unwritten samples in the ring buffer get
        overwritten - an unrecoverable data loss (overrun, see
        `core/ringbuffer.py`). This makes it a more direct risk indicator
        than plain free disk space.
        """
        if self._storage_writer is None:
            return

        ring_buffer = self._controller.get_ring_buffer()
        if ring_buffer is None:
            return

        try:
            pending = self._storage_writer.pending_samples
            file_bytes = self._storage_writer.output_path.stat().st_size
        except (KeyError, OSError):
            logger.debug("Speicherpuffer-Status konnte nicht ermittelt werden", exc_info=True)
            return

        capacity = ring_buffer.capacity
        percent = (pending / capacity * 100.0) if capacity > 0 else 0.0

        self._storage_progress.setValue(int(round(min(100.0, percent))))
        detail_text = t(
            "storage_detail",
            file_size=_format_bytes(file_bytes),
            pending=f"{pending:,}",
            capacity=f"{capacity:,}",
            percent=f"{percent:.1f}",
        )
        if get_language() == "de":
            # German number format: thousands dot instead of comma (only
            # affects the :,-formatted integers, not the decimal numbers
            # already formatted with a dot).
            detail_text = detail_text.replace(",", ".")
        self._storage_detail_label.setText(detail_text)
        if percent >= _STORAGE_CRITICAL_PERCENT:
            color = "#dc3545"  # red
        elif percent >= _STORAGE_WARN_PERCENT:
            color = "#fd7e14"  # orange
        else:
            color = "#28a745"  # green
        self._storage_progress.setStyleSheet(f"QProgressBar::chunk {{ background-color: {color}; }}")

    def _on_timer_tick(self) -> None:
        if self._reader_id is None:
            return

        session = self._controller.current_session
        if session is not None and session.start_time is not None:
            self._duration_label.setText(
                t("duration_value", value=f"{session.duration_seconds:.1f} s")
            )
            # Configured recording limit (see
            # `data/models.py::MeasurementConfig.is_recording_limit_reached`)
            # - checked against the actually acquired sample count, NOT
            # wall-clock time: samples are clocked by the DAQ module's
            # hardware sample clock, which makes the limit reliable
            # regardless of GUI/thread delays. Stops via the same path as
            # the manual "Stop Measurement" button
            # (`self._stop_button.clicked.connect(self.stop_requested.emit)`),
            # so metadata/storage writer are finalized identically.
            #
            # IMPORTANT for triggered measurements: during the armed phase
            # (`self._armed`) there's NO check yet - nothing is being
            # recorded yet after all. Afterward, checked against
            # `total_samples_acquired - _recording_baseline_samples`
            # instead of the raw counter, since that already runs from
            # the start of acquisition (= arming instant), not only from
            # the actual trigger (see `mark_recording_started`). On a
            # manual start, the baseline stays 0, i.e. unchanged behavior.
            if (
                not self._armed
                and not session.config.recording_unlimited
                and session.config.is_recording_limit_reached(
                    self._controller.total_samples_acquired
                    - self._recording_baseline_samples
                )
            ):
                self.stop_requested.emit()
                return
        self._sample_rate_label.setText(
            t("sample_rate_value", value=self._format_sample_rate_label_value())
        )

        max_display_samples = int(self._sample_rate_hz * self._display_window_seconds)
        raw = self._controller.read_live_data(self._reader_id, max_samples=max_display_samples)
        if raw.shape[1] == 0:
            return

        scaled = apply_scaling(raw, self._channels)
        self._check_threshold_trigger(scaled)
        self._check_stop_threshold_trigger(scaled)
        self._write_to_display_buffer(scaled)
        # Numeric readout runs on its OWN, per-channel rate (see
        # `Channel.plot_value_refresh_hz`) - computed once here for both
        # the main grid and any own windows, so the same channel never
        # shows two different numbers at the same moment.
        due_values = self._update_value_readouts(scaled)

        # Per channel ONCE per tick (shared by the main grid and own
        # windows), incrementally - see `_SweepCache`.
        draw_data: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        value_ranges: dict[int, tuple[float, float] | None] = {}
        for index in range(len(self._channels)):
            cache, values = self._sweep_cache(index)
            draw_data[index] = (
                cache.display_data(values) if cache is not None else (np.array([]), values)
            )
            value_ranges[index] = cache.value_range() if cache is not None else None
        if not any(values.size for _times, values in draw_data.values()):
            return

        for pos, curve in enumerate(self._curves):
            channel_index = self._curve_channel_indices[pos]
            curve.setData(*draw_data[channel_index])
            if channel_index in due_values and self._value_labels[pos] is not None:
                channel = self._channels[channel_index]
                self._value_labels[pos].setText(
                    _format_channel_value(
                        due_values[channel_index],
                        channel.plot_value_integer_digits,
                        channel.plot_value_decimal_digits,
                    )
                )

        # Re-evaluate per-plot hybrid autoscaling (fixed range, until
        # readings exceed/undershoot it - see `_apply_channel_y_range`)
        # with the values ACTUALLY displayed NOW - for a group over ALL of
        # its curves (one shared Y axis).
        for pos, plot_item in enumerate(self._plot_items):
            slot = self._plot_slots[pos]
            self._apply_channel_y_range(
                plot_item,
                self._channels[slot.lead],
                _union_value_range([value_ranges[i] for i in slot.indices]),
            )

        # Update own windows (see `ChannelPopoutWindow`) with the same
        # values, independent of the main grid - `draw_data` is always
        # indexed by `self._channels` (see `apply_scaling`), regardless of
        # `plot_visible`, hence by index rather than position here.
        for key, window in self._popout_windows.items():
            slot = self._popout_slots.get(key)
            if slot is None:
                continue
            for curve, value_label, index in zip(
                window.curves, window.value_labels, slot.indices
            ):
                channel = self._channels[index]
                curve.setData(*draw_data[index])
                if index in due_values:
                    value_label.setText(
                        _format_channel_value(
                            due_values[index],
                            channel.plot_value_integer_digits,
                            channel.plot_value_decimal_digits,
                        )
                    )
            self._apply_channel_y_range(
                window.plot_item,
                self._channels[slot.lead],
                _union_value_range([value_ranges[i] for i in slot.indices]),
                self._popout_y_auto_active,
            )

        # The X range itself stays fixed at window width (sweep doesn't
        # scroll) - only shifted to the new absolute time span on an
        # actual cycle change (new sweep started), so the axis label
        # shows the real measurement time (see `_channel_cycle_starts`).
        # Deliberately not set on every tick - per channel, since the
        # window length (and thus the cycle rhythm) can differ per
        # channel.
        for pos, plot_item in enumerate(self._plot_items):
            # Per PLOT, from its lead channel - all members of a group
            # have the same time window (see `sync_plot_groups`).
            channel_index = self._plot_slots[pos].lead
            x_min = self._channel_cycle_starts.get(channel_index, 0.0)
            if self._channel_x_range_applied.get(channel_index) == x_min:
                continue
            self._channel_x_range_applied[channel_index] = x_min
            x_max = x_min + self._channels[channel_index].plot_time_window_seconds
            plot_item.setXRange(x_min, x_max, padding=0)
        for key, window in self._popout_windows.items():
            channel = self._find_channel_by_key(key)
            if channel is None:
                continue
            channel_index = self._channels.index(channel)
            x_min = self._channel_cycle_starts.get(channel_index, 0.0)
            if self._channel_x_range_applied.get(channel_index) == x_min:
                continue
            self._channel_x_range_applied[channel_index] = x_min
            window.plot_item.setXRange(
                x_min, x_min + channel.plot_time_window_seconds, padding=0
            )

    @staticmethod
    def _capacity_for_rate(rate_hz: float, window_seconds: float) -> int:
        """Sample count for ONE sweep window at a given rate - shared
        rounding logic for `_ensure_display_buffer` and
        `_channel_display_capacity`."""
        return max(1, int(rate_hz * window_seconds))

    def _ensure_display_buffer(self, num_channels: int) -> None:
        """Initializes or resizes the internal sweep display buffer.

        ONE shared array for all channels - the column count is the
        LARGEST capacity needed by any single channel (its own native
        rate * `plot_time_window_seconds`, see `_channel_native_rates`),
        NO LONGER a blanket `self._sample_rate_hz * largest window` - a
        channel with its own, slower native rate (e.g. NI9210 @ 14 S/s)
        needs significantly fewer columns for that than before (see
        `_write_to_display_buffer`, whose cost scales with the buffer
        fill position, not just with the capacity).
        """
        capacity = max(
            (
                self._capacity_for_rate(
                    self._channel_native_rates.get(index, self._sample_rate_hz),
                    channel.plot_time_window_seconds,
                )
                for index, channel in enumerate(self._channels)
            ),
            default=self._capacity_for_rate(self._sample_rate_hz, self._display_window_seconds),
        )
        if self._display_buffer is None or self._display_buffer.shape != (num_channels, capacity):
            self._display_capacity_samples = capacity
            self._display_buffer = np.zeros((num_channels, capacity), dtype=np.float64)
            self._buffer_write_pos = 0
            self._sweep_caches = {}

    def _channel_display_capacity(self, channel_index: int) -> int:
        """Maximum sample count of ONE sweep cycle for `channel_index`
        (OWN native rate * configured time span, capped to the actually
        allocated buffer size) - shared by `_write_to_display_buffer`
        (ring-buffer wraparound point) AND `_downsample_for_display` (see
        there for why that matters for a stable downsampling factor - ONE
        fixed value per cycle; `_channel_native_rates` doesn't change
        while a measurement is running, so the return value stays stable
        too). Uses the rate from `_channel_native_rates` instead of the
        global tick rate `self._sample_rate_hz` - for a channel with its
        own, slower native rate, the capacity would otherwise be too
        large by a factor of tick rate/native rate."""
        native_rate = self._channel_native_rates.get(channel_index, self._sample_rate_hz)
        return max(
            1,
            min(
                self._display_capacity_samples,
                self._capacity_for_rate(
                    native_rate, self._channels[channel_index].plot_time_window_seconds
                ),
            ),
        )

    def _extract_native_rate_samples(self, channel_index: int, row: np.ndarray) -> np.ndarray:
        """Reduces `row` (ZOH-forward-filled at tick rate, see
        `core/rate_merge.py`) to exactly the values that are actually
        newly DUE according to this channel's native rate within this
        block - the same `due(t) = floor(t * native_rate / tick_rate)`
        counting as `core/rate_merge.py::RateMerger`, reproduced here
        independently based on the cumulative tick count
        (`_channel_total_ticks_seen`).

        DELIBERATELY purely tick-/time-based, NOT value-based (an earlier
        attempt compared consecutive values for equality) - a real, but
        coincidentally unchanged sample of a slow channel (e.g. a stable
        thermocouple reading) would have looked like a ZOH repeat in that
        case and been incorrectly discarded. That made the sweep buffer
        position grow more slowly than real measurement time was passing
        - the sweep display visibly lagged behind real time as a result.
        """
        ticks_before = self._channel_total_ticks_seen.get(channel_index, 0)
        n = row.shape[0]
        ticks_after = ticks_before + n
        self._channel_total_ticks_seen[channel_index] = ticks_after

        native_rate = self._channel_native_rates.get(channel_index, self._sample_rate_hz)
        due_before = int(ticks_before * native_rate / self._sample_rate_hz)
        due_after = int(ticks_after * native_rate / self._sample_rate_hz)
        num_new = due_after - due_before
        if num_new <= 0:
            return row[:0]

        # For each newly due "slot", find the tick index WITHIN this
        # block at which it is first reached (row is ZOH-held, so the
        # value there is the one valid for that slot) - `due_at_tick` is
        # monotonically non-decreasing, `searchsorted` delivers that
        # directly, vectorized, without a Python loop.
        local_ticks = ticks_before + np.arange(1, n + 1)
        due_at_tick = (local_ticks * native_rate / self._sample_rate_hz).astype(np.int64)
        target_due = due_before + np.arange(1, num_new + 1)
        indices = np.searchsorted(due_at_tick, target_due)
        return row[indices]

    def _write_to_display_buffer(self, scaled_block: np.ndarray) -> None:
        """Writes new samples into the sweep buffer (see class doc above).

        Fills up the current sweep from the write position onward. If the
        new block extends past the end of the window, the excess
        remainder starts a NEW sweep at index 0 - the old curve
        disappears completely at that point, instead of (as with a
        classic ring buffer) slowly scrolling out at the left edge. A
        loop instead of recursion, in case a single block (after a GUI
        delay) contains even more than one full window.

        Channels with a native rate slower than the tick rate (see
        `_channel_native_rates`, e.g. NI9210 @ 14 S/s) arrive in
        `scaled_block` ZOH-forward-filled (see `core/rate_merge.py`) -
        each row repeats the same value until the next real hardware
        sample. Only the slots that are actually newly due according to
        the native rate are written (see `_extract_native_rate_samples` -
        purely tick-/time-based, NOT based on value equality, otherwise a
        real, but coincidentally unchanged sample of a slow channel would
        be incorrectly discarded as a repeat). A channel at the tick rate
        itself (the normal case) does NOT go through this reduction.
        """
        if self._display_buffer is None:
            return
        for channel_index in range(scaled_block.shape[0]):
            native_rate = self._channel_native_rates.get(channel_index, self._sample_rate_hz)
            row = scaled_block[channel_index]
            if native_rate < self._sample_rate_hz:
                new_data = self._extract_native_rate_samples(channel_index, row)
            else:
                new_data = row

            cap = self._channel_display_capacity(channel_index)
            pos = self._channel_buffer_positions.get(channel_index, 0)
            start = 0
            while start < new_data.shape[0]:
                if pos >= cap:
                    pos = 0
                    self._channel_cycle_starts[channel_index] = (
                        self._channel_cycle_starts.get(channel_index, 0.0)
                        + cap / native_rate
                    )
                take = min(cap - pos, new_data.shape[0] - start)
                self._display_buffer[channel_index, pos:pos + take] = new_data[
                    start:start + take
                ]
                pos += take
                start += take
            self._channel_buffer_positions[channel_index] = pos
        self._buffer_write_pos = max(self._channel_buffer_positions.values(), default=0)

    def _get_channel_display_view(self, channel_index: int) -> tuple[np.ndarray, np.ndarray]:
        """Returns the current sweep for a single channel.

        The time values are shifted by `_channel_cycle_starts[channel_index]`,
        so they show the actual measurement time (e.g. "40-45s" in the
        9th cycle of a 5s window) instead of always starting at 0 - the
        sweep itself (curve runs through a fixed, per-channel
        configurable window, then resets) is unaffected by this.

        The return value grows with the sweep position (instead of a
        constant, NaN-padded window length - this was tried, but made
        the display worse rather than better: every curve would then
        have had to be processed at full window length on EVERY tick,
        even when only a few points are real data).

        ALWAYS shows the full currently-arrived state
        (`_channel_buffer_positions`) - deliberately WITHOUT artificial
        catch-up pacing: an earlier attempt to "smear" newly arrived
        ~25ms blocks (see `gui/setup_view.py::_calculate_samples_per_read`)
        across several ticks did smooth out the curve's visible
        block-wise growth, but introduced noticeable extra latency in the
        process - for a direct stimulus-response test (tap test on an
        accelerometer) that was unacceptable: latency matters more than
        display smoothness for a live measurement instrument.

        The time axis uses the channel's native rate
        (`_channel_native_rates`) instead of the global tick rate
        `self._sample_rate_hz` - for a channel with its own, slower
        native rate, `position` (see `_write_to_display_buffer`) doesn't
        grow at the tick-rate pace, so a slot would correspond to the
        wrong (too short) time span at the global rate.
        """
        cache, values = self._sweep_cache(channel_index)
        if cache is None:
            return np.array([]), np.empty((0,))
        return cache.times[:values.shape[0]], values

    def _sweep_cache(self, channel_index: int) -> tuple[_SweepCache | None, np.ndarray]:
        """Brings the `_SweepCache` of `channel_index` up to date and
        returns it together with the current sweep's values (a view into
        the display buffer). `(None, empty)` if the sweep has no data yet.
        """
        if self._display_buffer is None:
            return None, np.empty((0,))
        position = self._channel_buffer_positions.get(channel_index, 0)
        if position == 0:
            return None, np.empty((0,))
        values = self._display_buffer[channel_index, :position]
        cycle_start = self._channel_cycle_starts.get(channel_index, 0.0)
        capacity = self._channel_display_capacity(channel_index)
        rate = self._channel_native_rates.get(channel_index, self._sample_rate_hz)
        caches = self.__dict__.setdefault("_sweep_caches", {})
        cache = caches.get(channel_index)
        if cache is None or not cache.matches(cycle_start, capacity, rate, position):
            cache = caches[channel_index] = _SweepCache(cycle_start, capacity, rate)
        cache.advance(values)
        return cache, values
