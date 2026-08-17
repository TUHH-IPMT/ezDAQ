"""Gemeinsame Dialog-Helfer für die GUI."""

from __future__ import annotations

from PyQt6.QtWidgets import QMessageBox, QWidget

from gui.i18n import t


def confirm_delete(parent: QWidget, body: str) -> bool:
    """Fragt einheitlich nach der Bestätigung einer Löschaktion."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(t("confirm_delete_title"))
    box.setText(body)
    delete_button = box.addButton(t("delete_action"), QMessageBox.ButtonRole.YesRole)
    box.addButton(t("cancel"), QMessageBox.ButtonRole.NoRole)
    box.setDefaultButton(delete_button)
    box.exec()
    return box.clickedButton() is delete_button
