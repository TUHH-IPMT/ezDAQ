"""
gui/widgets/channel_table.py

Reusable table widget for editing a channel configuration.

Used by `gui/setup_view.py`. Encapsulates the conversion between
`data.models.Channel` objects and the individual cell widgets (checkbox,
text fields, combo boxes, spin boxes).
"""

from __future__ import annotations

import re

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
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

from config.sensor_database import SensorDatabaseManager
from core.measurement import device_name_from_hw_channel
from data.models import (
    ADC_TIMING_MODES,
    NI9235_BRIDGE_TYPES,
    THERMOCOUPLE_TYPES,
    Channel,
    DeviceInfo,
    ModuleType,
    SignalType,
)
from gui.i18n import connect_language_changed, t
from gui.widgets.spinbox import PrecisionDoubleSpinBox
from gui.theme import (
    connect_theme_changed,
    disabled_text_color,
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
    "col_parameters",
]

# Detects the module part of a device/channel name, e.g. "Mod2" in
# "cDAQ9185-0217ED5EMod2". The chassis part in front (often a long serial
# number like "cDAQ9185-0217ED5E") is irrelevant for distinguishing
# channels - only which module/channel is meant matters (see
# `_short_hw_channel_text`).
_MODULE_SUFFIX_PATTERN = re.compile(r"Mod\d+")

# Which signal types a module supports at the hardware level (see
# `hardware/ni9215.py`/`hardware/ni9234.py`/`hardware/ni9235.py`, each of
# which rejects the wrong signal type with an `AcquisitionError`). The
# channel table restricts the signal type selection per row accordingly,
# instead of letting the error only surface when the measurement starts.
_MODULE_SIGNAL_TYPES: dict[ModuleType, list[SignalType]] = {
    ModuleType.NI9215: [SignalType.VOLTAGE],
    ModuleType.NI9234: [SignalType.VOLTAGE, SignalType.IEPE_ACCELERATION],
    ModuleType.NI9210: [SignalType.THERMOCOUPLE],
    ModuleType.NI9213: [SignalType.THERMOCOUPLE],
    ModuleType.NI9235: [SignalType.STRAIN],
}

# Translated display labels for the signal type selection dialog/button.
# The actual value (Channel.signal_type.value, e.g. "voltage") stays
# stable independent of the UI language (persistence/hardware
# comparisons) - it is stored as the button property "signal_type", see
# `_create_signal_type_widget`. Deliberately WITHOUT an underscore prefix:
# also imported by `gui/sensor_database_dialog.py` so the same translation
# table doesn't have to be duplicated.
SIGNAL_TYPE_LABEL_KEYS: dict[SignalType, str] = {
    SignalType.VOLTAGE: "signal_type_voltage",
    SignalType.IEPE_ACCELERATION: "signal_type_iepe",
    SignalType.THERMOCOUPLE: "signal_type_thermocouple",
    SignalType.STRAIN: "signal_type_strain",
}

# Translated display labels per ADC timing mode (see
# `data/models.py::ADC_TIMING_MODES`) - available ONLY on the NI9213 (see
# `ChannelParameterDialog`).
_ADC_TIMING_MODE_LABEL_KEYS: dict[str, str] = {
    "HIGH_RESOLUTION": "adc_timing_mode_high_resolution",
    "HIGH_SPEED": "adc_timing_mode_high_speed",
}

# Translated display labels per NI9235 quarter-bridge variant (see
# `data/models.py::NI9235_BRIDGE_TYPES`) - analogous to
# `_ADC_TIMING_MODE_LABEL_KEYS`.
_BRIDGE_TYPE_LABEL_KEYS: dict[str, str] = {
    "QUARTER_BRIDGE_I": "bridge_type_quarter_i",
    "QUARTER_BRIDGE_II": "bridge_type_quarter_ii",
}

_COL_NUMBER = 0
_COL_ENABLED = 1
_COL_HW_CHANNEL = 2
_COL_NAME = 3
_COL_UNIT = 4
_COL_SIGNAL = 5
_COL_PARAMETERS = 6

_ROLE_CHANNEL_VALUE = int(Qt.ItemDataRole.UserRole)


class HardwareChannelPickerDialog(QDialog):
    """Dialog for selecting a hardware channel, grouped by device/module.

    Replaces the previous dropdown selection in the channel table: with
    several modules each having many channels, a tree view grouped by
    device is clearer than one long, flat list. Channels already assigned
    to ANOTHER row are shown but disabled (not selectable) - the same
    physical channel must not be assigned twice. Also disabled (and
    ALWAYS, even if `current_channel` happens to point to such a module):
    channels of a device whose module type `hardware/nidaq_device.py::
    _map_product_type` does not recognize (`DeviceInfo.module_type is
    None`) - without a known module type there is no matching hardware
    device class (see `core/measurement.py::_DEVICE_CLASSES`), and a
    selection would either fail when the measurement starts or (worse) be
    silently misconfigured via the former NI9215 fallback in
    `_apply_device_constraint`.
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
            # No device discovery has run yet (or no hardware was found) -
            # instead of an empty tree, show a clear hint about what to do.
            # No OK button, since there is (as yet) nothing to select.
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
            # Checked BEFORE the module type: for a device that doesn't
            # answer, the module type comes from the same stale NI-DAQmx
            # configuration cache that made the device look available at
            # all - labeling it "not supported" would name the wrong
            # cause (see `hardware/nidaq_device.py::_is_device_connected`).
            is_offline = not device.is_connected
            is_unsupported_module = not is_offline and device.module_type is None
            if is_offline:
                module_info = f" [{t('device_not_connected')}]"
            elif is_unsupported_module:
                module_info = f" [{t('device_module_unsupported')}]"
            else:
                module_info = f" [{device.module_type.value}]"
            device_item = QTreeWidgetItem([f"{device.device_name} - {device.product_type}{module_info}"])
            device_item.setFlags(device_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            if is_unsupported_module or is_offline:
                # Flags alone (ItemIsEnabled/-Selectable) are NOT enough to
                # make an item visually recognizable as disabled - Qt does
                # not reliably apply the palette's disabled color group for
                # this automatically (see
                # `gui/theme.py::disabled_text_color`). Therefore set
                # explicitly here AND below on the individual channel items.
                device_item.setForeground(0, QBrush(disabled_text_color()))
            channels = device.physical_channels or [
                f"{device.device_name}/ai{i}" for i in range(device.num_channels)
            ]
            for channel in channels:
                is_used = channel in used_channels and channel != current_channel
                if is_offline:
                    label = t("hw_channel_device_offline", channel=channel)
                elif is_unsupported_module:
                    label = t("hw_channel_unsupported_module", channel=channel)
                elif is_used:
                    label = t("hw_channel_already_used", channel=channel)
                else:
                    label = channel
                channel_item = QTreeWidgetItem([label])
                channel_item.setData(0, _ROLE_CHANNEL_VALUE, channel)
                if is_used or is_unsupported_module or is_offline:
                    channel_item.setFlags(
                        channel_item.flags()
                        & ~Qt.ItemFlag.ItemIsEnabled
                        & ~Qt.ItemFlag.ItemIsSelectable
                    )
                    channel_item.setForeground(0, QBrush(disabled_text_color()))
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
    """Dialog for selecting the signal type of a row.

    Analogous to `HardwareChannelPickerDialog` (its own window instead of
    an inline combo box) - without grouping here, since `allowed` is
    already restricted to the types supported by the row's module (see
    `_MODULE_SIGNAL_TYPES`) and therefore contains only one or two
    entries.
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
            item = QTreeWidgetItem([t(SIGNAL_TYPE_LABEL_KEYS[signal_type])])
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


