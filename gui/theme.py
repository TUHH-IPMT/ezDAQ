"""
gui/theme.py

Simple light/dark theming for the entire application.

Usage (see `gui/i18n.py` for the same basic pattern):
    from gui.theme import init_theme, set_theme, connect_theme_changed

    init_theme(app)          # once at app startup, after QApplication(...)
    set_theme("dark")        # or "light" - takes effect immediately on all Qt widgets

Live switching:
    Standard Qt widgets (buttons, labels, menus, ...) automatically follow
    the `QApplication` `QPalette` - `set_theme()` alone is enough for that.
    PyQtGraph plots (Live View, Analysis) do NOT follow the palette
    automatically; views with plots register via
    `connect_theme_changed(self.retheme_plots)` and recolor their existing
    plot widgets themselves via
    `style_plot_container()`/`style_plot_item()`.
"""

from __future__ import annotations

import math
from typing import Callable, Optional

import pyqtgraph as pg
from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QPolygonF,
)

_current_theme = "light"

_PLOT_COLORS = {
    # Deliberately hex instead of PyQtGraph shorthand ("w"/"k"): those are
    # also used in real Qt stylesheets (see
    # `gui/live_view.py::ChannelPopoutWindow._style_value_labels`), where
    # "w"/"k" are NOT valid CSS and were silently dropped there -
    # visible as a completely black value display in the light theme (the
    # shorthand is only understood by PyQtGraph itself, not Qt's CSS
    # parser). `is_theme_default_plot_background()` still recognizes "w"
    # as a legacy value (see there), so this only affects NEWLY saved
    # values.
    "light": {"background": "#ffffff", "foreground": "#000000", "curve": "#1565c0"},
    "dark": {"background": "#232323", "foreground": "#e0e0e0", "curve": "#64b5f6"},
}

# Background color of the plot CONTAINER (axis tick border, gaps between
# subplots, value display boxes) - deliberately NOT the same as
# `_PLOT_COLORS[...]["background"]` (that stays reserved for the actual
# plot area where data/curves are drawn, see `_channel_background_color`
# in gui/live_view.py). Matches exactly `QPalette.ColorRole.Window` from
# `_build_light_palette`/`_build_dark_palette` below, so the border of the
# plot widgets visually blends with the rest of the app surface (window/
# panel background) instead of looking like a standalone white/black
# rectangle.
_PLOT_CONTAINER_COLORS = {"light": "#f0f0f0", "dark": "#353535"}

# Relative to the app's default font size (not hard-coded, see
# `_AXIS_TICK_FONT_SIZE_INCREASE` for the same principle) - the play/
# record/stop/arm buttons look a bit small at the default size for such
# prominent action buttons.
_ACTION_BUTTON_FONT_SIZE_INCREASE = 1


def _action_button_font_size_pt() -> int:
    return QFont().pointSize() + _ACTION_BUTTON_FONT_SIZE_INCREASE


# Shared base style for play/record/stop AND the trigger arm button
# (`_trigger_arm_button`) in BOTH views (`gui/setup_view.py`/
# `gui/live_view.py`) - defined in one place so they can't drift apart.
# Text/background color deliberately do NOT come as literals but via
# `palette(button-text)`/`palette(button)` - so they automatically follow
# the current theme (including automatically dimmed color in the disabled
# state, without any dedicated `:disabled` rule - that's the whole point
# of `palette(...)` in Qt stylesheets). `Button` and `Window` are
# deliberately IDENTICAL in both palettes (see `_build_light_palette`/
# `_build_dark_palette`) - without their own border, the buttons would
# therefore be visually indistinguishable from the background, hence the
# visible `border`. `palette(mid)` would be too low-contrast for that
# (see the comment on the navigation tiles in gui/main_window.py - same
# finding), hence `palette(dark)`. Hover/press get a subtle effect via
# `palette(midlight)`/`palette(mid)`. More generous padding than Qt's
# default makes the buttons overall a bit taller/more prominent.
#
# AS A FUNCTION instead of a fixed string constant (different before this
# change): `QFont()` (for the font size, see
# `_action_button_font_size_pt`) may only be called AFTER `QApplication`
# has been created - but `gui/theme.py` is sometimes imported before that
# (see the `gui/i18n.py` comment on the same issue). Callers therefore use
# `action_button_style()` instead of a module-level constant.
# NOTE: neither style below sets `color` in the plain `QPushButton {}`
# block, on purpose. A stylesheet color there applies in EVERY state
# and overrides Qt's automatic `QPalette.ColorGroup.Disabled`
# handling - a disabled play/record/stop button then keeps its
# full-strength label and looks perfectly clickable. Measured as text
# contrast against the button background: with the line, disabled was
# identical to enabled (195 light / 202 dark); without it, it drops to
# 97 and 74 respectively, i.e. Qt grays it out by itself in both
# themes. `palette(button-text)` was the default for the enabled state
# anyway, so nothing is lost.
def action_button_style() -> str:
    size = _action_button_font_size_pt()
    return (
        "QPushButton {"
        "   border: 1px solid palette(dark);"
        "   border-radius: 4px;"
        "   padding: 8px 18px;"
        "   background-color: palette(button);"
        f"   font-size: {size}pt;"
        "}"
        "QPushButton:hover { background-color: palette(midlight); }"
        "QPushButton:pressed { background-color: palette(mid); }"
    )


