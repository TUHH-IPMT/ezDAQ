"""
gui/theme.py

Theme-Hilfsfunktionen fuer die Anwendung.

Dark-Mode wurde entfernt; es wird ausschliesslich das Standard-Theme
der Plattform verwendet.
"""
from __future__ import annotations

from PyQt6.QtWidgets import QApplication


def apply_theme() -> None:
    """Setzt das globale Stylesheet auf das Standard-Theme der Plattform."""
    app = QApplication.instance()
    if app is None:
        return
    app.setStyleSheet("")
