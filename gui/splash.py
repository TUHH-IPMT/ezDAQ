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

from PyQt6.QtCore import QEasingCurve, QEventLoop, QPropertyAnimation, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QSplashScreen

# Height of the band holding the animated trace, below the logo.
_TRACE_HEIGHT = 58
# Height of the strip below it holding progress bar, status and version.
_FOOTER_HEIGHT = 46
_PROGRESS_HEIGHT = 4
_MARGIN = 16

# ~33 fps. Fast enough to look continuous, slow enough that the repaint
# never competes with the initialization steps it is meant to cover.
_FRAME_INTERVAL_MS = 30
# Full sweeps of the trace per second.
_TRACE_SPEED_HZ = 0.5
_TRACE_CYCLES = 2.5

_FADE_MS = 180


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

        self._logo = logo
        self._version = version
        self._step_count = max(1, step_count)
        self._step = 0
        self._message = ""
        self._phase = 0.0

        self._background = QColor(plot_background_color())
        self._text_color = QColor(plot_foreground_color())
        self._muted_color = disabled_text_color()
        self._trace_color = QColor(curve_color())
        # Grid and progress trough: the foreground at low opacity rather
        # than a second hardcoded pair of colors, so both themes stay
        # consistent without a lookup table of their own.
        self._faint_color = QColor(self._text_color)
        self._faint_color.setAlpha(38)

        # The logo PNG is opaque white with no alpha channel, so under
        # the dark theme it cannot blend into the background. It is
        # therefore placed on a rounded white card with a margin all
        # around - which reads as a deliberate design element instead
        # of a mismatched white block butting against a dark strip. In
        # the light theme the card is barely distinguishable from the
        # background anyway.
        width = logo.width() + 2 * _MARGIN
        height = _MARGIN + logo.height() + _TRACE_HEIGHT + _FOOTER_HEIGHT
        canvas = QPixmap(width, height)
        canvas.fill(self._background)
        self._logo_card = QColor("#ffffff")
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
        self._phase += _FRAME_INTERVAL_MS / 1000.0 * _TRACE_SPEED_HZ
        self.repaint()

    # ------------------------------------------------------------------ #
    # Painting
    # ------------------------------------------------------------------ #

    def drawContents(self, painter: QPainter) -> None:  # type: ignore[override]
        """Draws logo, trace, progress bar, status and version.

        Everything is painted here rather than baked into the splash
        pixmap: the trace changes every frame, and the progress bar and
        status change per step.
        """
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        width = self.pixmap().width()

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._logo_card)
        painter.drawRoundedRect(
            _MARGIN // 2,
            _MARGIN // 2,
            width - _MARGIN,
            self._logo.height() + _MARGIN,
            6,
            6,
        )
        painter.drawPixmap(_MARGIN, _MARGIN, self._logo)

        trace_top = _MARGIN + self._logo.height()
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
        visible = int((right - left) * progress)
        if visible < 2:
            return

        painter.setPen(QPen(self._trace_color, 2))
        previous = None
        for offset in range(visible + 1):
            x = left + offset
            angle = 2 * math.pi * _TRACE_CYCLES * offset / (right - left)
            # Fades in over the first few pixels so the curve does not
            # jump back to full height when the sweep restarts.
            y = middle - math.sin(angle) * amplitude * min(1.0, progress * 4)
            current = (x, int(y))
            if previous is not None:
                painter.drawLine(previous[0], previous[1], current[0], current[1])
            previous = current

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