# Like `action_button_style()`, but with an additional, clearly visible
# "armed/active" state (`:checked`, stays pressed until clicked again) -
# using `palette(highlight)`/`palette(highlighted-text)` for that (Qt's
# dedicated roles for exactly this purpose: a highlighted element with
# guaranteed readable text on top, a strong accent tone in both themes
# instead of a neutral gray).
def trigger_arm_button_style() -> str:
    size = _action_button_font_size_pt()
    return (
        "QPushButton {"
        "   border: 1px solid palette(dark);"
        "   border-radius: 4px;"
        "   padding: 8px 18px;"
        "   background-color: palette(button);"
        f"   font-size: {size}pt;"
        "}"
        "QPushButton:hover:!checked { background-color: palette(midlight); }"
        "QPushButton:pressed:!checked { background-color: palette(mid); }"
        "QPushButton:checked {"
        "   background-color: palette(highlight);"
        "   color: palette(highlighted-text);"
        "   border-color: palette(highlight);"
        "}"
    )

# Fixed (theme-independent) icon colors for the play/record buttons (see
# `draw_play_icon`/`draw_record_icon` below as well as
# `gui/setup_view.py`/`gui/live_view.py`) - green = live view only (no
# saving), red = recording WITH saving. Stop AND the trigger arm button
# deliberately get NO fixed icon color (they stay on the theme-dependent
# `nav_icon_color()`) - "red for stop" is already taken here by the
# record button, and a fixed white icon would be invisible on a light
# theme background (neither button has had a fixed dark background since
# `ACTION_BUTTON_STYLE`/`TRIGGER_ARM_BUTTON_STYLE`).
PLAY_ICON_COLOR = QColor(46, 204, 113)
RECORD_ICON_COLOR = QColor(220, 53, 69)


def _build_light_palette() -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(240, 240, 240))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(0, 0, 0))
    palette.setColor(QPalette.ColorRole.Base, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(245, 245, 245))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(255, 255, 220))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(0, 0, 0))
    palette.setColor(QPalette.ColorRole.Text, QColor(0, 0, 0))
    palette.setColor(QPalette.ColorRole.Button, QColor(240, 240, 240))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(0, 0, 0))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 0, 0))
    # 3D shading roles (Light/Midlight/Dark/Mid/Shadow) - NOT automatically
    # derived by QPalette from Button/Window when (as here) individual
    # roles are set instead of using the single-color constructor. Without
    # them, QSS references like "palette(light)"/"palette(dark)" (see the
    # navigation tiles in gui/main_window.py) fall back to Qt's
    # theme-independent default grays instead of this theme.
    palette.setColor(QPalette.ColorRole.Light, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Midlight, QColor(247, 247, 247))
    palette.setColor(QPalette.ColorRole.Dark, QColor(160, 160, 160))
    palette.setColor(QPalette.ColorRole.Mid, QColor(200, 200, 200))
    palette.setColor(QPalette.ColorRole.Shadow, QColor(105, 105, 105))
    palette.setColor(QPalette.ColorRole.Link, QColor(0, 102, 204))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(51, 153, 255))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(150, 150, 150))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(150, 150, 150))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(150, 150, 150))
    return palette


