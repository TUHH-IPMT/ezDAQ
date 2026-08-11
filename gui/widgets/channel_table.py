"""
gui/widgets/channel_table.py

Wiederverwendbares Tabellen-Widget zur Bearbeitung einer Kanalkonfiguration.

Wird von `gui/setup_view.py` verwendet. Kapselt die Umwandlung zwischen
`data.models.Channel`-Objekten und den einzelnen Zellen-Widgets
(Checkbox, Textfelder, Comboboxen, Spinboxen).
"""

from __future__ import annotations

import re

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QCheckBox,
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
from gui.theme import (
    connect_theme_changed,
    draw_ellipsis_icon,
    draw_minus_icon,
    draw_plus_icon,
)

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

# Erkennt den Modul-Teil eines Geräte-/Kanalnamens, z. B. "Mod2" in
# "cDAQ9185-0217ED5EMod2". Der vorangestellte Chassis-Teil (oft eine lange
# Seriennummer wie "cDAQ9185-0217ED5E") ist für die Unterscheidung der
# Kanäle irrelevant - relevant ist nur, welches Modul/welcher Kanal
# gemeint ist (siehe `_short_hw_channel_text`).
_MODULE_SUFFIX_PATTERN = re.compile(r"Mod\d+")

# Welche Signaltypen ein Modul hardwareseitig unterstützt (siehe
# `hardware/ni9215.py`/`hardware/ni9234.py`, die jeweils den falschen
# Signaltyp mit einem `AcquisitionError` ablehnen). Die Kanaltabelle
# schränkt die Signaltyp-Auswahl pro Zeile entsprechend ein, statt den
# Fehler erst beim Messstart auftreten zu lassen.
_MODULE_SIGNAL_TYPES: dict[ModuleType, list[SignalType]] = {
    ModuleType.NI9215: [SignalType.VOLTAGE],
    ModuleType.NI9234: [SignalType.VOLTAGE, SignalType.IEPE_ACCELERATION],
}

# Übersetzte Anzeige-Labels für den Signaltyp-Auswahldialog/-Button. Der
# eigentliche Wert (Channel.signal_type.value, z. B. "voltage") bleibt
# unabhängig von der UI-Sprache stabil (Persistenz/Hardware-Vergleiche) -
# er wird als Button-Property "signal_type" hinterlegt, siehe
# `_create_signal_type_widget`.
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


class SignalTypePickerDialog(QDialog):
    """Dialog zur Auswahl des Signaltyps einer Zeile.

    Analog zu `HardwareChannelPickerDialog` (eigenes Fenster statt
    Inline-Combobox) - hier ohne Gruppierung, da `allowed` bereits auf die
    vom Modul der Zeile unterstützten Typen eingeschränkt ist (siehe
    `_MODULE_SIGNAL_TYPES`) und damit nur ein bis zwei Einträge enthält.
    """

    def __init__(
        self,
        allowed: list[SignalType],
        current_value: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("choose_signal_type_title"))
        self._selected_value: str | None = None

        layout = QVBoxLayout(self)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.itemSelectionChanged.connect(self._update_ok_enabled)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self._tree)

        selected_item: QTreeWidgetItem | None = None
        for signal_type in allowed:
            item = QTreeWidgetItem([t(_SIGNAL_TYPE_LABEL_KEYS[signal_type])])
            item.setData(0, _ROLE_CHANNEL_VALUE, signal_type.value)
            self._tree.addTopLevelItem(item)
            if signal_type.value == current_value:
                selected_item = item

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

        self.resize(300, 180)

    def selected_value(self) -> str | None:
        return self._selected_value

    def _update_ok_enabled(self) -> None:
        self._ok_button.setEnabled(bool(self._tree.selectedItems()))

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        self._selected_value = item.data(0, _ROLE_CHANNEL_VALUE)
        self.accept()

    def _on_accept(self) -> None:
        items = self._tree.selectedItems()
        if items:
            self._selected_value = items[0].data(0, _ROLE_CHANNEL_VALUE)
        self.accept()