class TwoPointCalibrationDialog(QDialog):
    """Dialog for two-point calibration of a channel.

    Computes scale and offset from two known reference points (measured
    raw value vs. known target value) - e.g. for a thermocouple, the ice
    point (0 °C) and boiling point (100 °C). The two points are stored
    with the channel (see `data/models.py::Channel.cal_point1_measured`
    etc.) so the calibration can be reviewed later, or a single point
    re-set, without having to re-enter both points - `scale`/`offset`
    remain the actually applied values regardless; the points themselves
    have no direct effect on the measurement.

    Calculation (linear two-point form):
        scale  = (reference2 - reference1) / (measured2 - measured1)
        offset = reference1 - scale * measured1
    """

    def __init__(
        self,
        point1_measured: float | None,
        point1_reference: float | None,
        point2_measured: float | None,
        point2_reference: float | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("two_point_cal_dialog_title"))
        self._result_scale = 1.0
        self._result_offset = 0.0
        self._point1_measured = point1_measured
        self._point1_reference = point1_reference
        self._point2_measured = point2_measured
        self._point2_reference = point2_reference

        layout = QVBoxLayout(self)

        hint = QLabel(t("two_point_cal_hint"))
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()

        self._point1_measured_spin = PrecisionDoubleSpinBox()
        self._point1_measured_spin.setRange(-1e9, 1e9)
        self._point1_measured_spin.setValue(point1_measured if point1_measured is not None else 0.0)
        form.addRow(t("cal_point1_measured_label"), self._point1_measured_spin)

        self._point1_reference_spin = PrecisionDoubleSpinBox()
        self._point1_reference_spin.setRange(-1e9, 1e9)
        self._point1_reference_spin.setValue(point1_reference if point1_reference is not None else 0.0)
        form.addRow(t("cal_point1_reference_label"), self._point1_reference_spin)

        self._point2_measured_spin = PrecisionDoubleSpinBox()
        self._point2_measured_spin.setRange(-1e9, 1e9)
        self._point2_measured_spin.setValue(point2_measured if point2_measured is not None else 1.0)
        form.addRow(t("cal_point2_measured_label"), self._point2_measured_spin)

        self._point2_reference_spin = PrecisionDoubleSpinBox()
        self._point2_reference_spin.setRange(-1e9, 1e9)
        self._point2_reference_spin.setValue(point2_reference if point2_reference is not None else 1.0)
        form.addRow(t("cal_point2_reference_label"), self._point2_reference_spin)

        layout.addLayout(form)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.button(QDialogButtonBox.StandardButton.Ok).setText(t("ok"))
        button_box.button(QDialogButtonBox.StandardButton.Cancel).setText(t("cancel"))
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        self.resize(340, 220)

    def _on_accept(self) -> None:
        m1 = self._point1_measured_spin.value()
        r1 = self._point1_reference_spin.value()
        m2 = self._point2_measured_spin.value()
        r2 = self._point2_reference_spin.value()
        if m1 == m2:
            QMessageBox.warning(self, t("error"), t("cal_identical_points_error"))
            return

        self._result_scale = (r2 - r1) / (m2 - m1)
        self._result_offset = r1 - self._result_scale * m1
        self._point1_measured, self._point1_reference = m1, r1
        self._point2_measured, self._point2_reference = m2, r2
        self.accept()

    def result_scale(self) -> float:
        return self._result_scale

    def result_offset(self) -> float:
        return self._result_offset

    def point1(self) -> tuple[float | None, float | None]:
        return self._point1_measured, self._point1_reference

    def point2(self) -> tuple[float | None, float | None]:
        return self._point2_measured, self._point2_reference