def _build_dark_palette() -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Base, QColor(35, 35, 35))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(255, 255, 220))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(0, 0, 0))
    palette.setColor(QPalette.ColorRole.Text, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 0, 0))
    # See the comment in _build_light_palette() - the same roles were
    # missing here as well.
    palette.setColor(QPalette.ColorRole.Light, QColor(90, 90, 90))
    palette.setColor(QPalette.ColorRole.Midlight, QColor(70, 70, 70))
    palette.setColor(QPalette.ColorRole.Dark, QColor(20, 20, 20))
    palette.setColor(QPalette.ColorRole.Mid, QColor(40, 40, 40))
    palette.setColor(QPalette.ColorRole.Shadow, QColor(10, 10, 10))
    palette.setColor(QPalette.ColorRole.Link, QColor(93, 173, 226))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(0, 0, 0))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(127, 127, 127))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(127, 127, 127))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(127, 127, 127))
    return palette


_PALETTES = {"light": _build_light_palette, "dark": _build_dark_palette}


def init_theme(app) -> None:
    """Must be called once at app startup (after `QApplication(...)`,
    before the first window is created).

    Sets the `Fusion` style so that light/dark palettes take effect
    reliably on all platforms (the native Windows style ignores a custom
    `QPalette` for many widgets).
    """
    app.setStyle("Fusion")
    app.setPalette(_PALETTES[_current_theme]())
    # Global PyQtGraph default for newly created widgets, BEFORE
    # `style_plot_container()` applies explicitly - window background
    # color (see there), not the plot area color.
    pg.setConfigOption("background", _PLOT_CONTAINER_COLORS[_current_theme])
    pg.setConfigOption("foreground", _PLOT_COLORS[_current_theme]["foreground"])


def get_theme() -> str:
    """Returns the current theme ("light" or "dark")."""
    return _current_theme


# Categorical palette for the channel curves. Without it every channel
# was drawn in the same `curve` color above, so a grid of six subplots
# showed six identical blue traces.
#
# Values are the documented default palette of the data-viz guidance,
# taken unchanged - re-stepping them by hand would invalidate the
# checks they passed. Verified against ezDAQ's ACTUAL plot surfaces
# (#ffffff / #232323) rather than the reference ones, on the adjacent
# pairlist, in both modes:
#
#   lightness band, chroma floor          PASS
#   CVD separation  worst dE 9.1 / 8.4    PASS (target >= 8)
#   normal vision   worst dE 19.6 / 19.3  PASS (floor >= 15)
#   contrast        3 light slots < 3:1   WARN
#
# The contrast warning carries an obligation to label the marks
# directly - which the live view does by construction: every subplot
# is titled with its channel name and unit and carries its own axis
# labels.
#
# That direct labelling is also why the palette is used at its full
# length rather than the three-slot cap the guidance puts on
# all-pairs forms: color here is a scanning aid, not the key to which
# channel is which. Beyond eight channels the order repeats, for the
# same reason.
_CHANNEL_CURVE_COLORS = {
    "light": (
        "#2a78d6", "#eb6834", "#1baf7a", "#eda100",
        "#e87ba4", "#008300", "#4a3aa7", "#e34948",
    ),
    "dark": (
        "#3987e5", "#d95926", "#199e70", "#c98500",
        "#d55181", "#008300", "#9085e9", "#e66767",
    ),
}


def channel_curve_color(index: int) -> str:
    """Default curve color for the channel at position `index`.

    Derived from the position on every repaint rather than stored on the
    channel: a stored color would stop following the theme, which is the
    exact defect `gui/live_view.py::ChannelPlotSettingsDialog` was fixed
    for. A channel that HAS its own color keeps it - see
    `_apply_channel_curve_style`.
    """
    palette = _CHANNEL_CURVE_COLORS[_current_theme]
    return palette[max(0, index) % len(palette)]


def curve_color() -> str:
    """Default curve color for new plots in the current theme."""
    return _PLOT_COLORS[_current_theme]["curve"]


def plot_foreground_color() -> str:
    """Default foreground color (axes/text) in the current theme."""
    return _PLOT_COLORS[_current_theme]["foreground"]


