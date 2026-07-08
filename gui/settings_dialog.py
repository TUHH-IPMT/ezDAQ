"""
gui/settings_dialog.py

Einstellungen-Dialog fuer Sprache.
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QFormLayout,
    QComboBox,
    QPushButton,
    QHBoxLayout,
)

from gui.i18n import t, get_language, set_language


class SettingsDialog(QDialog):
    """Dialog fuer Anwendungseinstellungen (Sprache)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("settings"))
        self.setModal(True)
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)

        # Form für Einstellungen
        form_layout = QFormLayout()

        # Sprach-Auswahl
        self._language_combo = QComboBox()
        self._language_combo.addItem(t("german"), "de")
        self._language_combo.addItem(t("english"), "en")
        current_lang = get_language()
        idx = 0 if current_lang == "de" else 1
        self._language_combo.setCurrentIndex(idx)
        form_layout.addRow(f"{t('language')}:", self._language_combo)

        layout.addLayout(form_layout)

        # OK / Cancel Buttons
        button_layout = QHBoxLayout()
        ok_button = QPushButton(t("ok"))
        cancel_button = QPushButton(t("cancel"))
        ok_button.clicked.connect(self._on_ok)
        cancel_button.clicked.connect(self.reject)
        button_layout.addStretch()
        button_layout.addWidget(ok_button)
        button_layout.addWidget(cancel_button)

        layout.addLayout(button_layout)

    def _on_ok(self) -> None:
        """Speichert die Einstellungen und schließt den Dialog."""
        # Sprache ändern
        new_language = self._language_combo.currentData()
        set_language(new_language)

        self.accept()

    def get_language(self) -> str:
        """Gibt die gewählte Sprache zurück."""
        return self._language_combo.currentData()
