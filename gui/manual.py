"""
gui/manual.py

Opening the user manual (PDF) from Help -> "User Manual (PDF)" / F1
(see `gui/main_window.py::_build_menu`).

The finished PDFs live in `doc/manual/` (bundled into the Windows build
under the same path, see `packaging/ezDAQ.spec`). Their sources and
build tools are kept outside the repository. The manual matching the current UI
language is opened; if it is missing, the other language is used rather
than nothing.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QMessageBox, QWidget

from config.settings import get_app_path
from gui.i18n import get_language, t

logger = logging.getLogger(__name__)

# File name per UI language (see `tools/build_manual.py`, which writes them).
MANUAL_FILES: dict[str, str] = {
    "de": "ezDAQ_Handbuch.pdf",
    "en": "ezDAQ_Manual.pdf",
}


def manual_path(language: str) -> Path | None:
    """The manual for `language`, or - if that one is missing - the one of
    another language; `None` if no manual is installed at all."""
    order = [language] + [lang for lang in MANUAL_FILES if lang != language]
    for lang in order:
        file_name = MANUAL_FILES.get(lang)
        if file_name is None:
            continue
        path = get_app_path("doc", "manual", file_name)
        if path.exists():
            return path
    return None


def open_manual(parent: QWidget | None = None) -> bool:
    """Opens the manual in the system's PDF viewer. Shows a message (and
    returns `False`) if there is no manual or no program to open it."""
    language = get_language()
    path = manual_path(language)
    if path is None:
        expected = get_app_path("doc", "manual", MANUAL_FILES.get(language, MANUAL_FILES["en"]))
        logger.warning("Handbuch nicht gefunden: %s", expected)
        QMessageBox.warning(
            parent, t("manual_missing_title"), t("manual_missing_body", path=str(expected))
        )
        return False
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
        logger.warning("Handbuch konnte nicht geöffnet werden: %s", path)
        QMessageBox.warning(
            parent, t("manual_open_failed_title"), t("manual_open_failed_body", path=str(path))
        )
        return False
    return True