def plot_background_color() -> str:
    """Default background color for new plots in the current theme.

    Fallback for channels without an individually configured background
    color (see `gui/live_view.py::ChannelDisplayDialog`).
    """
    return _PLOT_COLORS[_current_theme]["background"]


def plot_container_background_color() -> str:
    """Background color for the plot CONTAINER (everything except the
    actual plot area, see `_PLOT_CONTAINER_COLORS`) - for the value
    display boxes (see `gui/live_view.py`) AND `style_plot_container()`."""
    return _PLOT_CONTAINER_COLORS[_current_theme]


def is_theme_default_plot_background(color: str | None) -> bool:
    """Recognizes stored plot backgrounds that came from a theme default.

    Older configurations store the light default as ``"w"``; later
    versions may contain the hex values of the light or dark theme. These
    values are not individual channel colors and should follow the
    current theme on a theme switch.
    """
    return color in {"w", "#ffffff", "#232323"}


def style_plot_container(widget) -> None:
    """Sets the background of a PyQtGraph container (`PlotWidget` or
    `GraphicsLayoutWidget`) to the window background color (see
    `plot_container_background_color()`) - NOT the plot area color, which
    remains reserved for the actual data area (ViewBox)."""
    widget.setBackground(_PLOT_CONTAINER_COLORS[_current_theme])


# PyQtGraph renders axis ticks in the app's default font size by default -
# on the tightly packed axes of the live/analysis plots that looks
# unnecessarily small. `+2pt` relative to the default size (instead of a
# fixed point size) so a system font size change is still respected.
_AXIS_TICK_FONT_SIZE_INCREASE = 2


def _axis_tick_font() -> QFont:
    font = QFont()
    font.setPointSize(font.pointSize() + _AXIS_TICK_FONT_SIZE_INCREASE)
    return font


def axis_tick_point_size() -> int:
    """Point size of the axis tick labels (see `_axis_tick_font`) - public
    so axis LABELS (e.g. "Time [s]", channel name/unit on the Y axis, see
    `gui/live_view.py`) can use the same font size as the tick values
    themselves, instead of PyQtGraph's smaller default for axis titles."""
    return _axis_tick_font().pointSize()


def style_plot_item(plot_item) -> None:
    """Colors the axes and title of a single PyQtGraph `PlotItem` in the
    current theme and slightly enlarges the axis tick labels (see
    `_axis_tick_font`).

    Necessary because already-created `PlotItem`s do NOT retroactively
    pick up the global `pg.setConfigOption(...)` values - only newly
    created plots do that automatically. The title (`addPlot(title=...)`)
    is especially affected by this: it otherwise stays stuck in its
    original color (e.g. black from the light theme), even after the axes
    have already been recolored via `axis.setTextPen(...)`.
    """
    foreground = _PLOT_COLORS[_current_theme]["foreground"]
    tick_font = _axis_tick_font()
    for axis_name in ("left", "bottom", "right", "top"):
        axis = plot_item.getAxis(axis_name)
        if axis is not None:
            axis.setPen(foreground)
            axis.setTextPen(foreground)
            axis.setTickFont(tick_font)
    if plot_item.titleLabel.text:
        plot_item.setTitle(plot_item.titleLabel.text, color=foreground)


