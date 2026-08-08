"""
gui/widgets/channel_table.py

Wiederverwendbares Tabellen-Widget zur Bearbeitung einer Kanalkonfiguration.

Wird von `gui/setup_view.py` verwendet. Kapselt die Umwandlung zwischen
`data.models.Channel`-Objekten und den einzelnen Zellen-Widgets
(Checkbox, Textfelder, Comboboxen, Spinboxen).
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from data.models import Channel, DeviceInfo, ModuleType, SignalType
from gui.i18n import connect_language_changed, t
from gui.theme import connect_theme_changed, draw_minus_icon, draw_plus_icon

_COLUMN_KEYS = [
    "col_number",
    "col_active",
    "col_hw_channel",
    "col_display_name",
    "col_unit",
    "col_signal_type",
    "col_scale",
    "col_offset",
    "col_sensitivity",
]

# Welche Signaltypen ein Modul hardwareseitig unterstützt (siehe
# `hardware/ni9215.py`/`hardware/ni9234.py`, die jeweils den falschen
# Signaltyp mit einem `AcquisitionError` ablehnen). Die Kanaltabelle
# schränkt die Signaltyp-Auswahl pro Zeile entsprechend ein, statt den
# Fehler erst beim Messstart auftreten zu lassen.
_MODULE_SIGNAL_TYPES: dict[ModuleType, list[SignalType]] = {
    ModuleType.NI9215: [SignalType.VOLTAGE],
    ModuleType.NI9234: [SignalType.VOLTAGE, SignalType.IEPE_ACCELERATION],
}

# Übersetzte Anzeige-Labels für die Signaltyp-Combobox. Der eigentliche
# Wert (Channel.signal_type.value, z. B. "voltage") bleibt unabhängig
# von der UI-Sprache stabil (Persistenz/Hardware-Vergleiche) - er wird
# als `userData` der Combobox-Einträge hinterlegt, siehe
# `_populate_signal_combo`.
_SIGNAL_TYPE_LABEL_KEYS: dict[SignalType, str] = {
    SignalType.VOLTAGE: "signal_type_voltage",
    SignalType.IEPE_ACCELERATION: "signal_type_iepe",
}

_COL_NUMBER = 0
_COL_ENABLED = 1
_COL_HW_CHANNEL = 2
_COL_NAME = 3
_COL_UNIT = 4
_COL_SIGNAL = 5
_COL_SCALE = 6
_COL_OFFSET = 7
_COL_SENSITIVITY = 8

_ROLE_CHANNEL_VALUE = int(Qt.ItemDataRole.UserRole)


class HardwareChannelPickerDialog(QDialog):
    """Dialog zur Auswahl eines Hardwarekanals, gruppiert nach Gerät/Modul.

    Ersetzt die vorherige Dropdown-Auswahl in der Kanaltabelle: bei
    mehreren Modulen mit jeweils vielen Kanälen ist eine nach Gerät
    gruppierte Baumansicht übersichtlicher als eine lange, flache Liste.
    Kanäle, die bereits einer ANDEREN Zeile zugeordnet sind, werden
    angezeigt, aber deaktiviert (nicht auswählbar) - derselbe physische
    Kanal darf nicht doppelt vergeben werden.
    """

    def __init__(
        self,
        devices: list[DeviceInfo],
        used_channels: set[str],
        current_channel: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("choose_hw_channel_title"))
        self._selected_channel: str | None = None

        layout = QVBoxLayout(self)

        if not devices:
            # Keine Geräteerkennung erfolgt (oder keine Hardware gefunden) -
            # statt eines leeren Baums ein klarer Hinweis, was zu tun ist.
            # Kein OK-Button, da es (noch) nichts zum Auswählen gibt.
            hint_label = QLabel(t("hw_channel_picker_no_devices"))
            hint_label.setWordWrap(True)
            layout.addWidget(hint_label)
            self._tree = None
            close_box = QDialogButtonBox()
            close_button = close_box.addButton(t("cancel"), QDialogButtonBox.ButtonRole.RejectRole)
            close_button.clicked.connect(self.reject)
            layout.addWidget(close_box)
            self.resize(360, 140)
            return

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.itemSelectionChanged.connect(self._update_ok_enabled)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self._tree)

        selected_item: QTreeWidgetItem | None = None
        for device in devices:
            module_info = f" [{device.module_type.value}]" if device.module_type else ""
            device_item = QTreeWidgetItem([f"{device.device_name} - {device.product_type}{module_info}"])
            device_item.setFlags(device_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            channels = device.physical_channels or [
                f"{device.device_name}/ai{i}" for i in range(device.num_channels)
            ]
            for channel in channels:
                is_used = channel in used_channels and channel != current_channel
                label = t("hw_channel_already_used", channel=channel) if is_used else channel
                channel_item = QTreeWidgetItem([label])
                channel_item.setData(0, _ROLE_CHANNEL_VALUE, channel)
                if is_used:
                    channel_item.setFlags(
                        channel_item.flags()
                        & ~Qt.ItemFlag.ItemIsEnabled
                        & ~Qt.ItemFlag.ItemIsSelectable
                    )
                device_item.addChild(channel_item)
                if channel == current_channel:
                    selected_item = channel_item
            self._tree.addTopLevelItem(device_item)
        self._tree.expandAll()

        self._button_box = QDialogButtonBox()
        self._ok_button = self._button_box.addButton(
            t("ok"), QDialogButtonBox.ButtonRole.AcceptRole
        )
        cancel_button = self._button_box.addButton(
            t("cancel"), QDialogButtonBox.ButtonRole.RejectRole
        )
        self._ok_button.setEnabled(False)
        self._ok_button.clicked.connect(self._on_accept)
        cancel_button.clicked.connect(self.reject)
        layout.addWidget(self._button_box)

        if selected_item is not None:
            self._tree.setCurrentItem(selected_item)

        self.resize(380, 420)

    def selected_channel(self) -> str | None:
        return self._selected_channel

    def _update_ok_enabled(self) -> None:
        items = self._tree.selectedItems()
        has_leaf = bool(items) and items[0].data(0, _ROLE_CHANNEL_VALUE) is not None
        self._ok_button.setEnabled(has_leaf)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        value = item.data(0, _ROLE_CHANNEL_VALUE)
        if value is not None and (item.flags() & Qt.ItemFlag.ItemIsSelectable):
            self._selected_channel = value
            self.accept()

    def _on_accept(self) -> None:
        items = self._tree.selectedItems()
        if items:
            self._selected_channel = items[0].data(0, _ROLE_CHANNEL_VALUE)
        self.accept()


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

        self._table = QTableWidget(0, len(_COLUMN_KEYS), self)
        self._table.setHorizontalHeaderLabels([t(key) for key in _COLUMN_KEYS])
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._table.horizontalHeader().setSectionResizeMode(
            _COL_NUMBER, QHeaderView.ResizeMode.Fixed
        )
        self._table.horizontalHeader().setSectionResizeMode(
            _COL_ENABLED, QHeaderView.ResizeMode.Fixed
        )
        self._table.setColumnWidth(_COL_NUMBER, 52)
        # Erste Spalte (Aktiv + Checkbox) bewusst etwas breiter halten.
        self._table.setColumnWidth(_COL_ENABLED, 86)
        # Nummerierung läuft über eigene erste Spalte statt Zeilenkopf.
        self._table.verticalHeader().setVisible(False)
        layout.addWidget(self._table)

        # Erkannte Geräte (Geräteerkennung) - bestimmen, welche
        # Hardwarekanäle wählbar sind und welches Modul zu welchem Kanal
        # gehört (siehe `set_available_devices`).
        self._available_devices: list[DeviceInfo] = []
        self._available_hw_channels: list[str] = []
        self._hw_channel_to_module: dict[str, ModuleType] = {}

        button_row = QHBoxLayout()
        self._add_button = QPushButton(t("add_channel_button"))
        self._remove_button = QPushButton(t("remove_channel_button"))
        self._add_button.setIconSize(QSize(14, 14))
        self._remove_button.setIconSize(QSize(14, 14))
        self._retheme_action_button_icons()
        self._add_button.clicked.connect(self._on_add_clicked)
        self._remove_button.clicked.connect(self._on_remove_clicked)
        button_row.addWidget(self._add_button)
        button_row.addWidget(self._remove_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        connect_language_changed(self.retranslate_ui)
        connect_theme_changed(self._retheme_action_button_icons)

    # ------------------------------------------------------------------ #
    # Öffentliche API
    # ------------------------------------------------------------------ #

    def retranslate_ui(self) -> None:
        """Aktualisiert Spaltenköpfe und Buttons nach einem Sprachwechsel."""
        self._table.setHorizontalHeaderLabels([t(key) for key in _COLUMN_KEYS])
        self._add_button.setText(t("add_channel_button"))
        self._remove_button.setText(t("remove_channel_button"))
        for row in range(self._table.rowCount()):
            # Der Hardwarekanal-Button zeigt ohne Auswahl einen
            # übersetzten Platzhalter (siehe `_create_hw_channel_widget`) -
            # bei bereits gewähltem Kanal steht dort der reine (nicht zu
            # übersetzende) Kanalname, der bleibt unverändert.
            hw_widget = self._table.cellWidget(row, _COL_HW_CHANNEL)
            if hw_widget is not None and not hw_widget.property("hw_channel"):
                hw_widget.setText(t("choose_hw_channel_button"))
            self._apply_module_signal_constraint(row)

    def _on_signal_type_changed(self) -> None:
        """Reagiert auf einen Signaltyp-Wechsel in einer beliebigen Zeile.

        Ermittelt die betroffene Zeile über `sender()` statt über eine beim
        Verbinden erfasste Zeilennummer, da sich Zeilenindizes durch
        `_on_remove_clicked` verschieben können.
        """
        combo = self.sender()
        for row in range(self._table.rowCount()):
            if self._table.cellWidget(row, _COL_SIGNAL) is combo:
                self._update_scale_sensitivity_state(row)
                return

    def _apply_device_constraint(self, row: int) -> None:
        """Aktualisiert den intern mitgeführten Modulwert des Hardwarekanal-Buttons.

        Modul ist NIE eine freie Auswahl - es ist entweder durch die
        erkannte Hardware eindeutig vorgegeben (siehe
        `set_available_devices`/`_hw_channel_to_module`) oder stammt aus
        einer geladenen Konfiguration ohne angeschlossene Hardware. Es
        hat deshalb keine eigene Spalte, sondern wird direkt an der
        Hardwarekanal-Zelle mitgeführt (siehe `_create_hw_channel_widget`).
        """
        hw_widget = self._table.cellWidget(row, _COL_HW_CHANNEL)
        if hw_widget is None:
            return

        hw_channel = self._get_hw_channel_text_from_widget(hw_widget).strip()
        known_module = self._hw_channel_to_module.get(hw_channel)
        module_value = (
            known_module.value
            if known_module is not None
            else self._get_module_value_from_widget(hw_widget) or ModuleType.NI9215.value
        )
        hw_widget.setProperty("module_type", module_value)
        self._update_hw_channel_button_text(hw_widget)

        # Modul kann sich geändert haben - Signaltyp-Einschränkung
        # entsprechend nachziehen.
        self._apply_module_signal_constraint(row)

    def _apply_module_signal_constraint(self, row: int) -> None:
        """Schränkt die Signaltyp-Auswahl einer Zeile auf das gewählte Modul ein.

        Ein NI9215 unterstützt nur Spannung, ein NI9234 sowohl Spannung als
        auch IEPE-Beschleunigung (siehe `_MODULE_SIGNAL_TYPES`) - die für
        das jeweilige Modul nicht unterstützten Optionen werden aus der
        Combobox entfernt, statt erst beim Messstart als Fehler
        aufzufallen. Die Combobox bleibt dabei normal bedienbar (nicht
        ausgegraut) - fehlt eine Option, ist sie schlicht nicht in der
        Liste enthalten.

        Ist NOCH KEIN Hardwarekanal zugewiesen, ist unklar, welches Modul
        (und damit welche Signaltypen) überhaupt gelten - die Combobox
        wird dann komplett gesperrt, statt eine geratene Vorgabe (aktuell
        NI9215/Spannung) als scheinbar bewusste Auswahl anzubieten.
        """
        hw_widget = self._table.cellWidget(row, _COL_HW_CHANNEL)
        signal_widget = self._table.cellWidget(row, _COL_SIGNAL)
        if hw_widget is None or signal_widget is None:
            return

        has_channel = bool(self._get_hw_channel_text_from_widget(hw_widget).strip())
        module_type = ModuleType(self._get_module_value_from_widget(hw_widget))
        allowed = _MODULE_SIGNAL_TYPES.get(module_type, list(SignalType))
        allowed_values = [s.value for s in allowed]
        previous_value = signal_widget.currentData()

        self._populate_signal_combo(
            signal_widget,
            allowed,
            previous_value if previous_value in allowed_values else allowed_values[0],
        )
        signal_widget.setEnabled(has_channel)

        # Signaltyp kann sich durch die Einschränkung geändert haben -
        # Skalierung/Sensitivität entsprechend nachziehen (`_populate_signal_combo`
        # blockt Signale waehrend des Neuaufbaus, das passiert also nicht automatisch).
        self._update_scale_sensitivity_state(row)

    @staticmethod
    def _populate_signal_combo(
        combo: QComboBox, allowed: list[SignalType], selected_value: str
    ) -> None:
        """Befüllt eine Signaltyp-Combobox mit übersetzten Labels.

        Der technische Wert (z. B. "voltage") wird als `userData` je
        Eintrag hinterlegt und bleibt damit unabhängig von der
        UI-Sprache abrufbar (siehe `_read_row`/`_update_scale_sensitivity_state`).
        """
        combo.blockSignals(True)
        combo.clear()
        for signal_type in allowed:
            combo.addItem(t(_SIGNAL_TYPE_LABEL_KEYS[signal_type]), signal_type.value)
        index = combo.findData(selected_value)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    def _update_scale_sensitivity_state(self, row: int) -> None:
        """Sperrt/entsperrt Skalierung bzw. Sensitivität je nach Signaltyp der Zeile.

        Bei IEPE-Beschleunigungssensoren übernimmt bereits der NI-DAQmx-
        Treiber die physikalische Umrechnung über die Sensitivität (siehe
        `hardware/ni9234.py`) - eine zusätzliche Skalierung würde die
        Werte doppelt skalieren. Bei Spannungskanälen ist die Sensitivität
        umgekehrt bedeutungslos (`hardware/ni9215.py` liest sie nie). Das
        jeweils irrelevante Feld wird gesperrt UND auf seinen neutralen
        Wert zurückgesetzt - ein nur optisch gesperrtes Feld würde seinen
        Wert beim Auslesen (`_read_row`) sonst trotzdem behalten.
        """
        signal_widget = self._table.cellWidget(row, _COL_SIGNAL)
        scale_widget = self._table.cellWidget(row, _COL_SCALE)
        sensitivity_widget = self._table.cellWidget(row, _COL_SENSITIVITY)
        if signal_widget is None or scale_widget is None or sensitivity_widget is None:
            return

        is_iepe = signal_widget.currentData() == SignalType.IEPE_ACCELERATION.value
        scale_widget.setEnabled(not is_iepe)
        sensitivity_widget.setEnabled(is_iepe)
        if is_iepe:
            scale_widget.setValue(1.0)
            scale_widget.setToolTip(t("scale_disabled_tooltip"))
            sensitivity_widget.setToolTip("")
        else:
            sensitivity_widget.setValue(0.0)
            sensitivity_widget.setToolTip(t("sensitivity_disabled_tooltip"))
            scale_widget.setToolTip("")

    def set_channels(self, channels: list[Channel]) -> None:
        """Befüllt die Tabelle mit den übergebenen Kanälen (ersetzt den Inhalt)."""
        self._table.setRowCount(0)
        for channel in channels:
            self._add_row(channel)
        self._update_row_numbers()
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
        if self._available_devices:
            # Erster noch unbenutzter Kanal als Vorgabe - verhindert, dass
            # mehrere neu hinzugefügte Zeilen denselben physischen Kanal
            # doppelt zugewiesen bekommen (siehe `_used_hw_channels`).
            used = self._used_hw_channels()
            default_hw_channel = next(
                (ch for ch in self._available_hw_channels if ch not in used), None
            )
            if default_hw_channel is None:
                QMessageBox.information(
                    self, t("all_channels_assigned_title"), t("all_channels_assigned_body")
                )
                return
        else:
            # Noch keine Geräteerkennung erfolgt - kein erfundener
            # Platzhalterkanal mehr (der könnte nie zur tatsächlichen
            # Hardware passen). Der Button zeigt "Kanal wählen...", der
            # Dialog erklärt beim Klick, dass zuerst eine Geräteerkennung
            # nötig ist (siehe `HardwareChannelPickerDialog`).
            default_hw_channel = ""

        default_channel = Channel(
            hardware_channel=default_hw_channel,
            display_name=t("default_channel_name", index=self._table.rowCount() + 1),
        )
        self._add_row(default_channel)

    def _used_hw_channels(self, exclude_row: int | None = None) -> set[str]:
        """Sammelt die aktuell in der Tabelle bereits zugeordneten Kanäle.

        `exclude_row` lässt die eigene Zeile beim Öffnen des Auswahldialogs
        aus - sonst würde ihr eigener, bereits gültiger Kanal fälschlich
        als "belegt" erscheinen.
        """
        used: set[str] = set()
        for row in range(self._table.rowCount()):
            if row == exclude_row:
                continue
            widget = self._table.cellWidget(row, _COL_HW_CHANNEL)
            channel = self._get_hw_channel_text_from_widget(widget).strip()
            if channel:
                used.add(channel)
        return used

    def set_available_devices(self, devices: list[DeviceInfo]) -> None:
        """Setzt die aktuell erkannten Geräte/Module (Geräteerkennung).

        Beschränkt die Hardwarekanal-Auswahl je Zeile auf die tatsächlich
        vorhandenen physischen Kanäle ALLER erkannten Geräte (nicht nur
        eines einzelnen) - welche Kanäle existieren, gibt die Hardware
        vor, kein Freitext. Das Modul je Zeile wird passend dazu
        automatisch abgeleitet, siehe `_apply_device_constraint`.

        Eine leere Liste (keine Geräteerkennung erfolgt/keine Hardware
        gefunden) zeigt im Auswahldialog nur einen entsprechenden Hinweis
        an (siehe `HardwareChannelPickerDialog`) - kein Freitextfeld mehr.
        """
        self._available_devices = devices or []
        self._hw_channel_to_module = {}
        self._available_hw_channels = []
        for device in self._available_devices:
            channels = device.physical_channels or [
                f"{device.device_name}/ai{i}" for i in range(device.num_channels)
            ]
            self._available_hw_channels.extend(channels)
            if device.module_type is not None:
                for hw_channel in channels:
                    self._hw_channel_to_module[hw_channel] = device.module_type
        self._apply_available_hw_channels_to_rows()

    def _apply_available_hw_channels_to_rows(self) -> None:
        for row in range(self._table.rowCount()):
            old_widget = self._table.cellWidget(row, _COL_HW_CHANNEL)
            current_text = self._get_hw_channel_text_from_widget(old_widget)
            current_module = self._get_module_value_from_widget(old_widget)
            self._table.setCellWidget(
                row, _COL_HW_CHANNEL, self._create_hw_channel_widget(current_text, current_module)
            )
            self._apply_device_constraint(row)

    def _create_hw_channel_widget(self, current_text: str, module_value: str = "") -> QWidget:
        """Baut das Zellwidget für die Hardwarekanal-Spalte einer Zeile.

        Immer ein Button, der `HardwareChannelPickerDialog` öffnet - auch
        OHNE bekannte Geräte (der Dialog zeigt dann einen Hinweis statt
        einer leeren Auswahl, siehe `HardwareChannelPickerDialog`). Kein
        Freitextfeld mehr: ein beliebiger, frei eingetippter Kanalname
        könnte nie zur tatsächlichen Hardware passen und denselben
        physischen Kanal mehrfach vergeben.

        Das Modul bekommt bewusst KEINE eigene Spalte: es gehört
        inhaltlich untrennbar zum Kanal (jeder physische Kanal hat genau
        ein Modul) und wird deshalb direkt am Kanal-Button als
        Klartext-Zusatz mitgeführt, z. B. "cDAQ1Mod1/ai0 (NI9215)".

        WICHTIG: Dieser Anzeigetext ist strikt von den intern verwendeten
        Werten getrennt - die eigentlichen Werte stecken in den
        Button-Properties "hw_channel" bzw. "module_type" (siehe
        `_get_hw_channel_text_from_widget`/`_get_module_value_from_widget`),
        NICHT im sichtbaren, zusammengesetzten Text. Ohne Auswahl zeigt
        der Button nur einen Platzhalter, der selbst kein gültiger
        Kanal-/Modulwert ist.
        """
        button = QPushButton()
        button.setProperty("hw_channel", current_text or "")
        button.setProperty("module_type", module_value or "")
        button.clicked.connect(self._on_choose_hw_channel_clicked)
        self._update_hw_channel_button_text(button)
        return button

    @staticmethod
    def _update_hw_channel_button_text(button: QPushButton) -> None:
        """Setzt den sichtbaren Button-Text aus den Properties zusammen.

        Getrennt von den Properties selbst, damit Anzeige- und interner
        Wert nie versehentlich vermischt werden (siehe
        `_create_hw_channel_widget`).
        """
        hw_channel = str(button.property("hw_channel") or "")
        module_value = str(button.property("module_type") or "")
        if not hw_channel:
            button.setText(t("choose_hw_channel_button"))
        elif module_value:
            button.setText(f"{hw_channel} ({module_value})")
        else:
            button.setText(hw_channel)

    def _on_choose_hw_channel_clicked(self) -> None:
        """Öffnet den Kanal-Auswahldialog für die Zeile des klickenden Buttons.

        Ermittelt die betroffene Zeile über `sender()` (siehe
        `_on_signal_type_changed`).
        """
        button = self.sender()
        row = None
        for candidate_row in range(self._table.rowCount()):
            if self._table.cellWidget(candidate_row, _COL_HW_CHANNEL) is button:
                row = candidate_row
                break
        if row is None:
            return

        current_channel = str(button.property("hw_channel") or "")
        dialog = HardwareChannelPickerDialog(
            self._available_devices, self._used_hw_channels(exclude_row=row), current_channel, self
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dialog.selected_channel()
        if not selected:
            return

        button.setProperty("hw_channel", selected)
        self._apply_device_constraint(row)

    def _on_remove_clicked(self) -> None:
        selected_rows = sorted(
            {index.row() for index in self._table.selectedIndexes()}, reverse=True
        )
        for row in selected_rows:
            self._table.removeRow(row)
        self._update_row_numbers()

    def _add_row(self, channel: Channel) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)

        number_item = QTableWidgetItem(str(row + 1))
        number_item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
        number_item.setFlags(number_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._table.setItem(row, _COL_NUMBER, number_item)

        enabled_checkbox = QCheckBox()
        enabled_checkbox.setChecked(channel.enabled)
        self._table.setCellWidget(row, _COL_ENABLED, self._center(enabled_checkbox))

        # Hardwarekanal-Button (immer, siehe `_create_hw_channel_widget`) -
        # führt das Modul direkt mit, keine eigene Spalte dafür.
        self._table.setCellWidget(
            row,
            _COL_HW_CHANNEL,
            self._create_hw_channel_widget(channel.hardware_channel, channel.module_type.value),
        )
        self._table.setCellWidget(row, _COL_NAME, self._line_edit(channel.display_name))
        self._table.setCellWidget(row, _COL_UNIT, self._line_edit(channel.unit))

        allowed_signal_types = _MODULE_SIGNAL_TYPES.get(channel.module_type, list(SignalType))
        allowed_signal_values = [s.value for s in allowed_signal_types]
        signal_combo = QComboBox()
        self._populate_signal_combo(
            signal_combo,
            allowed_signal_types,
            channel.signal_type.value
            if channel.signal_type.value in allowed_signal_values
            else allowed_signal_values[0],
        )
        signal_combo.currentTextChanged.connect(self._on_signal_type_changed)
        self._table.setCellWidget(row, _COL_SIGNAL, signal_combo)

        self._table.setCellWidget(row, _COL_SCALE, self._double_spin(channel.scale, -1e9, 1e9))
        self._table.setCellWidget(row, _COL_OFFSET, self._double_spin(channel.offset, -1e9, 1e9))
        sensitivity = channel.sensitivity_mv_per_unit if channel.sensitivity_mv_per_unit else 0.0
        self._table.setCellWidget(
            row, _COL_SENSITIVITY, self._double_spin(sensitivity, 0.0, 1e6)
        )
        # Leitet - falls der Hardwarekanal einem bekannten Gerät zugeordnet
        # ist - das Modul daraus ab (siehe `_apply_device_constraint`) und
        # zieht Signaltyp-/Skalierungs-Einschränkung entsprechend nach.
        self._apply_device_constraint(row)
        self._update_row_numbers()

    def _read_row(self, row: int) -> Channel:
        enabled_widget = self._table.cellWidget(row, _COL_ENABLED)
        enabled = enabled_widget.findChild(QCheckBox).isChecked()

        hardware_widget = self._table.cellWidget(row, _COL_HW_CHANNEL)
        hardware_channel = self._get_hw_channel_text_from_widget(hardware_widget).strip()
        display_name = self._table.cellWidget(row, _COL_NAME).text().strip()
        unit = self._table.cellWidget(row, _COL_UNIT).text().strip()
        module_type = ModuleType(self._get_module_value_from_widget(hardware_widget))
        signal_type = SignalType(self._table.cellWidget(row, _COL_SIGNAL).currentData())
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

    def _retheme_action_button_icons(self) -> None:
        self._add_button.setIcon(QIcon(draw_plus_icon(16)))
        self._remove_button.setIcon(QIcon(draw_minus_icon(16)))

    def _update_row_numbers(self) -> None:
        for row in range(self._table.rowCount()):
            item = self._table.item(row, _COL_NUMBER)
            if item is None:
                item = QTableWidgetItem()
                item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row, _COL_NUMBER, item)
            item.setText(str(row + 1))

    @staticmethod
    def _get_hw_channel_text_from_widget(widget) -> str:
        """Liest den Kanalwert aus dem Hardwarekanal-Zellwidget (immer ein
        `QPushButton`, siehe `_create_hw_channel_widget`).

        Der tatsächliche Wert steckt in der Property "hw_channel", NICHT
        im sichtbaren Button-Text - der zeigt bei fehlender Auswahl einen
        Platzhalter, der selbst kein gültiger Kanalname ist.
        """
        if widget is None:
            return ""
        return str(widget.property("hw_channel") or "")

    @staticmethod
    def _get_module_value_from_widget(widget) -> str:
        """Liest den intern mitgeführten Modulwert vom Hardwarekanal-Button
        (Property "module_type", siehe `_create_hw_channel_widget`)."""
        if widget is None:
            return ""
        return str(widget.property("module_type") or "")

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
