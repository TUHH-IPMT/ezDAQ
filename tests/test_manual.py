"""
tests/test_manual.py

Tests for opening the user manual from Help -> "User Manual (PDF)" / F1
(`gui/manual.py`): the PDF of the current UI language is opened, the
other language is used if that one is missing, and a missing manual is
reported instead of failing silently.

No PDF viewer is started - `QDesktopServices.openUrl` is replaced.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PyQt6.QtWidgets import QApplication

_APP: QApplication | None = None


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


class ManualTest(unittest.TestCase):
    def setUp(self) -> None:
        from gui import manual

        _app()
        self.manual = manual
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "doc" / "manual").mkdir(parents=True)
        patcher = mock.patch.object(
            manual, "get_app_path", lambda *parts: self.root.joinpath(*parts)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _create(self, lang: str) -> Path:
        path = self.root / "doc" / "manual" / self.manual.MANUAL_FILES[lang]
        path.write_bytes(b"%PDF-1.4")
        return path

    def test_manual_of_the_current_language(self) -> None:
        de, en = self._create("de"), self._create("en")
        self.assertEqual(self.manual.manual_path("de"), de)
        self.assertEqual(self.manual.manual_path("en"), en)

    def test_falls_back_to_the_other_language(self) -> None:
        en = self._create("en")
        self.assertEqual(self.manual.manual_path("de"), en)

    def test_none_without_any_manual(self) -> None:
        self.assertIsNone(self.manual.manual_path("de"))

    def test_open_hands_the_pdf_to_the_system_viewer(self) -> None:
        de = self._create("de")
        with mock.patch.object(self.manual, "get_language", return_value="de"), \
                mock.patch.object(self.manual.QDesktopServices, "openUrl", return_value=True) as open_url:
            self.assertTrue(self.manual.open_manual())
        self.assertEqual(Path(open_url.call_args.args[0].toLocalFile()), de)

    def test_missing_manual_is_reported(self) -> None:
        with mock.patch.object(self.manual, "get_language", return_value="de"), \
                mock.patch.object(self.manual.QMessageBox, "warning") as warning, \
                mock.patch.object(self.manual.QDesktopServices, "openUrl") as open_url:
            self.assertFalse(self.manual.open_manual())
        warning.assert_called_once()
        open_url.assert_not_called()

    def test_shipped_manuals_are_found_in_the_repository(self) -> None:
        """The real PDFs sit where `gui/manual.py` and the PyInstaller spec
        expect them (built by tools/build_manual.py)."""
        from config.settings import get_app_path

        for file_name in self.manual.MANUAL_FILES.values():
            path = get_app_path("doc", "manual", file_name)
            if not path.exists():
                self.skipTest(f"{path.name} noch nicht gebaut (tools/build_manual.py)")


if __name__ == "__main__":
    unittest.main()