def repolish(widget) -> None:
    """Forces a widget's stylesheet to be re-evaluated."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def fix_toggle_button_width(button, *texts: str) -> None:
    """Prevents a width change on a button whose text changes at runtime
    (e.g. the trigger arm button - "Arm Trigger" vs. "Disarm Trigger" are
    different lengths; toggling used to make the button visibly jump in
    width).

    Sets EVERY given text as a trial, measuring `sizeHint()` each time
    (automatically accounts for icon/padding/border from the currently
    set stylesheet, instead of reproducing those values here by hand) and
    fixes the width to the widest candidate. `texts` must already be
    fully formatted (including e.g. leading spaces for icon spacing) - the
    caller calls this again after EVERY text change (including after a
    language switch), since the width differs per language.
    """
    original_text = button.text()
    widest = 0
    for text in texts:
        button.setText(text)
        widest = max(widest, button.sizeHint().width())
    button.setText(original_text)
    button.setFixedWidth(widest)


def is_position_on_screen(x: int, y: int) -> bool:
    """Checks whether the point `(x, y)` lies on a CURRENTLY connected
    screen (see `QGuiApplication.screenAt`).

    Used when restoring a saved window position (main window in
    `gui/main_window.py`, channel popout windows in `gui/live_view.py`) -
    without this check, a window that was last positioned on a second
    monitor that has since been unplugged could end up at an unreachable
    position (outside all visible screens, e.g. with negative or very
    large coordinates)."""
    return QGuiApplication.screenAt(QPoint(x, y)) is not None


# ---------------------------------------------------------------------- #
# Simple, self-drawn navigation icons
# ---------------------------------------------------------------------- #
#
# `QStyle.standardIcon(...)` would be the obvious alternative, but for the
# icons used here it delivers FIXED-colored pixmaps that don't follow the
# palette - a theme switch would not change the icon color. These icons
# are instead drawn with the current `WindowText` color and recreated on
# every theme switch.


def nav_icon_color() -> QColor:
    """Current foreground color for navigation icons (follows the palette)."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None:
        return app.palette().color(QPalette.ColorRole.WindowText)
    return QColor(0, 0, 0)


def disabled_text_color() -> QColor:
    """Theme-dependent text color for disabled/unavailable entries (see
    `_build_light_palette`/`_build_dark_palette`,
    `QPalette.ColorGroup.Disabled`).

    Set as an EXPLICIT foreground color on individual `QTreeWidgetItem`s
    (e.g. `gui/widgets/channel_table.py::HardwareChannelPickerDialog` for
    "already assigned"/unsupported channels, `gui/setup_view.py`'s device
    tree for unsupported modules) - Qt does NOT reliably apply the
    disabled color group automatically just because a single item loses
    its `ItemIsEnabled` flag (unlike a fully disabled widget); the item
    would otherwise still look like a normal, enabled entry despite the
    flag being set.
    """
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None:
        return app.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
    return QColor(150, 150, 150)


def _new_icon_pixmap(size: int) -> tuple[QPixmap, QPainter]:
    """Creates an empty pixmap for a self-drawn icon.

    Allocated in device pixels (not logical pixels) and marked with the
    current `devicePixelRatio()` - otherwise the icons appear visibly
    pixelated at Windows scaling >100% (here: 250%), because Qt upscales
    the otherwise too-small pixmap when drawing. Drawing code in the
    draw_*_icon() functions stays unchanged (still uses `size` in logical
    units) - QPainter automatically scales based on the target pixmap's
    devicePixelRatio.
    """
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    dpr = app.devicePixelRatio() if app is not None else 1.0
    physical_size = max(1, round(size * dpr))

    pixmap = QPixmap(physical_size, physical_size)
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    return pixmap, painter


def draw_gear_icon(size: int = 36) -> QPixmap:
    """Gear symbol (Setup/Configuration).

    Blocky, rectangular teeth instead of thin spokes - thin lines
    radiating out from the circle look more like a sun at small sizes.
    """
    pixmap, painter = _new_icon_pixmap(size)
    color = nav_icon_color()
    center = size / 2
    body_radius = size * 0.30
    hole_radius = size * 0.13
    tooth_len = size * 0.11
    tooth_width = size * 0.15

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)

    # Teeth: small rectangles, distributed radially around the center.
    for i in range(8):
        painter.save()
        painter.translate(center, center)
        painter.rotate(i * 45)
        painter.drawRect(QRectF(body_radius, -tooth_width / 2, tooth_len, tooth_width))
        painter.restore()

    # Gear ring body.
    painter.drawEllipse(QPointF(center, center), body_radius, body_radius)

    # Punch out the center hole so a real ring results.
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    painter.drawEllipse(QPointF(center, center), hole_radius, hole_radius)

    painter.end()
    return pixmap