class ChannelParameterDialog(QDialog):
    """Dialog for editing channel parameters.

    Scale and offset are ALWAYS editable, regardless of the signal type:
    even when the driver already delivers physical units for IEPE/
    thermocouple (g or °C respectively), an additional linear conversion
    is often useful (e.g. g -> m/s² via scale, or °C -> °F via scale 1.8 +
    offset 32) - so neither should be automatically locked/hidden. In
    addition, the dialog shows ONLY the field that is hardware-mandatory
    for the row's current signal type (sensitivity for IEPE, thermocouple
    type for thermocouple, gage factor/bridge type/lead wire resistance
    for strain) - for voltage, this extra field is omitted entirely. The
    ADC timing mode is additionally shown only if the row's module is an
    NI9213 (the NI9210 has a fixed sample rate without this option).

    Replaces the previous, always-visible columns (scale/offset/
    sensitivity/thermocouple type): with a growing number of modules
    (currently NI9215/NI9234/NI9210/NI9213/NI9235, see
    `_MODULE_SIGNAL_TYPES`), that would mean more and more, mostly locked
    columns - a new module type with its own parameters only needs one
    more branch in this single dialog instead of a new table column.
    """

    def __init__(
        self,
        signal_type: SignalType,
        module_type: ModuleType,
        scale: float,
        offset: float,
        sensitivity: float,
        thermocouple_type: str,
        adc_timing_mode: str = "HIGH_RESOLUTION",
        strain_gage_factor: float = 0.0,
        strain_bridge_type: str = "QUARTER_BRIDGE_I",
        lead_wire_resistance_ohm: float = 0.0,
        cal_point1_measured: float | None = None,
        cal_point1_reference: float | None = None,
        cal_point2_measured: float | None = None,
        cal_point2_reference: float | None = None,
        sensor_database: SensorDatabaseManager | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("parameters_dialog_title"))

        self._signal_type = signal_type
        self._sensor_database = sensor_database
        self._cal_point1_measured = cal_point1_measured
        self._cal_point1_reference = cal_point1_reference
        self._cal_point2_measured = cal_point2_measured
        self._cal_point2_reference = cal_point2_reference

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self._sensitivity_spin: QDoubleSpinBox | None = None
        self._thermocouple_combo: QComboBox | None = None
        self._adc_timing_combo: QComboBox | None = None
        self._gage_factor_spin: QDoubleSpinBox | None = None
        self._bridge_type_combo: QComboBox | None = None
        self._lead_wire_resistance_spin: QDoubleSpinBox | None = None

        self._scale_spin = PrecisionDoubleSpinBox()
        self._scale_spin.setRange(-1e9, 1e9)
        self._scale_spin.setValue(scale)
        form.addRow(t("param_scale_label"), self._scale_spin)

        self._offset_spin = PrecisionDoubleSpinBox()
        self._offset_spin.setRange(-1e9, 1e9)
        self._offset_spin.setValue(offset)
        form.addRow(t("param_offset_label"), self._offset_spin)

        if signal_type == SignalType.IEPE_ACCELERATION:
            self._sensitivity_spin = PrecisionDoubleSpinBox()
            self._sensitivity_spin.setRange(0.0, 1e6)
            self._sensitivity_spin.setValue(sensitivity)
            form.addRow(t("param_sensitivity_label"), self._sensitivity_spin)
        elif signal_type == SignalType.THERMOCOUPLE:
            self._thermocouple_combo = QComboBox()
            self._thermocouple_combo.addItems(THERMOCOUPLE_TYPES)
            index = self._thermocouple_combo.findText(thermocouple_type)
            self._thermocouple_combo.setCurrentIndex(index if index >= 0 else 0)
            form.addRow(t("param_thermocouple_type_label"), self._thermocouple_combo)

            # ADC timing mode available only on the NI9213 (the NI9210 has
            # a fixed sample rate without this option) - see
            # hardware/ni9213.py.
            if module_type == ModuleType.NI9213:
                self._adc_timing_combo = QComboBox()
                for mode in ADC_TIMING_MODES:
                    self._adc_timing_combo.addItem(t(_ADC_TIMING_MODE_LABEL_KEYS[mode]), mode)
                self._adc_timing_combo.setToolTip(t("param_adc_timing_mode_hint"))
                index = self._adc_timing_combo.findData(adc_timing_mode)
                self._adc_timing_combo.setCurrentIndex(index if index >= 0 else 0)
                form.addRow(t("param_adc_timing_mode_label"), self._adc_timing_combo)
        elif signal_type == SignalType.STRAIN:
            self._gage_factor_spin = PrecisionDoubleSpinBox()
            self._gage_factor_spin.setRange(0.0, 1e3)
            self._gage_factor_spin.setValue(strain_gage_factor if strain_gage_factor > 0 else 2.0)
            form.addRow(t("param_gage_factor_label"), self._gage_factor_spin)

            self._bridge_type_combo = QComboBox()
            for bridge_type in NI9235_BRIDGE_TYPES:
                self._bridge_type_combo.addItem(t(_BRIDGE_TYPE_LABEL_KEYS[bridge_type]), bridge_type)
            index = self._bridge_type_combo.findData(strain_bridge_type)
            self._bridge_type_combo.setCurrentIndex(index if index >= 0 else 0)
            form.addRow(t("param_strain_bridge_type_label"), self._bridge_type_combo)

            self._lead_wire_resistance_spin = PrecisionDoubleSpinBox()
            self._lead_wire_resistance_spin.setRange(0.0, 1e6)
            self._lead_wire_resistance_spin.setValue(lead_wire_resistance_ohm)
            form.addRow(t("param_lead_wire_resistance_label"), self._lead_wire_resistance_spin)

        layout.addLayout(form)

        # Pure quick access to the sensor catalog (see
        # gui/sensor_database_dialog.py) for manually looking up the
        # sensitivity value - NO automatic takeover, the user reads off
        # the value themselves and enters it into the sensitivity field
        # via copy & paste. Only visible if there is a sensitivity field
        # at all (IEPE) AND a `SensorDatabaseManager` was passed in.
        if self._sensitivity_spin is not None and self._sensor_database is not None:
            open_db_button = QPushButton(t("menu_sensor_database"))
            open_db_button.clicked.connect(self._on_open_sensor_database_clicked)
            layout.addWidget(open_db_button)

        # Two-point calibration: a convenient way to compute scale/offset
        # from two known reference points instead of calculating them by
        # hand (see `TwoPointCalibrationDialog`). Offered only for
        # thermocouples (typical use case, e.g. ice point/boiling point) -
        # left out for voltage/IEPE so as not to clutter the selection
        # with a rarely needed option.
        if signal_type == SignalType.THERMOCOUPLE:
            cal_button = QPushButton(t("two_point_cal_button"))
            cal_button.clicked.connect(self._on_two_point_calibration_clicked)
            layout.addWidget(cal_button)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.button(QDialogButtonBox.StandardButton.Ok).setText(t("ok"))
        button_box.button(QDialogButtonBox.StandardButton.Cancel).setText(t("cancel"))
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        self.resize(340, 220)

    def _on_open_sensor_database_clicked(self) -> None:
        from gui.sensor_database_dialog import SensorDatabaseDialog

        dialog = SensorDatabaseDialog(self._sensor_database, self)
        dialog.exec()

    def _on_two_point_calibration_clicked(self) -> None:
        dialog = TwoPointCalibrationDialog(
            self._cal_point1_measured,
            self._cal_point1_reference,
            self._cal_point2_measured,
            self._cal_point2_reference,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        self._scale_spin.setValue(dialog.result_scale())
        self._offset_spin.setValue(dialog.result_offset())
        self._cal_point1_measured, self._cal_point1_reference = dialog.point1()
        self._cal_point2_measured, self._cal_point2_reference = dialog.point2()

    def scale(self) -> float:
        return self._scale_spin.value()

    def offset(self) -> float:
        return self._offset_spin.value()

    def sensitivity(self) -> float:
        return self._sensitivity_spin.value() if self._sensitivity_spin is not None else 0.0

    def thermocouple_type(self) -> str:
        return self._thermocouple_combo.currentText() if self._thermocouple_combo is not None else "K"

    def adc_timing_mode(self) -> str:
        return self._adc_timing_combo.currentData() if self._adc_timing_combo is not None else "HIGH_RESOLUTION"

    def strain_gage_factor(self) -> float:
        return self._gage_factor_spin.value() if self._gage_factor_spin is not None else 0.0

    def strain_bridge_type(self) -> str:
        return self._bridge_type_combo.currentData() if self._bridge_type_combo is not None else "QUARTER_BRIDGE_I"

    def lead_wire_resistance_ohm(self) -> float:
        return self._lead_wire_resistance_spin.value() if self._lead_wire_resistance_spin is not None else 0.0

    def cal_point1(self) -> tuple[float | None, float | None]:
        return self._cal_point1_measured, self._cal_point1_reference

    def cal_point2(self) -> tuple[float | None, float | None]:
        return self._cal_point2_measured, self._cal_point2_reference


class _PickerCell(QWidget):
    """Cell widget for columns with their own selection window (hardware
    channel, signal type): a text label with the current value on the
    left, and a compact button on the right, labeled ONLY with the
    ellipsis icon, that opens the respective selection dialog.

    Offers the same small API as a `QPushButton`
    (`setText`/`setToolTip`/`setIcon`/`setIconSize`/`clicked`), so the
    rest of the code in `ChannelTableWidget` (property handling, eliding,
    re-theming, ...) keeps working unchanged - see
    `_create_hw_channel_widget`/`_create_signal_type_widget` for details.
    """

    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 4, 0)
        # Clear spacing between the text and the selection button, so both
        # are clearly recognizable as separate elements.
        layout.setSpacing(10)

        self._label = QLabel()
        layout.addWidget(self._label, stretch=1)

        self._icon_button = QPushButton()
        # Deliberately NO setFlat(True): the normal, theme-dependent
        # colored button background (QPalette.ColorRole.Button, see
        # gui/theme.py) makes it clearer than a flat/transparent button
        # that this is a clickable area.
        self._icon_button.setFixedSize(22, 22)
        self._icon_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._icon_button.clicked.connect(self.clicked.emit)
        # Explicit vertical centering: the button has a fixed size and
        # should be centered on the text label's height, not fill the
        # full row height.
        layout.addWidget(self._icon_button, alignment=Qt.AlignmentFlag.AlignVCenter)

    def setText(self, text: str) -> None:
        self._label.setText(text)

    def setToolTip(self, text: str) -> None:  # noqa: D102 - see class docstring
        self._label.setToolTip(text)
        super().setToolTip(text)

    def setIcon(self, icon: QIcon) -> None:
        self._icon_button.setIcon(icon)

    def setIconSize(self, size: QSize) -> None:
        self._icon_button.setIconSize(size)