class _PickerCell(QWidget):
    """Zellwidget für Spalten mit eigenem Auswahlfenster (Hardwarekanal,
    Signaltyp): links ein Textlabel mit dem aktuellen Wert, rechts ein
    kompakter, NUR mit dem Drei-Punkte-Symbol beschrifteter Button, der
    den jeweiligen Auswahldialog öffnet.

    Bietet dieselbe kleine API wie ein `QPushButton`
    (`setText`/`setToolTip`/`setIcon`/`setIconSize`/`clicked`), damit der
    übrige Code in `ChannelTableWidget` (Property-Handling, Eliding,
    Retheme, ...) unverändert weiterfunktioniert - Details siehe
    `_create_hw_channel_widget`/`_create_signal_type_widget`.
    """

    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 4, 0)
        # Deutlicher Abstand zwischen Text und Auswahl-Button, damit beide
        # klar als getrennte Elemente erkennbar sind.
        layout.setSpacing(10)

        self._label = QLabel()
        layout.addWidget(self._label, stretch=1)

        self._icon_button = QPushButton()
        # Bewusst KEIN setFlat(True): der normale, theme-abhängig
        # eingefärbte Button-Hintergrund (QPalette.ColorRole.Button, siehe
        # gui/theme.py) macht deutlicher als ein flacher/transparenter
        # Button, dass hier eine klickbare Fläche ist.
        self._icon_button.setFixedSize(22, 22)
        self._icon_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._icon_button.clicked.connect(self.clicked.emit)
        # Explizite vertikale Zentrierung: der Button hat eine feste Größe
        # und soll zur Höhe des Textlabels zentriert stehen, nicht die
        # volle Zeilenhöhe ausfüllen.
        layout.addWidget(self._icon_button, alignment=Qt.AlignmentFlag.AlignVCenter)

    def setText(self, text: str) -> None:
        self._label.setText(text)

    def setToolTip(self, text: str) -> None:  # noqa: D102 - siehe Klassendoc
        self._label.setToolTip(text)
        super().setToolTip(text)

    def setIcon(self, icon: QIcon) -> None:
        self._icon_button.setIcon(icon)

    def setIconSize(self, size: QSize) -> None:
        self._icon_button.setIconSize(size)


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
        # Mindesthöhe für ~6 Zeilen + Kopfzeile, damit die Tabelle in der
        # Setup-Ansicht nicht auf eine einzelne, kaum nutzbare Zeile
        # zusammengequetscht wird (siehe SetupView, die die gesamte Ansicht
        # zusätzlich in einen QScrollArea einbettet).
        self._table.setMinimumHeight(230)
        self._table.horizontalHeader().sectionResized.connect(
            self._on_hw_channel_column_resized
        )
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
                self._update_hw_channel_button_text(hw_widget)
            self._apply_module_signal_constraint(row)

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
        auch IEPE-Beschleunigung (siehe `_MODULE_SIGNAL_TYPES`) - ein nicht
        vom aktuellen Modul unterstützter Signaltyp wird hier automatisch
        auf den ersten unterstützten zurückgesetzt, statt erst beim
        Messstart als Fehler aufzufallen. Der Auswahldialog
        (`SignalTypePickerDialog`, siehe `_on_choose_signal_type_clicked`)
        bekommt dieselbe Einschränkung beim Öffnen frisch berechnet - hier
        also keine feste Liste, die separat gepflegt werden müsste.

        Ist NOCH KEIN Hardwarekanal zugewiesen, ist unklar, welches Modul
        (und damit welche Signaltypen) überhaupt gelten - der Button wird
        dann komplett gesperrt, statt eine geratene Vorgabe (aktuell
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
        previous_value = str(signal_widget.property("signal_type") or "")

        signal_widget.setProperty(
            "signal_type",
            previous_value if previous_value in allowed_values else allowed_values[0],
        )
        self._update_signal_type_button_text(signal_widget)
        signal_widget.setEnabled(has_channel)

        # Signaltyp kann sich durch die Einschränkung geändert haben -
        # Skalierung/Sensitivität entsprechend nachziehen.
        self._update_scale_sensitivity_state(row)

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

        is_iepe = signal_widget.property("signal_type") == SignalType.IEPE_ACCELERATION.value
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
        cell = _PickerCell()
        cell.setProperty("hw_channel", current_text or "")
        cell.setProperty("module_type", module_value or "")
        cell.clicked.connect(self._on_choose_hw_channel_clicked)
        self._apply_picker_button_icon(cell)
        self._update_hw_channel_button_text(cell)
        return cell

    @staticmethod
    def _apply_picker_button_icon(button: "_PickerCell") -> None:
        """Setzt das Drei-Punkte-Symbol, das einen Button als "öffnet ein
        Auswahlfenster" statt einer Direktaktion kennzeichnet (siehe
        `_create_hw_channel_widget`/`_create_signal_type_widget`). Wird auch
        bei Theme-Wechsel erneut aufgerufen (siehe `_retheme_action_button_icons`),
        da die Symbolfarbe vom Theme abhängt."""
        button.setIcon(QIcon(draw_ellipsis_icon(14)))
        button.setIconSize(QSize(14, 14))

    def _update_hw_channel_button_text(self, button: "_PickerCell") -> None:
        """Setzt den sichtbaren Button-Text aus den Properties zusammen.

        Getrennt von den Properties selbst, damit Anzeige- und interner
        Wert nie versehentlich vermischt werden (siehe
        `_create_hw_channel_widget`). Angezeigt wird nur noch ab dem
        Modul-Teil (z. B. "Mod2/ai0 (NI9234)" statt
        "cDAQ9185-0217ED5EMod2/ai0 (NI9234)") - der Chassis-Seriennummer-
        Präfix davor ist für die Unterscheidung der Kanäle irrelevant und
        kostet nur Platz (siehe `_short_hw_channel_text`). Der volle Name
        bleibt vollständig als Tooltip verfügbar; nur falls selbst der
        gekürzte Text nicht in die Spalte passt, wird zusätzlich am Anfang
        elidiert (siehe `_apply_elided_hw_text`).
        """
        hw_channel = str(button.property("hw_channel") or "")
        module_value = str(button.property("module_type") or "")
        if not hw_channel:
            display_text = t("choose_hw_channel_button")
            button.setToolTip("")
        else:
            full_text = f"{hw_channel} ({module_value})" if module_value else hw_channel
            button.setToolTip(full_text)
            display_text = self._short_hw_channel_text(hw_channel, module_value)
        button.setProperty("display_text", display_text)
        self._apply_elided_hw_text(button)

    @staticmethod
    def _short_hw_channel_text(hw_channel: str, module_value: str) -> str:
        """Kürzt einen Hardwarekanal-Namen auf den Modul-Teil ("ab Mod").

        "cDAQ9185-0217ED5EMod2/ai0" -> "Mod2/ai0". Enthält der Name (z. B.
        bei einem manuell in NI MAX umbenannten Gerät) kein "Mod<N>", wird
        der volle Name unverändert übernommen - besser ein langer Name als
        ein durch falsches Kürzen unbrauchbarer.
        """
        match = _MODULE_SUFFIX_PATTERN.search(hw_channel)
        short = hw_channel[match.start():] if match else hw_channel
        return f"{short} ({module_value})" if module_value else short

    def _apply_elided_hw_text(self, button: "_PickerCell") -> None:
        """Kürzt den (bereits auf "ab Mod" reduzierten) Button-Text weiter,
        falls er selbst dafür noch nicht in die aktuelle Spaltenbreite passt.

        `ElideLeft` statt `ElideMiddle`: Modul/Kanal/Typ stehen am Ende des
        Texts - das ist die eigentlich unterscheidende Information (welches
        Modul, welcher Kanal) und soll so lange wie möglich vollständig
        sichtbar bleiben, auch wenn dafür der Anfang wegfällt.

        Nutzt `QTableWidget.columnWidth()` statt `button.width()` als
        Referenz: Direkt nach dem Erzeugen (bevor der Button in der Zelle
        sitzt und Qt das Layout verarbeitet hat) wäre `button.width()`
        noch 0. Wird bei Spaltenbreitenänderung erneut aufgerufen (siehe
        `sectionResized`-Verbindung in `__init__`).
        """
        display_text = str(button.property("display_text") or "")
        # Platz für den separaten Drei-Punkte-Button (22px), den Abstand
        # dazu (10px) und die Zellränder (siehe `_PickerCell`) abziehen.
        available_width = self._table.columnWidth(_COL_HW_CHANNEL) - 22 - 10 - 14
        if available_width <= 0:
            button.setText(display_text)
            return
        metrics = button.fontMetrics()
        button.setText(
            metrics.elidedText(display_text, Qt.TextElideMode.ElideLeft, available_width)
        )

    def _on_hw_channel_column_resized(self, index: int, _old_size: int, _new_size: int) -> None:
        if index != _COL_HW_CHANNEL:
            return
        for row in range(self._table.rowCount()):
            widget = self._table.cellWidget(row, _COL_HW_CHANNEL)
            if isinstance(widget, _PickerCell):
                self._apply_elided_hw_text(widget)

    def _on_choose_hw_channel_clicked(self) -> None:
        """Öffnet den Kanal-Auswahldialog für die Zeile des klickenden Buttons.

        Ermittelt die betroffene Zeile über `sender()` (siehe
        `_find_row_for_widget`).
        """
        button = self.sender()
        row = self._find_row_for_widget(_COL_HW_CHANNEL, button)
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

    def _create_signal_type_widget(self, current_value: str) -> "_PickerCell":
        """Baut das Zellwidget für die Signaltyp-Spalte einer Zeile.

        Wie beim Hardwarekanal (siehe `_create_hw_channel_widget`) ein
        `_PickerCell`, das ein eigenes Auswahlfenster öffnet, statt einer
        Inline-Combobox - konsistente Bedienung für beide Spalten.
        """
        cell = _PickerCell()
        cell.setProperty("signal_type", current_value)
        cell.clicked.connect(self._on_choose_signal_type_clicked)
        self._apply_picker_button_icon(cell)
        self._update_signal_type_button_text(cell)
        return cell

    @staticmethod
    def _update_signal_type_button_text(button: "_PickerCell") -> None:
        value = str(button.property("signal_type") or "")
        try:
            signal_type = SignalType(value)
        except ValueError:
            button.setText(t("choose_signal_type_button"))
            return
        button.setText(t(_SIGNAL_TYPE_LABEL_KEYS[signal_type]))

    def _on_choose_signal_type_clicked(self) -> None:
        """Öffnet den Signaltyp-Auswahldialog für die Zeile des klickenden Buttons.

        Die erlaubten Signaltypen werden hier - wie in
        `_apply_module_signal_constraint` - frisch aus dem Modul der
        Hardwarekanal-Zelle derselben Zeile abgeleitet, statt separat
        mitgeführt zu werden.
        """
        button = self.sender()
        row = self._find_row_for_widget(_COL_SIGNAL, button)
        if row is None:
            return

        hw_widget = self._table.cellWidget(row, _COL_HW_CHANNEL)
        module_type = ModuleType(self._get_module_value_from_widget(hw_widget))
        allowed = _MODULE_SIGNAL_TYPES.get(module_type, list(SignalType))
        current_value = str(button.property("signal_type") or "")

        dialog = SignalTypePickerDialog(allowed, current_value, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dialog.selected_value()
        if not selected:
            return

        button.setProperty("signal_type", selected)
        self._update_signal_type_button_text(button)
        self._update_scale_sensitivity_state(row)

    def _find_row_for_widget(self, column: int, widget: QWidget) -> int | None:
        """Findet die Zeile eines Zellwidgets in `column` (siehe `sender()`-
        Aufrufer wie `_on_choose_hw_channel_clicked`/
        `_on_choose_signal_type_clicked`) - Zeilenindizes können sich durch
        `_on_remove_clicked` verschieben, daher keine feste Nummer beim
        Verbinden erfassen."""
        for row in range(self._table.rowCount()):
            if self._table.cellWidget(row, column) is widget:
                return row
        return None

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
        initial_signal_value = (
            channel.signal_type.value
            if channel.signal_type.value in allowed_signal_values
            else allowed_signal_values[0]
        )
        self._table.setCellWidget(
            row, _COL_SIGNAL, self._create_signal_type_widget(initial_signal_value)
        )

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
        signal_type = SignalType(self._table.cellWidget(row, _COL_SIGNAL).property("signal_type"))
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
        # Drei-Punkte-Symbol der Hardwarekanal-/Signaltyp-Buttons ist
        # theme-abhängig eingefärbt (siehe `_apply_picker_button_icon`) -
        # bei bestehenden Zeilen nach einem Theme-Wechsel erneuern.
        for row in range(self._table.rowCount()):
            for column in (_COL_HW_CHANNEL, _COL_SIGNAL):
                widget = self._table.cellWidget(row, column)
                if isinstance(widget, _PickerCell):
                    self._apply_picker_button_icon(widget)

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
        `_PickerCell`, siehe `_create_hw_channel_widget`).

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