def draw_play_icon(size: int = 36, y_offset: float = 0.0, color: QColor | None = None) -> QPixmap:
    """Play triangle (Live View).

    `y_offset` allows a small vertical fine-adjustment for buttons.
    `color` overrides the otherwise theme-dependent `nav_icon_color()` -
    needed for buttons with a fixed (non-theme-dependent) background, e.g.
    the green start button in `gui/setup_view.py`, where black (the light
    mode foreground color) would barely be visible on dark green.
    """
    pixmap, painter = _new_icon_pixmap(size)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color if color is not None else nav_icon_color())
    m = size * 0.24
    y = float(y_offset)
    triangle = QPolygonF(
        [
            QPointF(m, m * 0.7 + y),
            QPointF(m, size - m * 0.7 + y),
            QPointF(size - m * 0.75, size / 2 + y),
        ]
    )
    painter.drawPolygon(triangle)
    painter.end()
    return pixmap


def draw_record_icon(size: int = 36, y_offset: float = 0.0, color: QColor | None = None) -> QPixmap:
    """Filled circle - the customary record symbol (video/audio devices),
    for the button that starts a measurement WITH saving (see
    `gui/setup_view.py`/`gui/live_view.py`) - visually distinguishes it
    from the pure live-display button (`draw_play_icon`).

    `y_offset`/`color` as in `draw_play_icon`.
    """
    pixmap, painter = _new_icon_pixmap(size)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color if color is not None else nav_icon_color())
    m = size * 0.2
    diameter = size - 2 * m
    painter.drawEllipse(QRectF(m, m + y_offset, diameter, diameter))
    painter.end()
    return pixmap


def draw_trigger_icon(size: int = 36, y_offset: float = 0.0, color: QColor | None = None) -> QPixmap:
    """Lightning bolt symbol for the trigger arm button
    (`_trigger_arm_button` in `gui/setup_view.py`/`gui/live_view.py`) -
    the customary sign for "trigger" (e.g. also used on oscilloscopes).

    `y_offset`/`color` as in `draw_play_icon` - the button has a fixed
    gray background independent of the theme, so the icon likewise always
    needs white instead of the otherwise theme-dependent
    `nav_icon_color()`.
    """
    pixmap, painter = _new_icon_pixmap(size)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color if color is not None else nav_icon_color())
    m = size * 0.14
    w = size - 2 * m
    h = size - 2 * m
    y = float(y_offset)
    bolt = QPolygonF(
        [
            QPointF(m + w * 0.58, m + y),
            QPointF(m + w * 0.14, m + h * 0.58 + y),
            QPointF(m + w * 0.42, m + h * 0.58 + y),
            QPointF(m + w * 0.32, m + h * 1.0 + y),
            QPointF(m + w * 0.86, m + h * 0.38 + y),
            QPointF(m + w * 0.55, m + h * 0.38 + y),
        ]
    )
    painter.drawPolygon(bolt)
    painter.end()
    return pixmap


