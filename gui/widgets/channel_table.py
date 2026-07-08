"""
gui/widgets/channel_table.py

Wiederverwendbares Tabellen-Widget zur Bearbeitung einer Kanalkonfiguration.

Wird von `gui/setup_view.py` verwendet. Kapselt die Umwandlung zwischen
`data.models.Channel`-Objekten und den einzelnen Zellen-Widgets
(Checkbox, Textfelder, Comboboxen, Spinboxen).
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from data.models import Channel, ModuleType, SignalType

_COLUMNS = [
    "Aktiv",
    "Hardwarekanal",
    "Anzeigename",
    "Einheit",
    "Modul",
    "Signaltyp",
    "Skalierung",
    "Offset",
    "Sensitivität\n(mV/g, IEPE)",
]

_COL_ENABLED = 0
_COL_HW_CHANNEL = 1
_COL_NAME = 2
_COL_UNIT = 3
_COL_MODULE = 4
_COL_SIGNAL = 5
_COL_SCALE = 6
_COL_OFFSET = 7
_COL_SENSITIVITY = 8


class ChannelTableWidget(QWidget):
    """Tabelle zur Bearbeitung von Kanälen (Setup-Ansicht).

    Jede Zeile entspricht einem `Channel`. Über `set_channels()`/
    `get_channels()` wird zwischen der Tabelle und den Datenmodellen
    konvertiert.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._table = QTableWidget(0, len(_COLUMNS), self)
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._table.horizontalHeader().setSectionResizeMode(
            _COL_ENABLED, QHeaderView.ResizeMode.ResizeToContents
        )
        layout.addWidget(self._table)

        # Optional verfügbare Hardware-Kanäle (z. B. aus Geräteerkennung)
        self._available_hw_channels: list[str] = []

        button_row = QHBoxLayout()
        self._add_button = QPushButton("Kanal hinzufügen")
        self._remove_button = QPushButton("Ausgewählten Kanal entfernen")
        self._add_button.clicked.connect(self._on_add_clicked)
        self._remove_button.clicked.connect(self._on_remove_clicked)
        button_row.addWidget(self._add_button)
        button_row.addWidget(self._remove_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

    # ------------------------------------------------------------------ #
    # Öffentliche API
    # ------------------------------------------------------------------ #

    def set_channels(self, channels: list[Channel]) -> None:
        """Befüllt die Tabelle mit den übergebenen Kanälen (ersetzt den Inhalt)."""
        self._table.setRowCount(0)
        for channel in channels:
            self._add_row(channel)
        # Falls verfügbare Hardware-Kanäle gesetzt sind, aktualisiere die Zellen
        if self._available_hw_channels:
            self._apply_available_hw_channels_to_rows()

    def get_channels(self) -> list[Channel]:
        """Liest die aktuelle Tabelle als Liste von `Channel`-Objekten aus."""
        channels: list[Channel] = []
        for row in range(self._table.rowCount()):
            channels.append(self._read_row(row))
        return channels

    # ------------------------------------------------------------------ #
    # Interna
    # ------------------------------------------------------------------ #

    def _on_add_clicked(self) -> None:
        default_channel = Channel(
            hardware_channel="cDAQ1Mod1/ai0",
            display_name=f"Kanal {self._table.rowCount() + 1}",
        )
        self._add_row(default_channel)

    def set_available_hw_channels(self, channels: list[str]) -> None:
        """Setzt die Liste von verfügbaren Hardware-Kanälen.

        Wenn eine Liste gesetzt ist, wird die `Hardwarekanal`-Spalte als
        editierbares Dropdown (`QComboBox`) gefüllt. Leere Liste entfernt
        das Dropdown und verwendet wieder `QLineEdit`.
        """
        self._available_hw_channels = channels or []
        self._apply_available_hw_channels_to_rows()

    def _apply_available_hw_channels_to_rows(self) -> None:
        for row in range(self._table.rowCount()):
            current_text = self._get_hw_channel_text_from_widget(self._table.cellWidget(row, _COL_HW_CHANNEL))
            if self._available_hw_channels:
                combo = QComboBox()
                combo.setEditable(True)
                combo.addItems(self._available_hw_channels)
                if current_text and current_text in self._available_hw_channels:
                    combo.setCurrentText(current_text)
                elif current_text:
                    combo.setEditText(current_text)
                self._table.setCellWidget(row, _COL_HW_CHANNEL, combo)
            else:
                edit = QLineEdit()
                edit.setText(current_text or "")
                self._table.setCellWidget(row, _COL_HW_CHANNEL, edit)

    def _on_remove_clicked(self) -> None:
        selected_rows = sorted(
            {index.row() for index in self._table.selectedIndexes()}, reverse=True
        )
        for row in selected_rows:
            self._table.removeRow(row)

    def _add_row(self, channel: Channel) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)

        enabled_checkbox = QCheckBox()
        enabled_checkbox.setChecked(channel.enabled)
        self._table.setCellWidget(row, _COL_ENABLED, self._center(enabled_checkbox))

        # Hardware-Kanal als Dropdown, falls verfügbar, sonst als Textfeld
        if self._available_hw_channels:
            combo = QComboBox()
            combo.setEditable(True)
            combo.addItems(self._available_hw_channels)
            if channel.hardware_channel in self._available_hw_channels:
                combo.setCurrentText(channel.hardware_channel)
            else:
                combo.setEditText(channel.hardware_channel)
            self._table.setCellWidget(row, _COL_HW_CHANNEL, combo)
        else:
            self._table.setCellWidget(row, _COL_HW_CHANNEL, self._line_edit(channel.hardware_channel))
        self._table.setCellWidget(row, _COL_NAME, self._line_edit(channel.display_name))
        self._table.setCellWidget(row, _COL_UNIT, self._line_edit(channel.unit))

        module_combo = QComboBox()
        module_combo.addItems([m.value for m in ModuleType])
        module_combo.setCurrentText(channel.module_type.value)
        self._table.setCellWidget(row, _COL_MODULE, module_combo)

        signal_combo = QComboBox()
        signal_combo.addItems([s.value for s in SignalType])
        signal_combo.setCurrentText(channel.signal_type.value)
        self._table.setCellWidget(row, _COL_SIGNAL, signal_combo)

        self._table.setCellWidget(row, _COL_SCALE, self._double_spin(channel.scale, -1e9, 1e9))
        self._table.setCellWidget(row, _COL_OFFSET, self._double_spin(channel.offset, -1e9, 1e9))
        sensitivity = channel.sensitivity_mv_per_unit if channel.sensitivity_mv_per_unit else 0.0
        self._table.setCellWidget(
            row, _COL_SENSITIVITY, self._double_spin(sensitivity, 0.0, 1e6)
        )

    def _read_row(self, row: int) -> Channel:
        enabled_widget = self._table.cellWidget(row, _COL_ENABLED)
        enabled = enabled_widget.findChild(QCheckBox).isChecked()

        hardware_widget = self._table.cellWidget(row, _COL_HW_CHANNEL)
        hardware_channel = self._get_hw_channel_text_from_widget(hardware_widget).strip()
        display_name = self._table.cellWidget(row, _COL_NAME).text().strip()
        unit = self._table.cellWidget(row, _COL_UNIT).text().strip()
        module_type = ModuleType(self._table.cellWidget(row, _COL_MODULE).currentText())
        signal_type = SignalType(self._table.cellWidget(row, _COL_SIGNAL).currentText())
        scale = self._table.cellWidget(row, _COL_SCALE).value()
        offset = self._table.cellWidget(row, _COL_OFFSET).value()
        sensitivity = self._table.cellWidget(row, _COL_SENSITIVITY).value()

        return Channel(
            hardware_channel=hardware_channel,
            display_name=display_name or hardware_channel,
            unit=unit,
            scale=scale,
            offset=offset,
            signal_type=signal_type,
            module_type=module_type,
            enabled=enabled,
            sensitivity_mv_per_unit=sensitivity if sensitivity > 0 else None,
        )

    @staticmethod
    def _get_hw_channel_text_from_widget(widget) -> str:
        if widget is None:
            return ""
        # QLineEdit
        if hasattr(widget, "text") and callable(getattr(widget, "text")):
            try:
                return widget.text()
            except Exception:
                pass
        # QComboBox
        if hasattr(widget, "currentText") and callable(getattr(widget, "currentText")):
            try:
                return widget.currentText()
            except Exception:
                pass
        # Fallback: attempt to find child QLineEdit
        child = widget.findChild(QLineEdit)
        if child is not None:
            return child.text()
        return ""

    @staticmethod
    def _center(widget: QWidget) -> QWidget:
        """Zentriert ein Widget in einer Tabellenzelle (z. B. Checkbox)."""
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.addWidget(widget)
        layout.setAlignment(widget, Qt.AlignmentFlag.AlignCenter)
        layout.setContentsMargins(0, 0, 0, 0)
        return container

    @staticmethod
    def _line_edit(text: str) -> QLineEdit:
        edit = QLineEdit()
        edit.setText(text)
        return edit

    @staticmethod
    def _double_spin(value: float, minimum: float, maximum: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(4)
        spin.setValue(value)
        return spin
