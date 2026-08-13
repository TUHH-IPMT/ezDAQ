"""
gui/sensor_database_dialog.py

Verwaltungsdialog für den Sensor-Katalog (siehe data/sensor_models.py,
config/sensor_database.py) - reine CRUD-Oberfläche (Sensoren, Achsen,
Messbereich-Varianten anlegen/bearbeiten/löschen).

Bewusst KEINE automatische Übernahme-Funktion: `ChannelParameterDialog`
(siehe `gui/widgets/channel_table.py`) bietet nur einen Schnellzugriff-
Button, der GENAU DIESEN Dialog öffnet - der Nutzer sucht den passenden
Wert manuell heraus und trägt ihn per Copy&Paste selbst in die
Kanaleinstellungen ein. Dieser Dialog dient ausschließlich der Pflege
des Katalogs selbst.

Speicherverhalten: Jede Änderung (Textfeld, Tabellenzelle, Achse/Variante
hinzufügen/entfernen) wird SOFORT über den `SensorDatabaseManager`
persistiert - kein OK/Abbrechen, der Katalog verhält sich wie eine kleine,
dauerhaft gepflegte Datenbank statt wie ein Formular.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config.sensor_database import SensorDatabaseManager
from data.models import SignalType
from data.sensor_models import SensorChannelDefinition, SensorEntry, SensorRangeVariant
from gui.i18n import connect_language_changed, t
from gui.widgets.channel_table import SIGNAL_TYPE_LABEL_KEYS
from gui.widgets.spinbox import format_optional_float, parse_optional_float

_ROLE_SENSOR_ID = int(Qt.ItemDataRole.UserRole)
# Markiert die dekorative "Kopfzeile" unter einer Achse (siehe
# `_build_variant_header_item`) - unterscheidet sie beim Lesen/Entfernen
# von echten Messbereich-Varianten.
_ROLE_VARIANT_HEADER = int(Qt.ItemDataRole.UserRole) + 1

# Spaltenindizes des Achsen-/Varianten-Baums. Achsen-Zeilen befuellen
# LABEL/SIGNAL_TYPE, Varianten-Zeilen (Kinder) LABEL (als freier
# Messbereichs-Text, z. B. "±50" - bewusst KEINE separaten numerischen
# Min/Max-Felder) /RANGE_UNIT/SENSITIVITY/SENSITIVITY_UNIT - siehe
# `_add_axis_item`/`_build_range_item`. Spalte 1 hat je nach Zeilentyp
# bewusst eine ANDERE Bedeutung (Signaltyp bei der Achse, Messbereich-
# Einheit bei der Variante) - dank der exklusiven Kopfzeile pro Ebene
# (siehe `_build_variant_header_item`) bleibt trotzdem an Ort und Stelle
# klar, was gerade gemeint ist.
_COL_LABEL = 0
_COL_SIGNAL_TYPE = 1
_COL_RANGE_UNIT = 1
_COL_SENSITIVITY = 2
_COL_SENSITIVITY_UNIT = 3

# Fest codiertes Passwort - reiner Schutz gegen VERSEHENTLICHES Ändern
# von Tabellenwerten, keine echte Sicherheitsfunktion (siehe Klassendoc
# unten). Bewusst kein pro-Katalog konfigurierbares Passwort (das wäre
# hier unnötiger Aufwand für ein reines Bedienungs-Versehen-Schloss).
_UNLOCK_PASSWORD = "fertig"


class SensorDatabaseDialog(QDialog):
    """Verwaltet den Sensor-Katalog: Sensoren links als Liste, rechts
    Stammdaten + Achsen/Messbereich-Varianten als Baum.

    Schreibschutz: Der Dialog startet IMMER gesperrt (nur Lesen/Kopieren
    möglich) - "Entsperren..." verlangt das feste Passwort
    (`_UNLOCK_PASSWORD`), danach ist Bearbeiten für den Rest dieser
    Dialog-Sitzung möglich. Rein gegen versehentliches Ändern von
    Tabellenwerten gedacht, keine echte Zugriffskontrolle.
    """

    def __init__(
        self, sensor_database: SensorDatabaseManager, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._sensor_database = sensor_database
        self._current_sensor_id: Optional[str] = None
        # Verhindert, dass programmatisches Befüllen der Widgets (siehe
        # `_show_sensor`) als Nutzeränderung interpretiert und dadurch
        # ungewollt sofort wieder gespeichert wird.
        self._loading = False
        # Startet IMMER gesperrt (siehe Klassendoc) - kein Opt-in mehr.
        self._locked = True

        self.setWindowTitle(t("sensor_database_dialog_title"))
        self.resize(860, 520)

        outer = QVBoxLayout(self)

        lock_row = QHBoxLayout()
        self._lock_status_label = QLabel()
        lock_row.addWidget(self._lock_status_label)
        lock_row.addStretch(1)
        self._unlock_button = QPushButton()
        self._unlock_button.clicked.connect(self._on_unlock_button_clicked)
        lock_row.addWidget(self._unlock_button)
        outer.addLayout(lock_row)

        root = QHBoxLayout()
        outer.addLayout(root)

        left = QVBoxLayout()
        # Baum statt flacher Liste: Sensoren werden nach `category`
        # gruppiert (siehe `_reload_sensor_list`) - Kategorie-Kopfzeilen
        # sind wie die Messbereich-Kopfzeile weder auswählbar noch
        # editierbar, nur Sensor-Blätter sind es.
        self._sensor_list = QTreeWidget()
        self._sensor_list.setHeaderHidden(True)
        self._sensor_list.currentItemChanged.connect(self._on_sensor_selection_changed)
        left.addWidget(self._sensor_list, stretch=1)
        list_button_row = QHBoxLayout()
        self._add_sensor_button = QPushButton(t("add_sensor_button"))
        self._add_sensor_button.clicked.connect(self._on_add_sensor_clicked)
        self._remove_sensor_button = QPushButton(t("remove_sensor_button"))
        self._remove_sensor_button.clicked.connect(self._on_remove_sensor_clicked)
        list_button_row.addWidget(self._add_sensor_button)
        list_button_row.addWidget(self._remove_sensor_button)
        left.addLayout(list_button_row)
        root.addLayout(left, stretch=1)

        right = QVBoxLayout()
        form = QFormLayout()
        # Freier Text statt fester Auswahlliste (siehe
        # data/sensor_models.py::SensorEntry.category Moduldoc) -
        # editierbare Combobox mit Autovervollständigung aus den bereits
        # verwendeten Kategorien, damit keine Tippfehler-Duplikate wie
        # "Kraft" vs. "Kraftmessung" entstehen.
        self._category_combo = QComboBox()
        self._category_combo.setEditable(True)
        self._category_combo.lineEdit().editingFinished.connect(self._save_current_sensor)
        self._category_combo.currentIndexChanged.connect(self._save_current_sensor)
        form.addRow(t("sensor_category_label"), self._category_combo)
        self._name_edit = QLineEdit()
        self._name_edit.editingFinished.connect(self._save_current_sensor)
        form.addRow(t("sensor_name_label"), self._name_edit)
        self._manufacturer_edit = QLineEdit()
        self._manufacturer_edit.editingFinished.connect(self._save_current_sensor)
        form.addRow(t("sensor_manufacturer_label"), self._manufacturer_edit)
        self._serial_edit = QLineEdit()
        self._serial_edit.editingFinished.connect(self._save_current_sensor)
        form.addRow(t("sensor_serial_label"), self._serial_edit)
        self._notes_edit = QLineEdit()
        self._notes_edit.editingFinished.connect(self._save_current_sensor)
        form.addRow(t("sensor_notes_label"), self._notes_edit)
        right.addLayout(form)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(4)
        self._update_tree_headers()
        self._tree.itemChanged.connect(self._on_tree_item_changed)
        # Etwas mehr Zeilenhöhe (Padding statt fester Pixelzahl, damit es
        # bei anderer Schriftgröße/DPI mitskaliert) - macht besonders die
        # zweite Ebene (Messbereich-Varianten) weniger gedrängt.
        self._tree.setStyleSheet("QTreeView::item { padding: 4px 0; }")
        # Zell- statt Zeilen-Selektion: ein Klick markiert nur die
        # angeklickte Zelle, nicht die ganze Zeile - sonst kaum möglich,
        # gezielt EINEN Wert (z. B. zum Kopieren) auszuwählen, ohne dass
        # optisch die komplette Zeile "im Weg" markiert wird.
        self._tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        right.addWidget(self._tree, stretch=1)

        tree_button_row = QHBoxLayout()
        self._add_axis_button = QPushButton(t("add_axis_button"))
        self._add_axis_button.clicked.connect(self._on_add_axis_clicked)
        self._remove_axis_button = QPushButton(t("remove_axis_button"))
        self._remove_axis_button.clicked.connect(self._on_remove_axis_clicked)
        self._add_range_button = QPushButton(t("add_range_button"))
        self._add_range_button.clicked.connect(self._on_add_range_clicked)
        self._remove_range_button = QPushButton(t("remove_range_button"))
        self._remove_range_button.clicked.connect(self._on_remove_range_clicked)
        for button in (
            self._add_axis_button,
            self._remove_axis_button,
            self._add_range_button,
            self._remove_range_button,
        ):
            tree_button_row.addWidget(button)
        right.addLayout(tree_button_row)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        self._close_button = QPushButton(t("close_button"))
        self._close_button.clicked.connect(self.accept)
        close_row.addWidget(self._close_button)
        right.addLayout(close_row)

        root.addLayout(right, stretch=2)

        # Ohne dies macht Qt automatisch den ERSTEN erzeugten QPushButton
        # (hier `_unlock_button`) zum Default-Button des Dialogs - Enter
        # in irgendeinem Feld (z. B. beim Eintippen eines Sensornamens)
        # hätte dann ungewollt "Entsperren"/"Sperren" ausgelöst. Dieser
        # Dialog speichert ohnehin automatisch bei jeder Änderung (siehe
        # Klassendoc), es gibt also kein sinnvolles "Enter bestätigt"-Ziel.
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)

        self._reload_sensor_list()
        self._apply_state()
        connect_language_changed(self.retranslate_ui)

    def retranslate_ui(self) -> None:
        self.setWindowTitle(t("sensor_database_dialog_title"))
        self._add_sensor_button.setText(t("add_sensor_button"))
        self._remove_sensor_button.setText(t("remove_sensor_button"))
        self._add_axis_button.setText(t("add_axis_button"))
        self._remove_axis_button.setText(t("remove_axis_button"))
        self._add_range_button.setText(t("add_range_button"))
        self._remove_range_button.setText(t("remove_range_button"))
        self._close_button.setText(t("close_button"))
        self._update_tree_headers()
        # Baut auch die pro-Achse eingebetteten Hinweis-Labels (siehe
        # `_install_axis_widgets`) sowie die Kategorie-Kopfzeilen der
        # Sensor-Liste (siehe `_reload_sensor_list`) mit neu - keine
        # eigene zentrale Stelle wie `_update_tree_headers()`, da beide
        # individuell erzeugt werden. Unschädlich fürs Neuladen: alle
        # Änderungen sind ohnehin schon gespeichert (siehe
        # `_save_current_sensor`), es geht nichts verloren.
        selected_id = self._current_sensor_id
        self._reload_sensor_list()
        if selected_id is not None:
            self._select_sensor_by_id(selected_id)

    def _update_tree_headers(self) -> None:
        # Sensitivitätswert/Einheit bekommen bewusst KEINE Kopfzeilen-
        # Beschriftung: sie gehören nur zur Messbereich-Zeile (Kind einer
        # Achse, mindestens eine pro Achse vorhanden) und nicht zur Achse
        # selbst - eine generische Spaltenüberschrift für beide Ebenen
        # gemeinsam war genau die ursprüngliche Verwirrung (leere Zellen
        # ohne erkennbaren Bezug).
        self._tree.setHeaderLabels(
            [
                t("sensor_col_axis"),
                t("sensor_col_signal_type"),
                "",
                "",
            ]
        )

    # ------------------------------------------------------------------ #
    # Sperr-/Passwortschutz
    # ------------------------------------------------------------------ #

    def _apply_state(self) -> None:
        """Wendet Sperrzustand UND aktuelle Sensor-Auswahl auf alle
        Widgets an - EINZIGE Stelle, die beides kombiniert (siehe
        `_show_sensor`/`_set_locked`), damit z. B. ein Sensorwechsel
        während der Sperre nicht versehentlich Bearbeitung freischaltet.

        Felder bleiben bei Sperre LESBAR/kopierbar (`setReadOnly`/
        `NoEditTriggers`), statt komplett deaktiviert zu werden
        (`setEnabled(False)`) - genau das war der Zweck des
        Passwortschutzes: Werte weiterhin einsehen können, nur nicht
        versehentlich verändern.
        """
        has_sensor = self._current_sensor_id is not None
        editable = has_sensor and not self._locked

        self._category_combo.setEnabled(has_sensor)
        self._category_combo.lineEdit().setReadOnly(self._locked)
        self._name_edit.setEnabled(has_sensor)
        self._manufacturer_edit.setEnabled(has_sensor)
        self._serial_edit.setEnabled(has_sensor)
        self._notes_edit.setEnabled(has_sensor)
        for line_edit in (
            self._name_edit,
            self._manufacturer_edit,
            self._serial_edit,
            self._notes_edit,
        ):
            line_edit.setReadOnly(self._locked)

        self._tree.setEnabled(has_sensor)
        self._tree.setEditTriggers(
            QTreeWidget.EditTrigger.NoEditTriggers
            if self._locked
            else (
                QTreeWidget.EditTrigger.DoubleClicked
                | QTreeWidget.EditTrigger.EditKeyPressed
                | QTreeWidget.EditTrigger.SelectedClicked
            )
        )
        for i in range(self._tree.topLevelItemCount()):
            combo = self._tree.itemWidget(self._tree.topLevelItem(i), _COL_SIGNAL_TYPE)
            if combo is not None:
                combo.setEnabled(editable)

        self._add_sensor_button.setEnabled(not self._locked)
        self._remove_sensor_button.setEnabled(editable)
        self._add_axis_button.setEnabled(editable)
        self._remove_axis_button.setEnabled(editable)
        self._add_range_button.setEnabled(editable)
        self._remove_range_button.setEnabled(editable)

        self._unlock_button.setText(
            t("sensor_db_unlock_button") if self._locked else t("sensor_db_relock_button")
        )
        self._lock_status_label.setText(
            t("sensor_db_locked_status") if self._locked else t("sensor_db_unlocked_status")
        )

    def _set_locked(self, locked: bool) -> None:
        self._locked = locked
        self._apply_state()

    def _on_unlock_button_clicked(self) -> None:
        if not self._locked:
            self._set_locked(True)
            return
        password, ok = QInputDialog.getText(
            self,
            t("sensor_db_enter_password_title"),
            t("sensor_db_enter_password_body"),
            QLineEdit.EchoMode.Password,
        )
        if not ok:
            return
        if password != _UNLOCK_PASSWORD:
            QMessageBox.warning(
                self, t("sensor_db_enter_password_title"), t("sensor_db_wrong_password_body")
            )
            return
        self._set_locked(False)

    # ------------------------------------------------------------------ #
    # Sensor-Liste
    # ------------------------------------------------------------------ #

    def _reload_sensor_list(self) -> None:
        """Baut die Sensor-Liste neu auf, gruppiert nach `category`
        (siehe data/sensor_models.py::SensorEntry.category Moduldoc) -
        Sensoren ohne Kategorie landen gesammelt unter
        `sensor_uncategorized_label`. Kategorie-Kopfzeilen sind wie die
        Messbereich-Kopfzeile (siehe `_build_variant_header_item`) weder
        auswählbar noch editierbar, nur die Sensor-Blätter darunter."""
        self._loading = True
        self._sensor_list.clear()
        groups: dict[str, QTreeWidgetItem] = {}
        for sensor in self._sensor_database.list_sensors():
            category = sensor.category or t("sensor_uncategorized_label")
            group_item = groups.get(category)
            if group_item is None:
                group_item = QTreeWidgetItem([category])
                group_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                font = group_item.font(0)
                font.setBold(True)
                group_item.setFont(0, font)
                self._sensor_list.addTopLevelItem(group_item)
                groups[category] = group_item
            leaf = QTreeWidgetItem([sensor.name or t("new_sensor_default_name")])
            leaf.setData(0, _ROLE_SENSOR_ID, sensor.id)
            group_item.addChild(leaf)
        self._sensor_list.expandAll()
        self._loading = False

        first_sensor_item = self._first_sensor_item()
        if first_sensor_item is not None:
            self._sensor_list.setCurrentItem(first_sensor_item)
        else:
            self._show_sensor(None)

    def _first_sensor_item(self) -> Optional[QTreeWidgetItem]:
        for i in range(self._sensor_list.topLevelItemCount()):
            group_item = self._sensor_list.topLevelItem(i)
            if group_item.childCount() > 0:
                return group_item.child(0)
        return None

    def _select_sensor_by_id(self, sensor_id: str) -> None:
        for i in range(self._sensor_list.topLevelItemCount()):
            group_item = self._sensor_list.topLevelItem(i)
            for j in range(group_item.childCount()):
                leaf = group_item.child(j)
                if leaf.data(0, _ROLE_SENSOR_ID) == sensor_id:
                    self._sensor_list.setCurrentItem(leaf)
                    return

    def _on_sensor_selection_changed(
        self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None
    ) -> None:
        if self._loading:
            return
        # `current.data(0, _ROLE_SENSOR_ID)` ist `None` für eine
        # Kategorie-Kopfzeile (nie damit versehen) - wird hier genauso wie
        # "keine Auswahl" behandelt.
        sensor_id = current.data(0, _ROLE_SENSOR_ID) if current is not None else None
        self._current_sensor_id = sensor_id
        sensor = self._sensor_database.get_sensor(sensor_id) if sensor_id else None
        self._show_sensor(sensor)

    def _on_add_sensor_clicked(self) -> None:
        if self._locked:
            return
        sensor = SensorEntry(name=t("new_sensor_default_name"))
        self._sensor_database.add_sensor(sensor)
        self._reload_sensor_list()
        self._select_sensor_by_id(sensor.id)
        self._name_edit.setFocus()
        self._name_edit.selectAll()

    def _on_remove_sensor_clicked(self) -> None:
        if self._locked or self._current_sensor_id is None:
            return
        sensor = self._sensor_database.get_sensor(self._current_sensor_id)
        name = sensor.name if sensor is not None else ""
        reply = QMessageBox.question(
            self,
            t("confirm_delete_title"),
            t("confirm_delete_sensor_body", name=name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._sensor_database.delete_sensor(self._current_sensor_id)
        self._current_sensor_id = None
        self._reload_sensor_list()

    # ------------------------------------------------------------------ #
    # Stammdaten + Achsen/Varianten-Baum für den ausgewählten Sensor
    # ------------------------------------------------------------------ #

    def _show_sensor(self, sensor: Optional[SensorEntry]) -> None:
        self._loading = True
        # Autovervollständigungs-Liste bei jedem Sensorwechsel neu
        # aufbauen, damit zwischenzeitlich an anderen Sensoren vergebene
        # neue Kategorien sofort verfügbar sind (siehe
        # `config/sensor_database.py::list_categories`).
        self._category_combo.clear()
        self._category_combo.addItems(self._sensor_database.list_categories())
        self._category_combo.setCurrentText(sensor.category if sensor else "")
        self._name_edit.setText(sensor.name if sensor else "")
        self._manufacturer_edit.setText(sensor.manufacturer if sensor else "")
        self._serial_edit.setText(sensor.serial_number if sensor else "")
        self._notes_edit.setText(sensor.notes if sensor else "")

        self._tree.clear()
        if sensor is not None:
            for axis in sensor.channels:
                self._add_axis_item(axis)
            self._tree.expandAll()
        self._loading = False
        self._apply_state()

    @staticmethod
    def _align_item_columns(item: QTreeWidgetItem, column_count: int = 4) -> None:
        """Setzt links-/vertikal-zentrierte Textausrichtung für alle
        Spalten eines Items - Qt richtet Text in Baum-Zellen sonst nur
        horizontal aus, vertikal am oberen Rand, was bei der erhöhten
        Zeilenhöhe (siehe `__init__`) unruhig aussieht."""
        alignment = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        for col in range(column_count):
            item.setTextAlignment(col, alignment)

    def _add_axis_item(self, axis: SensorChannelDefinition) -> QTreeWidgetItem:
        item = QTreeWidgetItem([axis.label, "", "", ""])
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self._align_item_columns(item)
        self._tree.addTopLevelItem(item)
        self._install_axis_widgets(item, axis)
        # Exklusive "Kopfzeile" NUR für die Messbereich-Ebene - siehe
        # `_build_variant_header_item` - immer das ERSTE Kind, direkt
        # sichtbar beim Aufklappen, vor den echten Varianten.
        item.addChild(self._build_variant_header_item())
        for variant in axis.ranges:
            range_item = self._build_range_item(variant)
            item.addChild(range_item)
        return item

    def _install_axis_widgets(self, item: QTreeWidgetItem, axis: SensorChannelDefinition) -> None:
        """Setzt die Signaltyp-Combo einer Achsen-Zeile und blockiert die
        Sensitivitätswert-/Einheit-Spalten auf DIESER Zeile (die haben
        hier keine Bedeutung - ihre exklusive Kopfzeile lebt stattdessen
        im ersten Kind, siehe `_build_variant_header_item`). Ohne diesen
        Block wären die dort leeren Zellen trotzdem technisch
        beschreibbar (Item-Flags gelten zeilenweise, nicht pro Spalte).

        `setItemWidget` wirkt nur auf bereits im Baum eingehängte Items -
        `item` muss daher VOR diesem Aufruf via `addTopLevelItem` in
        `self._tree` eingefügt worden sein (siehe `_add_axis_item`).
        """
        signal_combo = QComboBox()
        for signal_type in SignalType:
            signal_combo.addItem(t(SIGNAL_TYPE_LABEL_KEYS[signal_type]), signal_type.value)
        index = signal_combo.findData(axis.signal_type.value)
        signal_combo.setCurrentIndex(index if index >= 0 else 0)
        signal_combo.currentIndexChanged.connect(self._save_current_sensor)
        self._tree.setItemWidget(item, _COL_SIGNAL_TYPE, signal_combo)

        for col in (_COL_SENSITIVITY, _COL_SENSITIVITY_UNIT):
            blocker = QLabel("")
            blocker.setEnabled(False)
            self._tree.setItemWidget(item, col, blocker)

    @staticmethod
    def _build_variant_header_item() -> QTreeWidgetItem:
        """Exklusive "Kopfzeile" NUR für die Messbereich-Kindzeilen einer
        Achse (siehe Klassendoc) - erscheint als allererstes Kind, direkt
        beim Aufklappen sichtbar. `_ROLE_VARIANT_HEADER` markiert sie,
        damit sie beim Lesen/Entfernen NICHT als echte Variante behandelt
        wird (siehe `_iter_variant_items`).

        Bewusst NICHT `Qt.ItemFlag.NoItemFlags`: ein Item ohne
        `ItemIsEnabled` rendert Qt standardmäßig mit der "deaktiviert"-
        Palette (stark abgeblendet, schwer lesbar) - hier reicht
        `ItemIsEnabled` allein (normaler Kontrast), OHNE
        `ItemIsSelectable`/`ItemIsEditable` bleibt die Zeile trotzdem
        weder auswählbar noch editierbar. Fett+Kursiv statt Farbe hebt
        sie zusätzlich optisch von echten Datenzeilen ab, ohne eine feste
        (Theme-unabhängige) Farbe zu erzwingen.
        """
        item = QTreeWidgetItem(
            [t("sensor_col_range"), t("col_unit"), t("sensor_col_sensitivity"), t("col_unit")]
        )
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        item.setData(0, _ROLE_VARIANT_HEADER, True)
        font = item.font(_COL_LABEL)
        font.setBold(True)
        font.setItalic(True)
        for col in range(4):
            item.setFont(col, font)
        SensorDatabaseDialog._align_item_columns(item)
        return item

    @staticmethod
    def _build_range_item(variant: SensorRangeVariant) -> QTreeWidgetItem:
        item = QTreeWidgetItem(
            [
                variant.label,
                variant.unit,
                format_optional_float(variant.sensitivity_value),
                variant.sensitivity_unit,
            ]
        )
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        SensorDatabaseDialog._align_item_columns(item)
        return item

    @staticmethod
    def _iter_variant_items(axis_item: QTreeWidgetItem):
        """Liefert nur die ECHTEN Messbereich-Varianten-Kinder einer
        Achse - überspringt die dekorative Kopfzeile (siehe
        `_build_variant_header_item`), die immer als erstes Kind
        vorhanden ist."""
        for j in range(axis_item.childCount()):
            child = axis_item.child(j)
            if child.data(0, _ROLE_VARIANT_HEADER):
                continue
            yield child

    def _on_tree_item_changed(self, _item: QTreeWidgetItem, _column: int) -> None:
        self._save_current_sensor()

    def _read_sensor_from_widgets(self) -> Optional[SensorEntry]:
        if self._current_sensor_id is None:
            return None
        channels: list[SensorChannelDefinition] = []
        for i in range(self._tree.topLevelItemCount()):
            axis_item = self._tree.topLevelItem(i)
            signal_combo = self._tree.itemWidget(axis_item, _COL_SIGNAL_TYPE)
            ranges = [
                SensorRangeVariant(
                    label=variant_item.text(_COL_LABEL),
                    unit=variant_item.text(_COL_RANGE_UNIT),
                    sensitivity_value=parse_optional_float(
                        variant_item.text(_COL_SENSITIVITY)
                    ),
                    sensitivity_unit=variant_item.text(_COL_SENSITIVITY_UNIT),
                )
                for variant_item in self._iter_variant_items(axis_item)
            ]
            channels.append(
                SensorChannelDefinition(
                    label=axis_item.text(_COL_LABEL),
                    signal_type=(
                        SignalType(signal_combo.currentData())
                        if signal_combo is not None
                        else SignalType.IEPE_ACCELERATION
                    ),
                    ranges=ranges or [SensorRangeVariant()],
                )
            )
        return SensorEntry(
            id=self._current_sensor_id,
            name=self._name_edit.text().strip(),
            category=self._category_combo.currentText().strip(),
            manufacturer=self._manufacturer_edit.text().strip(),
            serial_number=self._serial_edit.text().strip(),
            notes=self._notes_edit.text().strip(),
            channels=channels,
        )

    def _save_current_sensor(self) -> None:
        if self._loading or self._locked or self._current_sensor_id is None:
            return
        sensor = self._read_sensor_from_widgets()
        if sensor is None:
            return
        self._sensor_database.update_sensor(sensor)
        current_item = self._sensor_list.currentItem()
        display_name = sensor.name or t("new_sensor_default_name")
        current_group = current_item.parent() if current_item is not None else None
        expected_category = sensor.category or t("sensor_uncategorized_label")
        if current_group is not None and current_group.text(0) != expected_category:
            # Kategorie hat sich geändert - Sensor gehört jetzt zu einem
            # anderen (ggf. neuen) Gruppenknoten, den `_reload_sensor_list`
            # bei Bedarf frisch anlegt. Ein einfaches `setText` würde den
            # Eintrag optisch in der falschen Gruppe belassen.
            self._reload_sensor_list()
            self._select_sensor_by_id(sensor.id)
        elif current_item is not None and current_item.text(0) != display_name:
            current_item.setText(0, display_name)

    # ------------------------------------------------------------------ #
    # Achsen/Varianten hinzufügen/entfernen
    # ------------------------------------------------------------------ #

    def _on_add_axis_clicked(self) -> None:
        if self._locked or self._current_sensor_id is None:
            return
        axis = SensorChannelDefinition(label=t("new_axis_default_label"))
        item = self._add_axis_item(axis)
        item.setExpanded(True)
        self._tree.setCurrentItem(item)
        self._save_current_sensor()

    def _on_remove_axis_clicked(self) -> None:
        item = self._tree.currentItem()
        if self._locked or item is None:
            return
        top_item = item if item.parent() is None else item.parent()
        index = self._tree.indexOfTopLevelItem(top_item)
        if index < 0:
            return
        self._tree.takeTopLevelItem(index)
        self._save_current_sensor()

    def _on_add_range_clicked(self) -> None:
        item = self._tree.currentItem()
        if self._locked or item is None:
            return
        axis_item = item if item.parent() is None else item.parent()
        range_item = self._build_range_item(SensorRangeVariant())
        axis_item.addChild(range_item)
        axis_item.setExpanded(True)
        self._save_current_sensor()

    def _on_remove_range_clicked(self) -> None:
        item = self._tree.currentItem()
        if self._locked or item is None or item.parent() is None:
            return
        if item.data(0, _ROLE_VARIANT_HEADER):
            # Die dekorative Kopfzeile selbst ist nie entfernbar (siehe
            # `_build_variant_header_item`).
            return
        axis_item = item.parent()
        if sum(1 for _ in self._iter_variant_items(axis_item)) <= 1:
            QMessageBox.information(
                self, t("sensor_database_dialog_title"), t("range_minimum_one_body")
            )
            return
        axis_item.removeChild(item)
        self._save_current_sensor()