def draw_magnifier_icon(size: int = 36) -> QPixmap:
    """Magnifying glass with a schematic time graph inside it (Analysis).

    The graph is clipped to the lens (so it really sits "in" the lens, not
    just next to it). The handle runs exactly radially outward from the
    circle's edge (45° direction from the lens center), instead of being
    offset tangentially.
    """
    pixmap, painter = _new_icon_pixmap(size)
    color = nav_icon_color()

    lens_center = QPointF(size * 0.44, size * 0.44)
    lens_radius = size * 0.32

    # Schematic time graph (zigzag line), clipped to the lens.
    inner_radius = lens_radius * 0.82
    clip_path = QPainterPath()
    clip_path.addEllipse(lens_center, inner_radius, inner_radius)
    painter.save()
    painter.setClipPath(clip_path)
    graph_pen = QPen(color)
    graph_pen.setWidthF(size * 0.045)
    graph_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    graph_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(graph_pen)
    r = inner_radius * 0.95
    cx, cy = lens_center.x(), lens_center.y()
    graph_points = [
        QPointF(cx - r, cy + r * 0.35),
        QPointF(cx - r * 0.45, cy - r * 0.55),
        QPointF(cx - r * 0.05, cy + r * 0.45),
        QPointF(cx + r * 0.45, cy - r * 0.65),
        QPointF(cx + r, cy + r * 0.05),
    ]
    for p1, p2 in zip(graph_points, graph_points[1:]):
        painter.drawLine(p1, p2)
    painter.restore()

    # Lens rim.
    lens_pen = QPen(color)
    lens_pen.setWidthF(size * 0.09)
    lens_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(lens_pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(lens_center, lens_radius, lens_radius)

    # Handle: starts exactly on the circle's edge and continues radially
    # (45°) outward - the same direction as the line from the lens center
    # to the start point, so no offset/tangent.
    angle = math.radians(45)
    direction = QPointF(math.cos(angle), math.sin(angle))
    handle_start = QPointF(
        lens_center.x() + lens_radius * direction.x(),
        lens_center.y() + lens_radius * direction.y(),
    )
    handle_length = size * 0.26
    handle_end = QPointF(
        handle_start.x() + handle_length * direction.x(),
        handle_start.y() + handle_length * direction.y(),
    )
    painter.drawLine(handle_start, handle_end)

    painter.end()
    return pixmap


def draw_plus_icon(size: int = 16) -> QPixmap:
    """Plus symbol for action buttons (e.g. add channel)."""
    pixmap, painter = _new_icon_pixmap(size)
    pen = QPen(nav_icon_color())
    pen.setWidthF(max(1.8, size * 0.14))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    c = size / 2
    m = size * 0.24
    painter.drawLine(QPointF(c, m), QPointF(c, size - m))
    painter.drawLine(QPointF(m, c), QPointF(size - m, c))
    painter.end()
    return pixmap


def draw_minus_icon(size: int = 16) -> QPixmap:
    """Minus symbol for action buttons (e.g. remove channel)."""
    pixmap, painter = _new_icon_pixmap(size)
    pen = QPen(nav_icon_color())
    pen.setWidthF(max(1.8, size * 0.14))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    c = size / 2
    m = size * 0.24
    painter.drawLine(QPointF(m, c), QPointF(size - m, c))
    painter.end()
    return pixmap


def draw_stop_icon(size: int = 16, y_offset: float = 0.0, color: QColor | None = None) -> QPixmap:
    """Square stop symbol for cancel/stop actions.

    `color` as in `draw_play_icon` - for buttons with a fixed (non-theme-
    dependent) background, ALWAYS white instead of the otherwise theme-
    dependent `nav_icon_color()`; without an override (e.g. the stop
    buttons in `gui/setup_view.py`/`gui/live_view.py`, which normally
    follow the QPalette), the icon falls back to `nav_icon_color()`.
    """
    pixmap, painter = _new_icon_pixmap(size)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color if color is not None else nav_icon_color())
    m = size * 0.23
    side = size - 2 * m
    painter.drawRect(QRectF(m, m + y_offset, side, side))
    painter.end()
    return pixmap


def draw_ellipsis_icon(size: int = 16) -> QPixmap:
    """Three-dot symbol ("…") indicating that a button opens its own
    selection window instead of a direct action (e.g. hardware channel/
    signal type selection in `gui/widgets/channel_table.py`)."""
    pixmap, painter = _new_icon_pixmap(size)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(nav_icon_color())
    dot_radius = max(1.0, size * 0.09)
    center_y = size / 2
    for i in (0.22, 0.5, 0.78):
        painter.drawEllipse(QPointF(size * i, center_y), dot_radius, dot_radius)
    painter.end()
    return pixmap


def draw_fft_icon(size: int = 36) -> QPixmap:
    """Spectrum symbol (bars of varying height) for FFT analysis."""
    pixmap, painter = _new_icon_pixmap(size)
    color = nav_icon_color()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)

    base_y = size * 0.82
    heights = [0.30, 0.55, 0.78, 0.45, 0.62, 0.22]
    bar_width = size * 0.10
    gap = size * 0.04
    total_width = len(heights) * bar_width + (len(heights) - 1) * gap
    x = (size - total_width) / 2
    for h in heights:
        bar_height = size * h
        painter.drawRect(QRectF(x, base_y - bar_height, bar_width, bar_height))
        x += bar_width + gap

    painter.end()
    return pixmap


