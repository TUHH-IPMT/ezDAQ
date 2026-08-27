"""
gui/splash.py

Startup splash screen (see `main.py`).

Beyond the logo it shows three things that the previous, purely static
splash did not:

    * a measurement trace sweeping across a small plot band - a data
      acquisition application showing what it is for while it loads,
    * a determinate progress bar over the actual initialization steps,
      instead of only a line of text,
    * the application version, so it can be read off a running start
      without hunting for the About dialog.

Two details make that possible at all:

    Theme. The splash is built BEFORE the configuration is loaded, so
    the stored theme is read separately beforehand (see
    `config/settings.py::read_stored_theme`). Without it the splash
    would be painted with the default palette and flash white on every
    start under the dark theme.

    Event loop. The startup steps run synchronously, and the minimum
    display time used to be a plain `time.sleep()` - during which Qt
    processes nothing and the splash is a frozen still image. `wait()`
    below therefore spins a local event loop instead, which is also what
    gives the trace its time to actually run.
"""

from __future__ import annotations

import math
import time

from PyQt6.QtCore import (
    QEasingCurve,
    QEventLoop,
    QPointF,
    QRect,
    QPropertyAnimation,
    Qt,
    QTimer,
)
from PyQt6.QtGui import (
    QImage,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import QSplashScreen

# Height of the band holding the animated trace, below the logo.
_TRACE_HEIGHT = 58
# Height of the strip below it holding progress bar, status and version.
_FOOTER_HEIGHT = 46
_PROGRESS_HEIGHT = 4
_MARGIN = 16
# Gap between the logo (or its card) and the trace band. Without it
# the card's rounded bottom edge reaches into the band and the curve
# visibly cuts across it.
_LOGO_GAP = 12

# ~60 fps. The splash only has to repaint the trace band and footer
# (logo and card are baked into the base pixmap), so this stays cheap.
_FRAME_INTERVAL_MS = 16
# Full sweeps of the trace per second.
_TRACE_SPEED_HZ = 0.5
_TRACE_CYCLES = 2.5
# Distance between path sample points, in pixels.
_TRACE_SAMPLE_PX = 3.0
# Edge length of the duck riding the leading edge of the trace.
_RIDER_SIZE = 30

_FADE_MS = 180


def _dark_ink_share(image: QImage) -> float:
    """Share of the opaque pixels that are very dark.

    Sampled coarsely - this only decides whether a light panel goes
    behind the logo, so a rough figure is plenty."""
    step = max(1, min(image.width(), image.height()) // 96)
    dark = opaque = 0
    for x in range(0, image.width(), step):
        for y in range(0, image.height(), step):
            color = image.pixelColor(x, y)
            if color.alpha() < 128:
                continue
            opaque += 1
            if color.lightness() < 90:
                dark += 1
    return dark / opaque if opaque else 0.0


def _opaque_bounds(image: QImage) -> QRect | None:
    """Bounding box of everything not fully transparent, or `None` for
    an image without an alpha channel or with nothing opaque in it.

    Sampled on a coarse grid and then padded by one step: an exact
    per-pixel scan of a 1374x1145 image on every start would cost more
    than the crop is worth, and a pixel of slack is invisible."""
    if not image.hasAlphaChannel():
        return None
    step = max(1, min(image.width(), image.height()) // 128)
    xs, ys = [], []
    for x in range(0, image.width(), step):
        for y in range(0, image.height(), step):
            if image.pixelColor(x, y).alpha() > 8:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    left = max(0, min(xs) - step)
    top = max(0, min(ys) - step)
    right = min(image.width() - 1, max(xs) + step)
    bottom = min(image.height() - 1, max(ys) + step)
    return QRect(left, top, right - left + 1, bottom - top + 1)


class StartupSplash(QSplashScreen):
    """Splash with an animated trace, progress bar and version label.

    Colors come from `gui/theme.py` and therefore follow whichever theme
    was restored before this was constructed.
    """

    def __init__(
        self,
        logo: QPixmap,
        version: str,
        step_count: int,
        parent=None,
    ) -> None:
        from gui.theme import (
            curve_color,
            disabled_text_color,
            plot_background_color,
            plot_foreground_color,
        )

        self._logo_height = logo.height()
        self._version = version
        self._step_count = max(1, step_count)
        self._step = 0
        self._message = ""
        self._phase = 0.0
        self._last_frame = time.monotonic()

        self._background = QColor(plot_background_color())
        self._text_color = QColor(plot_foreground_color())
        self._muted_color = disabled_text_color()
        self._trace_color = QColor(curve_color())
        # Grid and progress trough: the foreground at low opacity rather
        # than a second hardcoded pair of colors, so both themes stay
        # consistent without a lookup table of their own.
        self._faint_color = QColor(self._text_color)
        self._faint_color.setAlpha(38)

        # The duck rides the leading edge of the trace.
        self._rider = self._load_rider()

        # A cut-out logo sits directly on the themed background. Only an
        # OPAQUE one gets a rounded white card behind it: without an alpha
        # channel it cannot blend into a dark background, and a white
        # block butting against a dark strip looks like a mistake rather
        # than a design. See `_logo_card_color`.
        width = logo.width() + 2 * _MARGIN
        self._trace_top = _MARGIN + logo.height() + _LOGO_GAP
        height = self._trace_top + _TRACE_HEIGHT + _FOOTER_HEIGHT
        canvas = QPixmap(width, height)
        canvas.fill(self._background)

        # Logo and card are painted ONCE into the base pixmap, not per
        # frame in `drawContents`: they never change, and re-blitting a
        # 420px logo on every one of ~60 frames a second was the bulk of
        # the work behind the visibly jerky duck.
        card = self._logo_card_color(logo)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if card is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(card)
            painter.drawRoundedRect(
                _MARGIN // 2,
                _MARGIN // 2,
                width - _MARGIN,
                logo.height() + _MARGIN,
                6,
                6,
            )
        painter.drawPixmap(_MARGIN, _MARGIN, logo)
        painter.end()
        super().__init__(canvas)

        # Frameless is already implied by QSplashScreen; the translucent
        # background lets the fade animation blend against the desktop
        # instead of against a black rectangle.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

        self._timer = QTimer(self)
        self._timer.setInterval(_FRAME_INTERVAL_MS)
        self._timer.timeout.connect(self._on_frame)

        self._fade: QPropertyAnimation | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def show(self) -> None:  # type: ignore[override]
        """Shows the splash, fading it in and starting the animation."""
        self.setWindowOpacity(0.0)
        super().show()
        # Reset here, not in __init__: construction and show() can be
        # seconds apart, and the duck would jump forward by that gap.
        self._last_frame = time.monotonic()
        self._timer.start()
        self._fade_to(1.0)

    def set_step(self, message: str) -> None:
        """Advances the progress bar by one step and shows `message`.

        Replaces `QSplashScreen.showMessage()`: that draws only text, and
        the caller had to force a repaint by hand after every step. Here
        the running animation repaints anyway.
        """
        self._step = min(self._step + 1, self._step_count)
        self._message = message
        self.repaint()

    def wait(self, seconds: float) -> None:
        """Keeps the splash up for `seconds`, WITH a running event loop.

        A `time.sleep()` here would freeze the animation for exactly the
        time it is supposed to be watched.
        """
        if seconds <= 0:
            return
        loop = QEventLoop()
        QTimer.singleShot(int(seconds * 1000), loop.quit)
        loop.exec()

    def finish_with_fade(self, window) -> None:
        """Fades the splash out over `window`, then hands focus to it.

        `finish()` alone would make it vanish abruptly - which is exactly
        the moment a fade is worth having, since the main window appears
        underneath at the same time.
        """
        self._timer.stop()
        self._fade_to(0.0)
        loop = QEventLoop()
        QTimer.singleShot(_FADE_MS, loop.quit)
        loop.exec()
        self.finish(window)

    def _logo_card_color(self, logo: QPixmap) -> QColor | None:
        """A light panel behind the logo, or `None` if it can stand on
        the background by itself.

        Needed in two cases:

        * an OPAQUE logo, which carries its own white block and cannot
          blend into a dark background at all,
        * a cut-out logo drawn mostly in DARK ink on a dark background -
          the ezDAQ mark is about half deep navy (measured: 48% of its
          opaque pixels below lightness 90), which all but disappears on
          the dark theme's #232323.

        Under the light theme a cut-out logo needs nothing and gets
        nothing, which is the point of having cut it out."""
        if not logo.hasAlphaChannel():
            return QColor("#ffffff")
        if self._background.lightness() > 128:
            return None
        return (
            QColor("#f7f7f7")
            if _dark_ink_share(logo.toImage()) > 0.25
            else None
        )

    @staticmethod
    def _load_rider() -> QPixmap | None:
        """The small duck riding the trace, or `None` if unavailable.

        Cropped to its opaque bounding box first: the source is a
        cut-out with a wide transparent margin, and scaling that
        untouched would leave the duck floating well above the curve
        with no obvious reason.

        Never raises - a missing or damaged image costs the gimmick, it
        must not keep the application from starting."""
        try:
            from config.settings import get_resource_path

            path = get_resource_path("ezDAQ_logo_full_3.png")
            if not path.exists():
                return None
            image = QImage(str(path))
            if image.isNull():
                return None
            bounds = _opaque_bounds(image)
            if bounds is not None:
                image = image.copy(bounds)
            return QPixmap.fromImage(image).scaledToHeight(
                _RIDER_SIZE, Qt.TransformationMode.SmoothTransformation
            )
        except Exception:
            return None

    def _fade_to(self, target: float) -> None:
        animation = QPropertyAnimation(self, b"windowOpacity", self)
        animation.setDuration(_FADE_MS)
        animation.setStartValue(self.windowOpacity())
        animation.setEndValue(target)
        animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
        animation.start()
        # Held on the instance: a QPropertyAnimation that goes out of
        # scope is garbage-collected mid-flight and the fade never runs.
        self._fade = animation

    def _on_frame(self) -> None:
        # Time-based rather than counting frames: a delayed timer tick
        # would otherwise slow the duck down instead of letting it catch
        # up, which is exactly what reads as stuttering.
        now = time.monotonic()
        self._phase += (now - self._last_frame) * _TRACE_SPEED_HZ
        self._last_frame = now
        # `update()`, not `repaint()`: repaint() paints synchronously on
        # the spot, so a slow frame delays the next tick instead of being
        # coalesced with it.
        self.update()

    # ------------------------------------------------------------------ #
    # Painting
    # ------------------------------------------------------------------ #

    def drawContents(self, painter: QPainter) -> None:  # type: ignore[override]
        """Draws only what changes: the trace, the progress bar, the
        status line and the version.

        Logo and card are already in the base pixmap that QSplashScreen
        blits before calling this (see `__init__`)."""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        width = self.pixmap().width()
        trace_top = self._trace_top
        self._draw_trace(painter, trace_top, width, _TRACE_HEIGHT)
        self._draw_footer(painter, trace_top + _TRACE_HEIGHT, width)

    def _draw_trace(self, painter: QPainter, top: int, width: int, height: int) -> None:
        """A sine sweeping left to right, on a faint grid.

        Drawn as a partial curve growing with `_phase` and starting over,
        the way the live view's sweep display behaves - a still sine
        would read as decoration, this reads as a running measurement.
        """
        left = _MARGIN
        right = width - _MARGIN
        middle = top + height / 2
        amplitude = height / 2 - 8

        painter.setPen(QPen(self._faint_color, 1))
        for i in range(1, 8):
            x = left + (right - left) * i / 8
            painter.drawLine(int(x), top + 4, int(x), top + height - 4)
        for i in range(1, 3):
            y = top + height * i / 3
            painter.drawLine(left, int(y), right, int(y))

        progress = self._phase % 1.0
        # Float, not an integer pixel count: quantizing the leading edge to
        # whole pixels made the duck advance in visible steps.
        span = (right - left) * progress
        if span < 2:
            return

        # A single QPainterPath of QPointF, NOT a chain of drawLine() calls
        # between integer points: rounding every y to a whole pixel turns
        # the sine into a visible staircase, and antialiasing cannot
        # recover a shape that was already quantized away - it only
        # smooths the edges of each little step. Float coordinates let the
        # rasterizer place the curve between pixels, which is what makes
        # it look smooth.
        pen = QPen(self._trace_color, 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        # `drawPath()` FILLS with the current brush - which is still
        # the white of the logo card set above, so without this the
        # area under the sine is painted solid white.
        painter.setBrush(Qt.BrushStyle.NoBrush)

        fade = min(1.0, progress * 4)

        def trace_y(offset: float) -> float:
            """Fades in over the first few pixels so the curve does not jump
            back to full height when the sweep restarts."""
            angle = 2 * math.pi * _TRACE_CYCLES * offset / (right - left)
            return middle - math.sin(angle) * amplitude * fade

        # Sampled every few pixels instead of every single one: the sine is
        # smooth enough that the difference is invisible, and it keeps the
        # path short enough to rebuild on every frame.
        path = QPainterPath()
        path.moveTo(QPointF(left, trace_y(0.0)))
        offset = _TRACE_SAMPLE_PX
        while offset < span:
            path.lineTo(QPointF(left + offset, trace_y(offset)))
            offset += _TRACE_SAMPLE_PX
        # Ends exactly at the leading edge, so the duck sits on the tip
        # rather than a rounded-down sample point.
        path.lineTo(QPointF(left + span, trace_y(span)))
        painter.drawPath(path)

        if self._rider is None:
            return

        # Sits on the leading edge and tilts with the local slope, so it
        # leans into the climbs and descents instead of sliding along
        # flat. The slope is the analytic derivative of the sine above -
        # sampling two neighbouring points would jitter at the turning
        # points, where the difference is smallest.
        angle = 2 * math.pi * _TRACE_CYCLES * span / (right - left)
        slope = (
            -math.cos(angle)
            * amplitude
            * fade
            * (2 * math.pi * _TRACE_CYCLES / (right - left))
        )
        painter.save()
        painter.translate(QPointF(left + span, trace_y(span)))
        painter.rotate(math.degrees(math.atan(slope)))
        painter.drawPixmap(
            QPointF(-_RIDER_SIZE / 2, -float(_RIDER_SIZE)), self._rider
        )
        painter.restore()

    def _draw_footer(self, painter: QPainter, top: int, width: int) -> None:
        left = _MARGIN
        right = width - _MARGIN

        bar_y = top + 6
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._faint_color)
        painter.drawRoundedRect(
            left, bar_y, right - left, _PROGRESS_HEIGHT, 2, 2
        )
        filled = int((right - left) * self._step / self._step_count)
        if filled > 0:
            painter.setBrush(self._trace_color)
            painter.drawRoundedRect(left, bar_y, filled, _PROGRESS_HEIGHT, 2, 2)

        text_y = bar_y + _PROGRESS_HEIGHT + 6
        text_height = _FOOTER_HEIGHT - (text_y - top) - 4

        font = QFont(painter.font())
        painter.setPen(self._text_color)
        painter.setFont(font)
        painter.drawText(
            left,
            text_y,
            right - left,
            text_height,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self._message,
        )

        version_font = QFont(font)
        version_font.setPointSizeF(max(6.0, font.pointSizeF() - 1.0))
        painter.setFont(version_font)
        painter.setPen(self._muted_color)
        painter.drawText(
            left,
            text_y,
            right - left,
            text_height,
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            self._version,
        )
