"""
tests/test_recording_status.py

The status bar has to say whether a running measurement is being SAVED.

Both cases used to share one message - "Messung 'X' läuft ..." - whether
data was going to disk or the live view was merely displaying it. For a
measurement application that is the one state worth being sure about:
believing you are recording when you are not costs the measurement.
"""

from __future__ import annotations

import unittest

from PyQt6.QtWidgets import QApplication, QLabel

from data.models import MeasurementConfig

# Module-level reference, see tests/test_axis_labels.py for why.
_APP: QApplication | None = None


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


class _StubWindow:
    """Only the two widgets the status methods touch.

    Built without `MainWindow.__init__` - that pulls in the controller,
    every view and a device search, none of which this behaviour needs.
    """

    def __init__(self) -> None:
        self._recording_indicator = QLabel()
        self._status_label = QLabel()


def _bind(name: str):
    from gui.main_window import MainWindow

    return getattr(MainWindow, name)


class RecordingStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        _app()
        self.window = _StubWindow()
        self._set = _bind("_set_measurement_status")
        self._clear = _bind("_clear_measurement_status")

    def _config(self, save: bool) -> MeasurementConfig:
        return MeasurementConfig(name="Messung_001", sample_rate_hz=1000.0, channels=[], save_to_disk=save)

    def test_recording_shows_the_dot(self) -> None:
        self._set(self.window, self._config(save=True))

        self.assertTrue(self.window._recording_indicator.isVisibleTo(None) is not None)
        self.assertFalse(self.window._recording_indicator.isHidden())

    def test_live_only_hides_the_dot(self) -> None:
        self._set(self.window, self._config(save=False))

        self.assertTrue(self.window._recording_indicator.isHidden())

    def test_the_two_states_do_not_share_a_message(self) -> None:
        self._set(self.window, self._config(save=True))
        recording = self.window._status_label.text()

        self._set(self.window, self._config(save=False))
        live_only = self.window._status_label.text()

        self.assertNotEqual(recording, live_only)
        self.assertIn("Messung_001", recording)
        self.assertIn("Messung_001", live_only)

    def test_stopping_clears_dot_and_text(self) -> None:
        """The dot must not outlive the measurement."""
        self._set(self.window, self._config(save=True))
        self._clear(self.window)

        self.assertTrue(self.window._recording_indicator.isHidden())
        self.assertNotIn("Messung_001", self.window._status_label.text())

    def test_both_messages_exist_in_both_languages(self) -> None:
        from gui.i18n import _translations

        for language in ("de", "en"):
            for key in ("measurement_recording_named", "measurement_live_only_named"):
                with self.subTest(language=language, key=key):
                    self.assertIn(key, _translations[language])
                    self.assertIn("{name}", _translations[language][key])


if __name__ == "__main__":
    unittest.main()
