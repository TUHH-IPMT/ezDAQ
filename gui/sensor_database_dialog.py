"""
gui/sensor_database_dialog.py

Management dialog for the sensor catalog (see data/sensor_models.py,
config/sensor_database.py) - a pure CRUD interface (create/edit/delete
sensors, axes, measurement-range variants).

Deliberately NO automatic apply function: `ChannelParameterDialog`
(see `gui/widgets/channel_table.py`) only offers a quick-access button
that opens EXACTLY THIS dialog - the user looks up the appropriate
value manually and copy-pastes it into the channel settings themselves.
This dialog serves solely to maintain the catalog itself.

Save behavior: every change (text field, table cell, adding/removing an
axis/variant) is persisted IMMEDIATELY via `SensorDatabaseManager` - no
OK/Cancel; the catalog behaves like a small, continuously maintained
database rather than a form.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
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
from gui.dialogs import confirm_delete
from gui.widgets.channel_table import SIGNAL_TYPE_LABEL_KEYS
from gui.widgets.spinbox import format_optional_float, parse_optional_float

_ROLE_SENSOR_ID = int(Qt.ItemDataRole.UserRole)
# Marks the decorative "header row" beneath an axis (see
# `_build_variant_header_item`) - distinguishes it from actual
# measurement-range variants when reading/removing.
_ROLE_VARIANT_HEADER = int(Qt.ItemDataRole.UserRole) + 1

# Column indices of the axis/variant tree. Axis rows populate
# LABEL/SIGNAL_TYPE, variant rows (children) populate LABEL (as free-form
# range text, e.g. "±50" - deliberately NO separate numeric min/max
# fields) /RANGE_UNIT/SENSITIVITY/SENSITIVITY_UNIT - see
# `_add_axis_item`/`_build_range_item`. Column 1 deliberately has a
# DIFFERENT meaning depending on the row type (signal type on the axis,
# range unit on the variant) - thanks to the exclusive header row per
# level (see `_build_variant_header_item`), it nonetheless stays clear
# in place what is meant at any given point.
_COL_LABEL = 0
_COL_SIGNAL_TYPE = 1
_COL_RANGE_UNIT = 1
_COL_SENSITIVITY = 2
_COL_SENSITIVITY_UNIT = 3

class SensorDatabaseDialog(QDialog):
    """Manages the sensor catalog: sensors as a list on the left, master
    data + axes/measurement-range variants as a tree on the right.

    Write protection: the dialog ALWAYS starts locked (read/copy only).
    A simple edit/lock toggle guards against accidental changes; it is
    not a security feature.
    """

    def __init__(
        self, sensor_database: SensorDatabaseManager, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._sensor_database = sensor_database
        self._current_sensor_id: Optional[str] = None
        # Prevents programmatic population of the widgets (see
        # `_show_sensor`) from being interpreted as a user change and
        # thus unintentionally saved right back.
        self._loading = False
        # ALWAYS starts locked (see class docstring) - no opt-in anymore.
        self._locked = True
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(300)
        self._save_timer.timeout.connect(self._save_current_sensor_now)

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
        # Tree instead of a flat list: sensors are grouped by `category`
        # (see `_reload_sensor_list`) - category header rows, like the
        # measurement-range header row, are neither selectable nor
        # editable; only sensor leaves are.
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
        # Free text instead of a fixed selection list (see
        # data/sensor_models.py::SensorEntry.category module docstring) -
        # editable combo box with autocomplete from the categories
        # already in use, so no typo duplicates like "Force" vs.
        # "Force measurement" arise.
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
        # Slightly more row height (padding instead of a fixed pixel
        # count, so it scales with a different font size/DPI) - makes
        # the second level (measurement-range variants) in particular
        # feel less cramped.
        self._tree.setStyleSheet("QTreeView::item { padding: 4px 0; }")
        # Cell selection instead of row selection: a click highlights
        # only the clicked cell, not the whole row - otherwise it's
        # nearly impossible to select ONE specific value (e.g. to copy
        # it) without the entire row visually "getting in the way" as
        # highlighted.
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

        # Without this, Qt automatically makes the FIRST QPushButton
        # created (here `_unlock_button`) the dialog's default button -
        # pressing Enter in any field (e.g. while typing a sensor name)
        # would then unintentionally trigger "Unlock"/"Lock". This
        # dialog saves automatically on every change anyway (see class
        # docstring), so there is no meaningful "Enter confirms" target.
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
        # Also rebuilds the per-axis embedded hint labels (see
        # `_install_axis_widgets`) and the sensor list's category header
        # rows (see `_reload_sensor_list`) - there is no dedicated
        # central spot like `_update_tree_headers()` for these since
        # both are created individually. Harmless to reload: all changes
        # are already saved anyway (see `_save_current_sensor`), so
        # nothing is lost.
        selected_id = self._current_sensor_id
        self._reload_sensor_list()
        if selected_id is not None:
            self._select_sensor_by_id(selected_id)

    def _update_tree_headers(self) -> None:
        # Sensitivity value/unit deliberately get NO header label: they
        # belong only to the measurement-range row (a child of an axis,
        # at least one present per axis) and not to the axis itself - a
        # generic column header shared by both levels was exactly the
        # original source of confusion (empty cells with no recognizable
        # relation).
        self._tree.setHeaderLabels(
            [
                t("sensor_col_axis"),
                t("sensor_col_signal_type"),
                "",
                "",
            ]
        )

    # ------------------------------------------------------------------ #
    # Lock/password protection
    # ------------------------------------------------------------------ #

    def _apply_state(self) -> None:
        """Applies the lock state AND the current sensor selection to all
        widgets - the SINGLE place that combines both (see
        `_show_sensor`/`_set_locked`), so that e.g. switching sensors
        while locked doesn't accidentally unlock editing.

        While locked, fields stay READABLE/copyable (`setReadOnly`/
        `NoEditTriggers`) rather than being fully disabled
        (`setEnabled(False)`) - that is precisely the point of the
        password protection: values remain viewable, just not
        accidentally editable.
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
            t("sensor_db_edit_button") if self._locked else t("sensor_db_relock_button")
        )
        self._lock_status_label.setText(
            t("sensor_db_locked_status") if self._locked else t("sensor_db_unlocked_status")
        )

    def _set_locked(self, locked: bool) -> None:
        self._locked = locked
        self._apply_state()

    def _on_unlock_button_clicked(self) -> None:
        self._set_locked(not self._locked)

    # ------------------------------------------------------------------ #
    # Sensor list
    # ------------------------------------------------------------------ #

    def _reload_sensor_list(self) -> None:
        """Rebuilds the sensor list, grouped by `category` (see
        data/sensor_models.py::SensorEntry.category module docstring) -
        sensors without a category are collected under
        `sensor_uncategorized_label`. Category header rows, like the
        measurement-range header row (see `_build_variant_header_item`),
        are neither selectable nor editable; only the sensor leaves
        beneath them are."""
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
        # `current.data(0, _ROLE_SENSOR_ID)` is `None` for a category
        # header row (never assigned one) - treated here the same as
        # "no selection".
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
        if not confirm_delete(self, t("confirm_delete_sensor_body", name=name)):
            return
        self._sensor_database.delete_sensor(self._current_sensor_id)
        self._current_sensor_id = None
        self._reload_sensor_list()

    # ------------------------------------------------------------------ #
    # Master data + axis/variant tree for the selected sensor
    # ------------------------------------------------------------------ #

    def _show_sensor(self, sensor: Optional[SensorEntry]) -> None:
        self._loading = True
        # Rebuild the autocomplete list on every sensor switch, so new
        # categories assigned to other sensors in the meantime are
        # immediately available (see
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
        """Sets left-aligned/vertically-centered text alignment for all
        columns of an item - Qt otherwise only aligns text in tree cells
        horizontally, vertically pinned to the top, which looks
        unsettled with the increased row height (see `__init__`)."""
        alignment = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        for col in range(column_count):
            item.setTextAlignment(col, alignment)

    def _add_axis_item(self, axis: SensorChannelDefinition) -> QTreeWidgetItem:
        item = QTreeWidgetItem([axis.label, "", "", ""])
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self._align_item_columns(item)
        self._tree.addTopLevelItem(item)
        self._install_axis_widgets(item, axis)
        # Exclusive "header row" ONLY for the measurement-range level -
        # see `_build_variant_header_item` - always the FIRST child,
        # immediately visible when expanded, ahead of the real variants.
        item.addChild(self._build_variant_header_item())
        for variant in axis.ranges:
            range_item = self._build_range_item(variant)
            item.addChild(range_item)
        return item

    def _install_axis_widgets(self, item: QTreeWidgetItem, axis: SensorChannelDefinition) -> None:
        """Sets up the signal-type combo box for an axis row and blocks
        the sensitivity-value/unit columns on THIS row (they have no
        meaning here - their exclusive header row lives in the first
        child instead, see `_build_variant_header_item`). Without this
        block, the otherwise-empty cells there would still be
        technically writable (item flags apply per row, not per
        column).

        `setItemWidget` only works on items already attached to the
        tree - `item` must therefore have been inserted into
        `self._tree` via `addTopLevelItem` BEFORE this call (see
        `_add_axis_item`).
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
        """Exclusive "header row" ONLY for the measurement-range child
        rows of an axis (see class docstring) - appears as the very
        first child, immediately visible when expanded.
        `_ROLE_VARIANT_HEADER` marks it so it is NOT treated as a real
        variant when reading/removing (see `_iter_variant_items`).

        Deliberately NOT `Qt.ItemFlag.NoItemFlags`: by default Qt
        renders an item without `ItemIsEnabled` using the "disabled"
        palette (heavily dimmed, hard to read) - here `ItemIsEnabled`
        alone is enough (normal contrast); WITHOUT
        `ItemIsSelectable`/`ItemIsEditable` the row still remains
        neither selectable nor editable. Bold+italic instead of color
        additionally sets it visually apart from real data rows, without
        forcing a fixed (theme-independent) color.
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
        """Yields only the REAL measurement-range variant children of an
        axis - skips the decorative header row (see
        `_build_variant_header_item`), which is always present as the
        first child."""
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
        """Schedules the save after a brief pause in changes."""
        self._save_timer.start()

    def _save_current_sensor_now(self) -> None:
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
            # Category has changed - the sensor now belongs to a
            # different (possibly new) group node, which
            # `_reload_sensor_list` creates fresh as needed. A simple
            # `setText` would visually leave the entry in the wrong
            # group.
            self._reload_sensor_list()
            self._select_sensor_by_id(sensor.id)
        elif current_item is not None and current_item.text(0) != display_name:
            current_item.setText(0, display_name)

    # ------------------------------------------------------------------ #
    # Add/remove axes/variants
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
            # The decorative header row itself is never removable (see
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