class _IconTextButton(QPushButton):
    """`QPushButton` with its own icon+text layout instead of the native
    QPushButton rendering (see `_create_parameter_widget`).

    Reason: Qt's built-in icon+text layout does not reliably center both
    on the same axis at some button heights/styles - the same problem
    already occurred with the navigation tiles (see
    `gui/main_window.py::_build_navigation_and_workspace`: "icon stays
    stuck at the top, text ends up vertically centered separately"). Icon
    and text are instead built here as a tightly coupled package, and this
    package as a whole is centered in the button - both labels have
    `WA_TransparentForMouseEvents` set, so clicks still trigger the button
    instead of getting caught on the labels.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._icon_label = QLabel()
        self._icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._text_label = QLabel()
        self._text_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        content_layout = QHBoxLayout()
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(8)
        content_layout.addWidget(self._icon_label, alignment=Qt.AlignmentFlag.AlignVCenter)
        content_layout.addWidget(self._text_label, alignment=Qt.AlignmentFlag.AlignVCenter)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.addStretch(1)
        layout.addLayout(content_layout)
        layout.addStretch(1)

    def setText(self, text: str) -> None:  # noqa: D102 - see class docstring
        self._text_label.setText(text)

    def setIconPixmap(self, pixmap: QPixmap) -> None:
        self._icon_label.setPixmap(pixmap)


class ChannelTableWidget(QWidget):
    """Table for editing channels (setup view).

    Each row corresponds to a `Channel`. `set_channels()`/`get_channels()`
    convert between the table and the data models.
    """

    def __init__(
        self,
        sensor_database: SensorDatabaseManager | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._sensor_database = sensor_database

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
        # Deliberately keep the first column (Active + checkbox) a bit wider.
        self._table.setColumnWidth(_COL_ENABLED, 86)
        # Numbering runs through its own first column instead of the row header.
        self._table.verticalHeader().setVisible(False)
        # Minimum height for ~6 rows + header row, so the table doesn't get
        # squeezed down to a single, barely usable row in the setup view
        # (see SetupView, which additionally embeds the whole view in a
        # QScrollArea).
        self._table.setMinimumHeight(230)
        self._table.horizontalHeader().sectionResized.connect(
            self._on_hw_channel_column_resized
        )
        layout.addWidget(self._table)

        # Discovered devices (device discovery) - determine which hardware
        # channels are selectable and which module belongs to which
        # channel (see `set_available_devices`).
        self._available_devices: list[DeviceInfo] = []
        self._available_hw_channels: list[str] = []
        self._hw_channel_to_module: dict[str, ModuleType] = {}

        # Live view display settings (trace color, background, Y range,
        # autoscale) per channel - the table has no columns of its own for
        # these (set via the "channel display" dialog in gui/live_view.py,
        # see `apply_display_settings`), but they must be passed back to
        # the `Channel` when reading the table (`_read_row`) so they are
        # preserved when the configuration is saved. The key is
        # (hardware_channel, display_name), NOT `hardware_channel` alone -
        # see `gui/live_view.py::_channel_display_key` for the rationale
        # (several not-yet-assigned channels would otherwise share the
        # same empty key).
        self._display_settings: dict[tuple[str, str], dict] = {}

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
    # Public API
    # ------------------------------------------------------------------ #

    def retranslate_ui(self) -> None:
        """Updates column headers and buttons after a language change."""
        self._table.setHorizontalHeaderLabels([t(key) for key in _COLUMN_KEYS])
        self._add_button.setText(t("add_channel_button"))
        self._remove_button.setText(t("remove_channel_button"))
        for row in range(self._table.rowCount()):
            # Without a selection, the hardware channel button shows a
            # translated placeholder (see `_create_hw_channel_widget`) -
            # once a channel is chosen, it shows the plain (not to be
            # translated) channel name, which stays unchanged.
            hw_widget = self._table.cellWidget(row, _COL_HW_CHANNEL)
            if hw_widget is not None and not hw_widget.property("hw_channel"):
                self._update_hw_channel_button_text(hw_widget)
            param_widget = self._table.cellWidget(row, _COL_PARAMETERS)
            if param_widget is not None:
                param_widget.setText(t("choose_parameters_button"))
            self._apply_module_signal_constraint(row)

    def _apply_device_constraint(self, row: int) -> None:
        """Updates the internally tracked module value of the hardware channel button.

        The module is NEVER a free choice - it is either uniquely
        determined by the discovered hardware (see
        `set_available_devices`/`_hw_channel_to_module`) or comes from a
        loaded configuration without connected hardware. It therefore has
        no column of its own, but is tracked directly alongside the
        hardware channel cell (see `_create_hw_channel_widget`).
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

        # The module may have changed - update the signal type constraint accordingly.
        self._apply_module_signal_constraint(row)

        if ModuleType(module_value) == ModuleType.NI9213:
            param_widget = self._table.cellWidget(row, _COL_PARAMETERS)
            if param_widget is not None:
                timing_mode = str(
                    param_widget.property("adc_timing_mode") or "HIGH_RESOLUTION"
                )
                device_name = device_name_from_hw_channel(hw_channel)
                for other_row in range(self._table.rowCount()):
                    if other_row == row:
                        continue
                    other_hw_widget = self._table.cellWidget(other_row, _COL_HW_CHANNEL)
                    if device_name_from_hw_channel(
                        self._get_hw_channel_text_from_widget(other_hw_widget)
                    ) != device_name:
                        continue
                    other_param_widget = self._table.cellWidget(other_row, _COL_PARAMETERS)
                    if other_param_widget is not None:
                        timing_mode = str(
                            other_param_widget.property("adc_timing_mode") or "HIGH_RESOLUTION"
                        )
                        break
                param_widget.setProperty("adc_timing_mode", timing_mode)
                self._apply_adc_timing_mode_to_module(
                    hw_widget, timing_mode, exclude_row=row
                )

    def _apply_module_signal_constraint(self, row: int) -> None:
        """Restricts a row's signal type selection to the chosen module.

        An NI9215 only supports voltage, an NI9234 supports both voltage
        and IEPE acceleration (see `_MODULE_SIGNAL_TYPES`) - a signal type
        not supported by the current module is automatically reset here to
        the first supported one, instead of only surfacing as an error
        when the measurement starts. The selection dialog
        (`SignalTypePickerDialog`, see `_on_choose_signal_type_clicked`)
        gets the same restriction freshly computed when it opens - so
        there's no fixed list here that would need to be maintained
        separately.

        If NO hardware channel is assigned yet, it's unclear which module
        (and therefore which signal types) even apply - the button is then
        completely locked, instead of offering a guessed default (currently
        NI9215/voltage) as an apparently deliberate choice.
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

        # The signal type may have changed due to the restriction - update
        # sensitivity accordingly (see `_update_parameter_state`).
        self._update_parameter_state(row)

    def _update_parameter_state(self, row: int) -> None:
        """On a signal type change, resets the now-irrelevant sensitivity
        (as a property on the parameter button, see
        `ChannelParameterDialog`) to its neutral value.

        Scale and offset are preserved for EVERY signal type - even when
        the driver already delivers physical units for IEPE/thermocouple
        (g or °C respectively), an additional linear conversion is often
        useful (e.g. g -> m/s², °C -> °F) and is therefore not
        automatically reset. Sensitivity, on the other hand, is relevant
        ONLY for IEPE (see `hardware/ni9234.py`) and is otherwise reset to
        0 - a field merely hidden in the dialog would otherwise still keep
        its old value when read out (`_read_row`). Similarly, the gage
        factor (relevant only for STRAIN, see `hardware/ni9235.py`) is
        reset when the signal type changes.
        """
        signal_widget = self._table.cellWidget(row, _COL_SIGNAL)
        param_widget = self._table.cellWidget(row, _COL_PARAMETERS)
        if signal_widget is None or param_widget is None:
            return

        signal_value = str(signal_widget.property("signal_type") or "")
        try:
            signal_type = SignalType(signal_value)
        except ValueError:
            return

        if signal_type != SignalType.IEPE_ACCELERATION:
            param_widget.setProperty("sensitivity", 0.0)
        if signal_type != SignalType.STRAIN:
            param_widget.setProperty("gage_factor", 0.0)

    def set_channels(self, channels: list[Channel]) -> None:
        """Fills the table with the given channels (replaces the content)."""
        self._table.setRowCount(0)
        for channel in channels:
            self._add_row(channel)
        self._update_row_numbers()
        # If available hardware channels are set, refresh the cells
        if self._available_hw_channels:
            self._apply_available_hw_channels_to_rows()

    def get_channels(self) -> list[Channel]:
        """Reads the current table as a list of `Channel` objects."""
        channels: list[Channel] = []
        for row in range(self._table.rowCount()):
            channels.append(self._read_row(row))
        return channels

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _on_add_clicked(self) -> None:
        if self._available_devices:
            # First not-yet-used channel as the default - prevents several
            # newly added rows from ending up with the same physical
            # channel assigned twice (see `_used_hw_channels`).
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
            # No device discovery has run yet - no more made-up placeholder
            # channel (it could never match the actual hardware). The
            # button shows "Choose channel...", and the dialog explains on
            # click that device discovery must run first (see
            # `HardwareChannelPickerDialog`).
            default_hw_channel = ""

        default_channel = Channel(
            hardware_channel=default_hw_channel,
            display_name=t("default_channel_name", index=self._table.rowCount() + 1),
        )
        self._add_row(default_channel)

    def _used_hw_channels(self, exclude_row: int | None = None) -> set[str]:
        """Collects the channels already assigned in the table.

        `exclude_row` leaves out the row itself when opening the selection
        dialog - otherwise its own, already valid channel would incorrectly
        appear as "in use".
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
        """Sets the currently discovered devices/modules (device discovery).

        Restricts the hardware channel selection per row to the physical
        channels actually present on ALL discovered devices (not just a
        single one) - which channels exist is dictated by the hardware,
        not free text. The module per row is automatically derived to
        match, see `_apply_device_constraint`.

        An empty list (no device discovery has run / no hardware found)
        only shows a corresponding hint in the selection dialog (see
        `HardwareChannelPickerDialog`) - no more free-text field.

        Devices with an unknown module type (`DeviceInfo.module_type is
        None`, see `hardware/nidaq_device.py::_map_product_type`) are
        still SHOWN in the selection dialog (but disabled there, see
        `HardwareChannelPickerDialog`), but do NOT flow into
        `_available_hw_channels` - their channels should never
        automatically be used as a default for a new row (see
        `_on_add_clicked`).

        The same applies to devices that are configured in the driver but
        do not respond (`DeviceInfo.is_connected is False`, e.g. a
        reserved network cDAQ chassis whose cable was pulled): their
        channels exist only in the NI-DAQmx configuration cache, so
        assigning them would produce a configuration that fails at
        measurement start. They stay visible but disabled in the picker,
        for the same reason as unsupported modules - so the user sees
        WHY a channel they expect is missing.
        """
        self._available_devices = devices or []
        self._hw_channel_to_module = {}
        self._available_hw_channels = []
        for device in self._available_devices:
            channels = device.physical_channels or [
                f"{device.device_name}/ai{i}" for i in range(device.num_channels)
            ]
            if device.module_type is not None and device.is_connected:
                self._available_hw_channels.extend(channels)
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
        """Builds the cell widget for the hardware channel column of a row.

        Always a button that opens `HardwareChannelPickerDialog` - even
        WITHOUT known devices (the dialog then shows a hint instead of an
        empty selection, see `HardwareChannelPickerDialog`). No more free
        text field: an arbitrary, freely typed channel name could never
        match the actual hardware and could assign the same physical
        channel more than once.

        The module deliberately gets NO column of its own: it is
        inherently inseparable from the channel (every physical channel
        has exactly one module) and is therefore tracked directly on the
        channel button as a plain-text addition, e.g. "cDAQ1Mod1/ai0
        (NI9215)".

        IMPORTANT: This display text is strictly separate from the
        internally used values - the actual values live in the button
        properties "hw_channel" and "module_type" (see
        `_get_hw_channel_text_from_widget`/`_get_module_value_from_widget`),
        NOT in the visible, composed text. Without a selection, the button
        only shows a placeholder, which is itself not a valid channel/
        module value.
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
        """Sets the ellipsis icon that marks a button as "opens a selection
        window" rather than a direct action (see
        `_create_hw_channel_widget`/`_create_signal_type_widget`). Also
        called again on a theme change (see `_retheme_action_button_icons`),
        since the icon color depends on the theme."""
        button.setIcon(QIcon(draw_ellipsis_icon(14)))
        button.setIconSize(QSize(14, 14))

    @staticmethod
    def _apply_parameter_button_icon(button: "_IconTextButton") -> None:
        """Sets the ellipsis icon for the parameter button (see
        `_create_parameter_widget`). Spacing to the text and vertical
        centering are already handled by `_IconTextButton`'s own layout -
        only the (theme-dependent) pixmap is set here, also again on a
        theme change (see `_retheme_action_button_icons`)."""
        button.setIconPixmap(draw_ellipsis_icon(14))

    def _update_hw_channel_button_text(self, button: "_PickerCell") -> None:
        """Assembles the visible button text from the properties.

        Kept separate from the properties themselves, so the display value
        and the internal value are never accidentally mixed up (see
        `_create_hw_channel_widget`). Only the part from the module
        onwards is displayed (e.g. "Mod2/ai0 (NI9234)" instead of
        "cDAQ9185-0217ED5EMod2/ai0 (NI9234)") - the chassis serial number
        prefix before it is irrelevant for distinguishing channels and
        only costs space (see `_short_hw_channel_text`). The full name
        remains fully available as a tooltip; only if even the shortened
        text doesn't fit the column is it additionally elided at the start
        (see `_apply_elided_hw_text`).
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
        """Shortens a hardware channel name to the module part onward ("from Mod").

        "cDAQ9185-0217ED5EMod2/ai0" -> "Mod2/ai0". If the name (e.g. for a
        device manually renamed in NI MAX) contains no "Mod<N>", the full
        name is used unchanged - better a long name than one made useless
        by incorrect shortening.
        """
        match = _MODULE_SUFFIX_PATTERN.search(hw_channel)
        short = hw_channel[match.start():] if match else hw_channel
        return f"{short} ({module_value})" if module_value else short

    def _apply_elided_hw_text(self, button: "_PickerCell") -> None:
        """Further shortens the button text (already reduced to "from Mod
        onward") if it still doesn't fit the current column width on its own.

        `ElideLeft` instead of `ElideMiddle`: module/channel/type are at
        the end of the text - that's the actually distinguishing
        information (which module, which channel) and should stay fully
        visible as long as possible, even if that means the start gets
        dropped.

        Uses `QTableWidget.columnWidth()` instead of `button.width()` as
        the reference: right after creation (before the button sits in the
        cell and Qt has processed the layout), `button.width()` would still
        be 0. Called again on column width changes (see the
        `sectionResized` connection in `__init__`).
        """
        display_text = str(button.property("display_text") or "")
        # Subtract space for the separate ellipsis button (22px), the gap
        # to it (10px), and the cell margins (see `_PickerCell`).
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
        """Opens the channel selection dialog for the clicking button's row.

        Determines the affected row via `sender()` (see
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
        """Builds the cell widget for the signal type column of a row.

        As with the hardware channel (see `_create_hw_channel_widget`), a
        `_PickerCell` that opens its own selection window instead of an
        inline combo box - consistent handling for both columns.
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
        button.setText(t(SIGNAL_TYPE_LABEL_KEYS[signal_type]))

    def _on_choose_signal_type_clicked(self) -> None:
        """Opens the signal type selection dialog for the clicking button's row.

        The allowed signal types are, as in
        `_apply_module_signal_constraint`, freshly derived here from the
        module of the hardware channel cell in the same row, instead of
        being tracked separately.
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
        self._update_parameter_state(row)

    def _create_parameter_widget(self, channel: Channel) -> QPushButton:
        """Builds the cell widget for the parameter column of a row: a
        single, cell-filling button with fixed text (instead of a label +
        separate selection button as with hardware channel/signal type,
        see `_PickerCell`) - opens `ChannelParameterDialog`, which shows
        only the relevant fields depending on the row's signal type. The
        current values are deliberately NOT summarized in the button text
        (a simple, consistent button instead of a value preview), but only
        tracked as properties.
        """
        sensitivity = channel.sensitivity_mv_per_unit if channel.sensitivity_mv_per_unit else 0.0
        gage_factor = channel.strain_gage_factor if channel.strain_gage_factor else 0.0
        button = _IconTextButton()
        button.setText(t("choose_parameters_button"))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setProperty("scale", channel.scale)
        button.setProperty("offset", channel.offset)
        button.setProperty("sensitivity", sensitivity)
        button.setProperty("thermocouple_type", channel.thermocouple_type or "K")
        button.setProperty("adc_timing_mode", channel.adc_timing_mode or "HIGH_RESOLUTION")
        button.setProperty("gage_factor", gage_factor)
        button.setProperty("strain_bridge_type", channel.strain_bridge_type or "QUARTER_BRIDGE_I")
        button.setProperty("lead_wire_resistance_ohm", channel.lead_wire_resistance_ohm or 0.0)
        button.setProperty("cal_point1_measured", channel.cal_point1_measured)
        button.setProperty("cal_point1_reference", channel.cal_point1_reference)
        button.setProperty("cal_point2_measured", channel.cal_point2_measured)
        button.setProperty("cal_point2_reference", channel.cal_point2_reference)
        button.clicked.connect(self._on_choose_parameters_clicked)
        self._apply_parameter_button_icon(button)
        return button

    @staticmethod
    def _property_float_or_none(widget: QWidget, name: str) -> float | None:
        """Reads an optional float property (e.g. a calibration point) that
        is either `None` or a number - `widget.property()` returns `None`
        again for a property set to `None`, and the matching Python type
        for a number (see `_create_parameter_widget`)."""
        value = widget.property(name)
        return None if value is None else float(value)

    def _on_choose_parameters_clicked(self) -> None:
        """Opens `ChannelParameterDialog` for the clicking button's row."""
        button = self.sender()
        row = self._find_row_for_widget(_COL_PARAMETERS, button)
        if row is None:
            return

        signal_widget = self._table.cellWidget(row, _COL_SIGNAL)
        try:
            signal_type = SignalType(str(signal_widget.property("signal_type") or ""))
        except ValueError:
            return

        hw_widget = self._table.cellWidget(row, _COL_HW_CHANNEL)
        module_type = ModuleType(self._get_module_value_from_widget(hw_widget))

        dialog = ChannelParameterDialog(
            signal_type,
            module_type,
            scale=float(button.property("scale") or 1.0),
            offset=float(button.property("offset") or 0.0),
            sensitivity=float(button.property("sensitivity") or 0.0),
            thermocouple_type=str(button.property("thermocouple_type") or "K"),
            adc_timing_mode=str(button.property("adc_timing_mode") or "HIGH_RESOLUTION"),
            strain_gage_factor=float(button.property("gage_factor") or 0.0),
            strain_bridge_type=str(button.property("strain_bridge_type") or "QUARTER_BRIDGE_I"),
            lead_wire_resistance_ohm=float(button.property("lead_wire_resistance_ohm") or 0.0),
            cal_point1_measured=self._property_float_or_none(button, "cal_point1_measured"),
            cal_point1_reference=self._property_float_or_none(button, "cal_point1_reference"),
            cal_point2_measured=self._property_float_or_none(button, "cal_point2_measured"),
            cal_point2_reference=self._property_float_or_none(button, "cal_point2_reference"),
            sensor_database=self._sensor_database,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        button.setProperty("scale", dialog.scale())
        button.setProperty("offset", dialog.offset())
        button.setProperty("sensitivity", dialog.sensitivity())
        button.setProperty("thermocouple_type", dialog.thermocouple_type())
        button.setProperty("gage_factor", dialog.strain_gage_factor())
        button.setProperty("strain_bridge_type", dialog.strain_bridge_type())
        button.setProperty("lead_wire_resistance_ohm", dialog.lead_wire_resistance_ohm())
        cal_point1_measured, cal_point1_reference = dialog.cal_point1()
        cal_point2_measured, cal_point2_reference = dialog.cal_point2()
        button.setProperty("cal_point1_measured", cal_point1_measured)
        button.setProperty("cal_point1_reference", cal_point1_reference)
        button.setProperty("cal_point2_measured", cal_point2_measured)
        button.setProperty("cal_point2_reference", cal_point2_reference)

        new_adc_timing_mode = dialog.adc_timing_mode()
        button.setProperty("adc_timing_mode", new_adc_timing_mode)
        if module_type == ModuleType.NI9213:
            self._apply_adc_timing_mode_to_module(hw_widget, new_adc_timing_mode, exclude_row=row)

    def _apply_adc_timing_mode_to_module(
        self, hw_widget: QWidget, value: str, exclude_row: int
    ) -> None:
        """Propagates a newly chosen ADC timing mode to all other rows of
        the same physical module.

        nidaqmx requires the same ADC timing mode for all channels of the
        same device (see `hardware/ni9213.py`) - without this propagation,
        the user could set conflicting values per row, which would only
        surface as a hardware error when the measurement starts.
        """
        device_name = device_name_from_hw_channel(
            self._get_hw_channel_text_from_widget(hw_widget)
        )
        if not device_name:
            return
        for row in range(self._table.rowCount()):
            if row == exclude_row:
                continue
            other_hw_widget = self._table.cellWidget(row, _COL_HW_CHANNEL)
            other_device_name = device_name_from_hw_channel(
                self._get_hw_channel_text_from_widget(other_hw_widget)
            )
            if other_device_name != device_name:
                continue
            other_param_widget = self._table.cellWidget(row, _COL_PARAMETERS)
            if other_param_widget is not None:
                other_param_widget.setProperty("adc_timing_mode", value)

    def _find_row_for_widget(self, column: int, widget: QWidget) -> int | None:
        """Finds the row of a cell widget in `column` (see `sender()`
        callers such as `_on_choose_hw_channel_clicked`/
        `_on_choose_signal_type_clicked`) - row indices can shift due to
        `_on_remove_clicked`, so no fixed number is captured when
        connecting."""
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

        # Hardware channel button (always, see `_create_hw_channel_widget`) -
        # tracks the module directly alongside it, no column of its own for that.
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

        self._table.setCellWidget(
            row, _COL_PARAMETERS, self._create_parameter_widget(channel)
        )
        # If the hardware channel is assigned to a known device, derive the
        # module from it (see `_apply_device_constraint`) and update the
        # signal type/parameter restriction accordingly.
        self._apply_device_constraint(row)
        self._update_row_numbers()

        # Take over the live view display settings of the given channel
        # (e.g. when loading a saved configuration) - see
        # `_display_settings`/`_read_row`. The key is (hardware_channel,
        # display_name), NOT `hardware_channel` alone: several not-yet-
        # assigned channels (empty `hardware_channel`, e.g. before
        # connecting the hardware) would otherwise all share the same key
        # and overwrite each other - see
        # `gui/live_view.py::_channel_display_key` for the same issue
        # there.
        self._display_settings[(channel.hardware_channel, channel.display_name)] = {
            "plot_color": channel.plot_color,
            "plot_background": channel.plot_background,
            "plot_grid_color": channel.plot_grid_color,
            "plot_y_min": channel.plot_y_min,
            "plot_y_max": channel.plot_y_max,
            "plot_autoscale": channel.plot_autoscale,
            "plot_time_window_seconds": channel.plot_time_window_seconds,
            "plot_show_graph": channel.plot_show_graph,
            "plot_show_value": channel.plot_show_value,
            "plot_value_integer_digits": channel.plot_value_integer_digits,
            "plot_value_decimal_digits": channel.plot_value_decimal_digits,
            "plot_visible": channel.plot_visible,
            "plot_popout": channel.plot_popout,
            "plot_popout_x": channel.plot_popout_x,
            "plot_popout_y": channel.plot_popout_y,
            "plot_popout_width": channel.plot_popout_width,
            "plot_popout_height": channel.plot_popout_height,
        }

    def _read_row(self, row: int) -> Channel:
        enabled_widget = self._table.cellWidget(row, _COL_ENABLED)
        enabled = enabled_widget.findChild(QCheckBox).isChecked()

        hardware_widget = self._table.cellWidget(row, _COL_HW_CHANNEL)
        hardware_channel = self._get_hw_channel_text_from_widget(hardware_widget).strip()
        display_name = self._table.cellWidget(row, _COL_NAME).text().strip()
        unit = self._table.cellWidget(row, _COL_UNIT).text().strip()
        module_type = ModuleType(self._get_module_value_from_widget(hardware_widget))
        signal_type = SignalType(self._table.cellWidget(row, _COL_SIGNAL).property("signal_type"))
        param_widget = self._table.cellWidget(row, _COL_PARAMETERS)
        scale = float(param_widget.property("scale") or 1.0)
        offset = float(param_widget.property("offset") or 0.0)
        sensitivity = float(param_widget.property("sensitivity") or 0.0)
        thermocouple_type = str(param_widget.property("thermocouple_type") or "K")
        adc_timing_mode = str(param_widget.property("adc_timing_mode") or "HIGH_RESOLUTION")
        gage_factor = float(param_widget.property("gage_factor") or 0.0)
        strain_bridge_type = str(param_widget.property("strain_bridge_type") or "QUARTER_BRIDGE_I")
        lead_wire_resistance_ohm = float(param_widget.property("lead_wire_resistance_ohm") or 0.0)
        cal_point1_measured = self._property_float_or_none(param_widget, "cal_point1_measured")
        cal_point1_reference = self._property_float_or_none(param_widget, "cal_point1_reference")
        cal_point2_measured = self._property_float_or_none(param_widget, "cal_point2_measured")
        cal_point2_reference = self._property_float_or_none(param_widget, "cal_point2_reference")
        display_settings = self._display_settings.get((hardware_channel, display_name), {})

        return Channel(
            hardware_channel=hardware_channel,
            display_name=display_name or hardware_channel,
            unit=unit,
            scale=scale,
            offset=offset,
            signal_type=signal_type,
            module_type=module_type,
            enabled=enabled,
            # min_range/max_range are deliberately NOT editable here (no
            # table field for them) - explicitly None instead of letting
            # the Channel dataclass default (-10.0/10.0 V) take over, so
            # the hardware layer (hardware/ni9234.py etc.) applies its own
            # correct module measurement range as a fallback (e.g. NI9234
            # fixed at ±5V) instead of a blanket ±10V, which the NI9234
            # hardware hard-rejects when the measurement starts.
            min_range=None,
            max_range=None,
            sensitivity_mv_per_unit=sensitivity if sensitivity > 0 else None,
            thermocouple_type=thermocouple_type or "K",
            adc_timing_mode=adc_timing_mode or "HIGH_RESOLUTION",
            strain_gage_factor=gage_factor if gage_factor > 0 else None,
            strain_bridge_type=strain_bridge_type or "QUARTER_BRIDGE_I",
            lead_wire_resistance_ohm=lead_wire_resistance_ohm,
            cal_point1_measured=cal_point1_measured,
            cal_point1_reference=cal_point1_reference,
            cal_point2_measured=cal_point2_measured,
            cal_point2_reference=cal_point2_reference,
            plot_color=display_settings.get("plot_color"),
            plot_background=display_settings.get("plot_background"),
            plot_grid_color=display_settings.get("plot_grid_color"),
            plot_y_min=display_settings.get("plot_y_min"),
            plot_y_max=display_settings.get("plot_y_max"),
            plot_autoscale=display_settings.get("plot_autoscale", True),
            plot_time_window_seconds=max(
                0.1, float(display_settings.get("plot_time_window_seconds", 5.0))
            ),
            plot_show_graph=display_settings.get("plot_show_graph", True),
            plot_show_value=display_settings.get("plot_show_value", False),
            plot_value_integer_digits=max(
                1, int(display_settings.get("plot_value_integer_digits", 3))
            ),
            plot_value_decimal_digits=max(
                0, int(display_settings.get("plot_value_decimal_digits", 3))
            ),
            plot_visible=display_settings.get("plot_visible", True),
            plot_popout=display_settings.get("plot_popout", False),
            plot_popout_x=display_settings.get("plot_popout_x"),
            plot_popout_y=display_settings.get("plot_popout_y"),
            plot_popout_width=display_settings.get("plot_popout_width"),
            plot_popout_height=display_settings.get("plot_popout_height"),
        )

    def apply_display_settings(self, settings: dict[tuple[str, str], dict]) -> None:
        """Takes over values set by the "channel display" dialog (see
        `gui/live_view.py::ChannelDisplayDialog`) so they are preserved on
        the next readout of the table (`_read_row`, e.g. when saving the
        configuration). `settings` is keyed by (`hardware_channel`,
        `display_name`) (see `gui/live_view.py::_channel_display_key`),
        NOT `hardware_channel` alone - see `_add_row` for the expected
        dict format per channel.
        """
        for key, values in settings.items():
            self._display_settings[key] = values

    def update_display_settings(self, key: tuple[str, str], values: dict) -> None:
        """Updates ONLY the given fields for ONE channel (merge instead of
        replace, unlike `apply_display_settings`) - e.g. for the popout
        window geometry (see `gui/main_window.py`), which is updated
        independently of the channel display dialog and must therefore not
        overwrite its already stored values. Creates a new entry if
        needed, in case none exists yet."""
        existing = self._display_settings.get(key, {})
        self._display_settings[key] = {**existing, **values}

    def _retheme_action_button_icons(self) -> None:
        self._add_button.setIcon(QIcon(draw_plus_icon(16)))
        self._remove_button.setIcon(QIcon(draw_minus_icon(16)))
        # The ellipsis icon of the hardware channel/signal type/parameter
        # buttons is colored theme-dependently (see
        # `_apply_picker_button_icon`/`_apply_parameter_button_icon`) -
        # refresh it on existing rows after a theme change.
        for row in range(self._table.rowCount()):
            for column in (_COL_HW_CHANNEL, _COL_SIGNAL):
                widget = self._table.cellWidget(row, column)
                if isinstance(widget, _PickerCell):
                    self._apply_picker_button_icon(widget)
            param_widget = self._table.cellWidget(row, _COL_PARAMETERS)
            if isinstance(param_widget, QPushButton):
                self._apply_parameter_button_icon(param_widget)

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
        """Reads the channel value from the hardware channel cell widget
        (always a `_PickerCell`, see `_create_hw_channel_widget`).

        The actual value lives in the "hw_channel" property, NOT in the
        visible button text - that shows a placeholder when nothing is
        selected, which is itself not a valid channel name.
        """
        if widget is None:
            return ""
        return str(widget.property("hw_channel") or "")

    @staticmethod
    def _get_module_value_from_widget(widget) -> str:
        """Reads the internally tracked module value from the hardware
        channel button (property "module_type", see
        `_create_hw_channel_widget`)."""
        if widget is None:
            return ""
        return str(widget.property("module_type") or "")

    @staticmethod
    def _center(widget: QWidget) -> QWidget:
        """Centers a widget in a table cell (e.g. a checkbox)."""
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

