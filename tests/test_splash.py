"""
tests/test_splash.py

Tests for the startup splash (`gui/splash.py`) and the theme it is built
with (`config/settings.py::read_stored_theme`).

Two of these guard defects rather than features:

    * the splash used to be filled with a hardcoded white, while the
      stored theme was only applied afterwards - so the application
      flashed white on every start under the dark theme,
    * the minimum display time was a plain `time.sleep()`, which blocks
      the event loop: the splash stood frozen for exactly the time it
      was meant to be looked at, and no animation could run.
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication

# Module-level reference, see tests/test_axis_labels.py for why.
_APP: QApplication | None = None


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


class ReadStoredThemeTest(unittest.TestCase):
    """Reads the theme without building an `AppSettings` - and must never
    raise, since it runs before there is any window to report in."""

    def _with_settings_file(self, content: str | None):
        directory = TemporaryDirectory()
        path = Path(directory.name)
        if content is not None:
            (path / "settings.json").write_text(content, encoding="utf-8")
        return directory, patch("config.settings.get_config_directory", return_value=path)

    def test_reads_the_stored_theme(self) -> None:
        from config.settings import read_stored_theme

        directory, patched = self._with_settings_file(json.dumps({"theme": "dark"}))
        with directory, patched:
            self.assertEqual(read_stored_theme(), "dark")

    def test_missing_file_falls_back(self) -> None:
        from config.settings import read_stored_theme

        directory, patched = self._with_settings_file(None)
        with directory, patched:
            self.assertEqual(read_stored_theme(), "light")

    def test_damaged_file_falls_back(self) -> None:
        """A broken settings file must not keep the application from
        starting."""
        from config.settings import read_stored_theme

        directory, patched = self._with_settings_file("{ this is not json")
        with directory, patched:
            self.assertEqual(read_stored_theme(), "light")

    def test_unknown_value_falls_back(self) -> None:
        from config.settings import read_stored_theme

        directory, patched = self._with_settings_file(json.dumps({"theme": "neon"}))
        with directory, patched:
            self.assertEqual(read_stored_theme(), "light")


class StartupSplashTest(unittest.TestCase):
    def setUp(self) -> None:
        from gui.theme import init_theme

        self.app = _app()
        init_theme(self.app)

    def _splash(self, steps: int = 4):
        from gui.splash import StartupSplash

        logo = QPixmap(420, 300)
        logo.fill()
        return StartupSplash(logo, "v0.1", steps)

    def test_canvas_leaves_room_for_trace_and_footer(self) -> None:
        """Otherwise the trace band and progress bar would be painted on
        top of the logo."""
        from gui.splash import _FOOTER_HEIGHT, _TRACE_HEIGHT

        splash = self._splash()

        self.assertGreaterEqual(
            splash.pixmap().height(), 300 + _TRACE_HEIGHT + _FOOTER_HEIGHT
        )
        self.assertGreater(splash.pixmap().width(), 420)

        splash.deleteLater()
        self.app.processEvents()

    def test_steps_advance_and_stop_at_the_total(self) -> None:
        """A miscounted `_SPLASH_STEPS` in main.py must not overfill the
        bar or divide past 100%."""
        splash = self._splash(steps=2)

        splash.set_step("one")
        self.assertEqual(splash._step, 1)
        splash.set_step("two")
        splash.set_step("three")
        self.assertEqual(splash._step, 2)
        self.assertEqual(splash._message, "three")

        splash.deleteLater()
        self.app.processEvents()

    def test_wait_keeps_the_event_loop_running(self) -> None:
        """The actual point of `wait()` over `time.sleep()` - a timer
        scheduled beforehand has to fire DURING the wait."""
        splash = self._splash()
        fired = []
        QTimer.singleShot(20, lambda: fired.append(True))

        started = time.monotonic()
        splash.wait(0.25)
        elapsed = time.monotonic() - started

        self.assertTrue(fired, "event loop was blocked - animation would freeze")
        self.assertGreaterEqual(elapsed, 0.2)

        splash.deleteLater()
        self.app.processEvents()

    def test_wait_returns_immediately_for_a_past_deadline(self) -> None:
        """Startup was already slower than the minimum display time."""
        splash = self._splash()

        started = time.monotonic()
        splash.wait(-1.0)

        self.assertLess(time.monotonic() - started, 0.05)

        splash.deleteLater()
        self.app.processEvents()

    def test_colors_follow_the_active_theme(self) -> None:
        """The splash is built from `gui/theme.py`, not from a hardcoded
        white - that was the white flash under the dark theme."""
        from gui.theme import plot_background_color, set_theme

        seen = {}
        for theme in ("light", "dark"):
            set_theme("dark" if theme == "light" else "light")
            set_theme(theme)
            splash = self._splash()
            seen[theme] = splash._background.name()
            self.assertEqual(seen[theme], plot_background_color())
            splash.deleteLater()
        self.app.processEvents()

        self.assertNotEqual(seen["light"], seen["dark"])


class SplashStepCountTest(unittest.TestCase):
    """`main.py` divides the progress bar into `_SPLASH_STEPS` and then
    calls `_set_splash_status()` once per step. If those drift apart the
    bar either never fills or saturates early."""

    def test_step_constant_matches_the_status_calls(self) -> None:
        import re

        source = (Path(__file__).resolve().parent.parent / "main.py").read_text(
            encoding="utf-8"
        )
        declared = int(re.search(r"_SPLASH_STEPS\s*=\s*(\d+)", source).group(1))
        # Only actual calls: matching the bare name would also catch the
        # definition and the mention in the constant's comment.
        calls = len(re.findall(r'_set_splash_status\("', source))
        self.assertEqual(declared, calls)


if __name__ == "__main__":
    unittest.main()


class SkipWaitTest(unittest.TestCase):
    """A click must END the wait, not just hide the splash.

    `QSplashScreen.mousePressEvent()` hides the widget by default while
    `wait()` kept counting - so a click one second in left nothing on
    screen until the main window appeared.
    """

    def setUp(self) -> None:
        from gui.theme import init_theme

        self.app = _app()
        init_theme(self.app)

    def _splash(self):
        from gui.splash import StartupSplash

        logo = QPixmap(420, 300)
        logo.fill()
        return StartupSplash(logo, "v0.1", 4)

    def test_click_ends_the_wait_early(self) -> None:
        from PyQt6.QtCore import QPoint, Qt as QtCore_Qt
        from PyQt6.QtGui import QMouseEvent

        splash = self._splash()
        event = QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            QPoint(5, 5).toPointF(),
            QtCore_Qt.MouseButton.LeftButton,
            QtCore_Qt.MouseButton.LeftButton,
            QtCore_Qt.KeyboardModifier.NoModifier,
        )
        QTimer.singleShot(30, lambda: splash.mousePressEvent(event))

        started = time.monotonic()
        splash.wait(2.0)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0, "click did not cut the wait short")

        splash.deleteLater()
        self.app.processEvents()

    def test_escape_ends_the_wait_early(self) -> None:
        from PyQt6.QtCore import Qt as QtCore_Qt
        from PyQt6.QtGui import QKeyEvent

        splash = self._splash()
        event = QKeyEvent(
            QKeyEvent.Type.KeyPress,
            QtCore_Qt.Key.Key_Escape,
            QtCore_Qt.KeyboardModifier.NoModifier,
        )
        QTimer.singleShot(30, lambda: splash.keyPressEvent(event))

        started = time.monotonic()
        splash.wait(2.0)

        self.assertLess(time.monotonic() - started, 1.0)

        splash.deleteLater()
        self.app.processEvents()

    def test_other_keys_do_not_end_the_wait(self) -> None:
        from PyQt6.QtCore import Qt as QtCore_Qt
        from PyQt6.QtGui import QKeyEvent

        splash = self._splash()
        event = QKeyEvent(
            QKeyEvent.Type.KeyPress,
            QtCore_Qt.Key.Key_A,
            QtCore_Qt.KeyboardModifier.NoModifier,
        )
        QTimer.singleShot(20, lambda: splash.keyPressEvent(event))

        started = time.monotonic()
        splash.wait(0.3)

        self.assertGreaterEqual(time.monotonic() - started, 0.25)

        splash.deleteLater()
        self.app.processEvents()