def _draw_filter_response_icon(size: int, rising: bool) -> QPixmap:
    """Shared base for the lowpass/highpass icons: schematic amplitude
    response (a curve that falls/rises on one side)."""
    pixmap, painter = _new_icon_pixmap(size)
    color = nav_icon_color()

    axis_pen = QPen(color)
    axis_pen.setWidthF(size * 0.045)
    axis_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(axis_pen)
    m = size * 0.16
    painter.drawLine(QPointF(m, size - m), QPointF(size - m, size - m))
    painter.drawLine(QPointF(m, size - m), QPointF(m, m))

    curve_pen = QPen(color)
    curve_pen.setWidthF(size * 0.09)
    curve_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    curve_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(curve_pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    high = size * 0.28
    low = size - m
    mid_x = size * 0.55
    path = QPainterPath()
    if rising:
        path.moveTo(m, low)
        path.lineTo(mid_x * 0.75, low)
        path.cubicTo(mid_x, low, mid_x * 1.05, high, size - m * 1.3, high)
    else:
        path.moveTo(m, high)
        path.lineTo(mid_x * 0.75, high)
        path.cubicTo(mid_x, high, mid_x * 1.05, low, size - m * 1.3, low)
    painter.drawPath(path)

    painter.end()
    return pixmap


def draw_lowpass_icon(size: int = 36) -> QPixmap:
    """Amplitude response symbol for a lowpass filter (falls off to the right)."""
    return _draw_filter_response_icon(size, rising=False)


def draw_highpass_icon(size: int = 36) -> QPixmap:
    """Amplitude response symbol for a highpass filter (rises to the right)."""
    return _draw_filter_response_icon(size, rising=True)


def draw_smoothing_icon(size: int = 36) -> QPixmap:
    """Symbol for the smoothing filter: a noisy line above a smoothed
    curve."""
    pixmap, painter = _new_icon_pixmap(size)
    color = nav_icon_color()

    smooth_pen = QPen(color)
    smooth_pen.setWidthF(size * 0.09)
    smooth_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    smooth_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(smooth_pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    m = size * 0.16
    y_mid = size * 0.6
    smooth_path = QPainterPath()
    smooth_path.moveTo(m, y_mid + size * 0.12)
    smooth_path.cubicTo(size * 0.35, y_mid - size * 0.22, size * 0.65, y_mid + size * 0.22, size - m, y_mid - size * 0.12)
    painter.drawPath(smooth_path)

    noisy_pen = QPen(color)
    noisy_pen.setWidthF(size * 0.035)
    noisy_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    noisy_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(noisy_pen)
    y0 = size * 0.28
    jags = [0.30, 0.18, 0.36, 0.12, 0.32, 0.20, 0.34]
    step = (size - 2 * m) / (len(jags) - 1)
    noisy_path = QPainterPath()
    noisy_path.moveTo(m, y0 + size * jags[0])
    for i, j in enumerate(jags[1:], start=1):
        noisy_path.lineTo(m + step * i, y0 + size * j)
    painter.drawPath(noisy_path)

    painter.end()
    return pixmap


def set_theme(theme: str) -> None:
    """Sets the theme to "light" or "dark" and applies it immediately.

    Updates the `QApplication` palette (automatically affects all
    standard Qt widgets) and the global PyQtGraph color options (affects
    plots created from now on) - and notifies registered views via
    `connect_theme_changed` so they can recolor their existing plots
    themselves.
    """
    global _current_theme
    if theme not in _PALETTES or theme == _current_theme:
        return
    _current_theme = theme

    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None:
        app.setPalette(_PALETTES[theme]())

    pg.setConfigOption("background", _PLOT_CONTAINER_COLORS[theme])
    pg.setConfigOption("foreground", _PLOT_COLORS[theme]["foreground"])

    _get_signals().theme_changed.emit(theme)


# ---------------------------------------------------------------------- #
# Live theme switch signal
# ---------------------------------------------------------------------- #
#
# Lazily constructed for the same reason as in `gui/i18n.py`: this module
# can be imported before `QApplication` exists.

_signals: Optional["_ThemeSignals"] = None


def _get_signals() -> "_ThemeSignals":
    global _signals
    if _signals is None:
        from PyQt6.QtCore import QObject, pyqtSignal

        class _ThemeSignals(QObject):
            theme_changed = pyqtSignal(str)

        _signals = _ThemeSignals()
    return _signals


def connect_theme_changed(slot: Callable[[], None]) -> None:
    """Registers `slot` (typically `view.retheme_plots`), which is called
    on every theme change."""
    _get_signals().theme_changed.connect(slot)
