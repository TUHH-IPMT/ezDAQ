"""
tests/test_update_notification.py

Tests that `gui/main_window.py`'s update-check handlers show (or
suppress) the right dialog for the right situation - in particular the
one behavior `tests/test_update_checker.py` cannot cover on its own,
since that file deliberately stays free of Qt: that an outdated version
detected during the SILENT automatic startup check (`_start_update_check
(silent=True)` in `MainWindow.__init__`) still surfaces the
"update available" dialog. Only "already up to date" and "check failed"
are meant to stay silent on startup - see `_start_update_check`'s
docstring.

Builds a bare `MainWindow` via `QMainWindow.__init__` (skipping
`MainWindow.__init__`, which needs a running `MeasurementController`/
`ConfigurationManager`/hardware): the handlers under test only touch
`self` as a `QWidget` (as the parent of a `QMessageBox`) plus the two
`_update_check_*` attributes set directly below, so nothing else needs
to exist.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication, QMainWindow

from core.update_checker import UpdateCheckResult
from gui.main_window import MainWindow

# Module-level reference, see tests/test_splash.py for why.
_APP: QApplication | None = None


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def _bare_window() -> MainWindow:
    """A `MainWindow` with only its `QMainWindow` base initialized - see
    module docstring for why `MainWindow.__init__` is skipped."""
    _app()
    window = QMainWindow.__new__(MainWindow)
    QMainWindow.__init__(window)
    return window


_UPDATE_RESULT = UpdateCheckResult(
    update_available=True,
    current_version="0.5.0",
    latest_version="v9.9.9",
    release_url="https://github.com/TUHH-IPMT/ezDAQ/releases/tag/v9.9.9",
)
_UP_TO_DATE_RESULT = UpdateCheckResult(
    update_available=False,
    current_version="0.5.0",
    latest_version="v0.5.0",
    release_url="https://github.com/TUHH-IPMT/ezDAQ/releases/tag/v0.5.0",
)


class StartupSilentCheckTest(unittest.TestCase):
    """Exactly the automatic once-per-startup path: `silent=True`."""

    def test_update_available_shows_the_dialog(self) -> None:
        window = _bare_window()
        window._update_check_worker = object()  # any non-None sentinel
        window._update_check_silent = True

        with patch.object(window, "_show_update_available_dialog") as show_dialog:
            window._on_update_check_succeeded(_UPDATE_RESULT)

        show_dialog.assert_called_once_with(_UPDATE_RESULT)
        # Cleared so a later manual check is not mistaken for "already
        # running" (see `_start_update_check`).
        self.assertIsNone(window._update_check_worker)

    def test_already_up_to_date_shows_nothing(self) -> None:
        window = _bare_window()
        window._update_check_worker = object()
        window._update_check_silent = True

        with patch("gui.main_window.QMessageBox") as message_box:
            window._on_update_check_succeeded(_UP_TO_DATE_RESULT)

        message_box.information.assert_not_called()

    def test_failure_is_logged_but_shows_nothing(self) -> None:
        window = _bare_window()
        window._update_check_worker = object()
        window._update_check_silent = True

        with patch("gui.main_window.QMessageBox") as message_box:
            with self.assertLogs("gui.main_window", level="WARNING"):
                window._on_update_check_failed("no network")

        message_box.warning.assert_not_called()


class ManualCheckTest(unittest.TestCase):
    """The "Hilfe -> Nach Updates suchen..." action: `silent=False`,
    every outcome is reported."""

    def test_update_available_shows_the_dialog(self) -> None:
        window = _bare_window()
        window._update_check_worker = object()
        window._update_check_silent = False

        with patch.object(window, "_show_update_available_dialog") as show_dialog:
            window._on_update_check_succeeded(_UPDATE_RESULT)

        show_dialog.assert_called_once_with(_UPDATE_RESULT)

    def test_already_up_to_date_is_reported(self) -> None:
        window = _bare_window()
        window._update_check_worker = object()
        window._update_check_silent = False

        with patch("gui.main_window.QMessageBox") as message_box:
            window._on_update_check_succeeded(_UP_TO_DATE_RESULT)

        message_box.information.assert_called_once()

    def test_failure_is_reported(self) -> None:
        window = _bare_window()
        window._update_check_worker = object()
        window._update_check_silent = False

        with patch("gui.main_window.QMessageBox") as message_box:
            window._on_update_check_failed("no network")

        message_box.warning.assert_called_once()


class UpdateAvailableDialogTest(unittest.TestCase):
    """`_show_update_available_dialog` itself: the "open release page"
    button must actually open `result.release_url`."""

    def test_choosing_download_opens_the_release_url(self) -> None:
        window = _bare_window()

        with patch("gui.main_window.QMessageBox") as message_box_cls, patch(
            "gui.main_window.QDesktopServices"
        ) as desktop_services:
            box = message_box_cls.return_value
            download_button = object()
            box.addButton.return_value = download_button
            box.clickedButton.return_value = download_button

            window._show_update_available_dialog(_UPDATE_RESULT)

            box.exec.assert_called_once()
            self.assertTrue(desktop_services.openUrl.called)
            (opened_url,), _kwargs = desktop_services.openUrl.call_args
            self.assertEqual(opened_url.toString(), _UPDATE_RESULT.release_url)

    def test_choosing_close_does_not_open_anything(self) -> None:
        window = _bare_window()

        with patch("gui.main_window.QMessageBox") as message_box_cls, patch(
            "gui.main_window.QDesktopServices"
        ) as desktop_services:
            box = message_box_cls.return_value
            download_button = object()
            box.addButton.return_value = download_button
            box.clickedButton.return_value = None  # "Close" was clicked instead

            window._show_update_available_dialog(_UPDATE_RESULT)

        desktop_services.openUrl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
