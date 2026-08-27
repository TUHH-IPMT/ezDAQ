"""
gui/setup_view.py

Setup view: device discovery, channel configuration, and measurement parameters.

Functions (see spec):
    * detect connected NI devices
    * display modules
    * select/enable/disable channels, name them, set unit/
      scaling/offset
    * set sample rate
    * select storage format

This view communicates exclusively via signals with
`gui/main_window.py` - it knows neither `MeasurementController` nor
hardware details directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PyQt6.QtCore import QLocale, QSize, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QIcon
from PyQt6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QCheckBox,
    QScrollArea,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config.configuration_manager import ConfigurationManager
from config.sensor_database import SensorDatabaseManager
from data.models import (
    Channel,
    DeviceInfo,
    MeasurementConfig,
    ModuleType,
    RecordingStopUnit,
    StorageFormat,
    is_valid_grid_sample_rate,
    is_valid_ni9234_sample_rate,
    max_ni9213_sample_rate_hz,
    next_grid_sample_rate_at_or_above,
    next_ni9234_sample_rate_at_or_above,
    ni9213_device_groups,
    resolve_rate_groups,
)
from gui.i18n import connect_language_changed, t
from gui.theme import (
    PLAY_ICON_COLOR,
    RECORD_ICON_COLOR,
    action_button_style,
    connect_theme_changed,
    disabled_text_color,
    draw_play_icon,
    draw_record_icon,
    draw_stop_icon,
    draw_trigger_icon,
    fix_toggle_button_width,
    repolish,
    trigger_arm_button_style,
)
from gui.widgets.channel_table import ChannelTableWidget
from gui.widgets.spinbox import (
    GroupedDoubleSpinBox,
    NoWheelSpinBox,
    PrecisionDoubleSpinBox,
)

logger = logging.getLogger(__name__)

# Translated display labels for the storage format combo box. The
# actual value (StorageFormat.value, e.g. "parquet") stays stable
# independent of the UI language (persistence) and is stored as
# `userData` per entry, see `_populate_storage_format_combo`.
_STORAGE_FORMAT_LABEL_KEYS: dict[StorageFormat, str] = {
    StorageFormat.PARQUET: "storage_format_parquet",
    StorageFormat.CSV: "storage_format_csv",
}

# Translated display labels for the recording-limit unit combo box
# (see `_populate_recording_stop_unit_combo`) - analogous to
# `_STORAGE_FORMAT_LABEL_KEYS`.
_RECORDING_STOP_UNIT_LABEL_KEYS: dict[RecordingStopUnit, str] = {
    RecordingStopUnit.SAMPLES: "recording_stop_unit_samples",
    RecordingStopUnit.SECONDS: "recording_stop_unit_seconds",
    RecordingStopUnit.MINUTES: "recording_stop_unit_minutes",
    RecordingStopUnit.HOURS: "recording_stop_unit_hours",
}

# Translated display labels per ADC timing mode for the error message on
# an excessive sample rate (see `build_current_config`) - a deliberately
# small, separate copy of `gui/widgets/channel_table.py::_ADC_TIMING_MODE_LABEL_KEYS`
# rather than importing that module's private (`_`-prefixed) dict.
_ADC_TIMING_MODE_LABEL_KEYS: dict[str, str] = {
    "HIGH_RESOLUTION": "adc_timing_mode_high_resolution",
    "HIGH_SPEED": "adc_timing_mode_high_speed",
}


@dataclass
class NamingScheme:
    """Controls how `gui/main_window.py` builds the actual file/measurement
    name from the entered measurement name.

    Attributes:
        use_number_suffix: Whether a number suffix (e.g. "_001") is
            appended to automatically resolve name conflicts.
        number_suffix_digits: Digit count of the number suffix.
        include_date: Whether the current date (YYYYMMDD) is appended.
        include_time: Whether the current time (HHMMSS) is appended.
    """

    use_number_suffix: bool
    number_suffix_digits: int
    include_date: bool
    include_time: bool


class SetupView(QWidget):
    """View for configuring hardware, channels, and measurement parameters.

    Signals:
        discover_hardware_requested: User wants connected devices to be
            detected. `gui/main_window.py` then calls
            `controller.discover_hardware()` and delivers the result
            back via `set_discovered_devices()`.
        open_ni_max_requested: User wants to open NI-MAX (Measurement &
            Automation Explorer) as a separate program - e.g. to rename
            a device without leaving this application.
        start_measurement_requested: User wants to start the measurement
            with the given `MeasurementConfig` (either the play OR
            record button - `MeasurementConfig.save_to_disk` is already
            set accordingly, see `_on_play_clicked`/
            `_on_record_clicked`).
        stop_requested: User wants to stop the running measurement (only
            clickable while one is actually running, see
            `set_start_enabled`).
    """

    discover_hardware_requested = pyqtSignal()
    open_ni_max_requested = pyqtSignal()
    start_measurement_requested = pyqtSignal(object)  # MeasurementConfig
    stop_requested = pyqtSignal()
    storage_path_requested = pyqtSignal()
    # User clicked the arm button (see `_trigger_arm_button`) -
    # bool = new state (True = arm, False = disarm).
    trigger_arm_toggled = pyqtSignal(bool)

    def __init__(
        self,
        configuration_manager: ConfigurationManager,
        sensor_database: SensorDatabaseManager | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._configuration_manager = configuration_manager
        # None means: there has been no successful device discovery since
        # the last reset. An empty list, on the other hand, is a valid
        # result of a successful search that found no usable modules.
        self._discovered_devices: list[DeviceInfo] | None = None
        self._storage_path_is_set = False
        self._status_reason_key = ""
        self._discovery_in_progress = False
        # Problem devices (unsupported module / not connected) already
        # reported by dialog, as {(device_name, kind)}. Since switching
        # to this view triggers a device search on its own (see
        # `gui/main_window.py::_on_nav_changed`), the dialogs would
        # otherwise pop up again on EVERY switch for a problem that is
        # already visible - grayed out - in the device list. Reporting
        # only what is NEW keeps the notice for a device that has just
        # appeared/dropped out, without turning it into a recurring
        # modal interruption (see `set_discovered_devices`).
        self._reported_problem_devices: set[tuple[str, str]] = set()

        # The entire view sits inside a QScrollArea: with many sections
        # (devices, channels, measurement settings, storage) the window
        # height often isn't enough for all sections at once - without a
        # scroll area, Qt would instead squeeze ALL sections evenly
        # (in particular the channel table down to a single, barely
        # usable row). With a scroll area, all sections keep their
        # preferred/minimum size, and excess content is scrolled instead
        # of squeezed.
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        outer_layout.addWidget(scroll_area, stretch=1)

        content = QWidget()
        scroll_area.setWidget(content)
        layout = QVBoxLayout(content)

        # --- Device discovery ---
        self._device_header = QLabel(t("connected_devices"))
        layout.addWidget(self._device_header)
        self._device_group = QGroupBox()
        device_layout = QVBoxLayout(self._device_group)
        discover_row = QHBoxLayout()
        self._discover_button = QPushButton(t("search_devices"))
        self._discover_button.clicked.connect(self.discover_hardware_requested.emit)
        # Quick access to NI-MAX (Measurement & Automation Explorer) -
        # e.g. to rename/configure a device without leaving this
        # application (see `open_ni_max_requested`).
        self._open_ni_max_button = QPushButton(t("open_ni_max_button"))
        self._open_ni_max_button.clicked.connect(self.open_ni_max_requested.emit)
        discover_row.addWidget(self._discover_button)
        discover_row.addWidget(self._open_ni_max_button)
        # Tree view device -> channels (same grouping as in the channel
        # assignment dialog, see
        # `gui/widgets/channel_table.py::HardwareChannelPickerDialog`).
        self._device_list = QTreeWidget()
        self._device_list.setHeaderHidden(True)
        # Minimum height for a few visible rows, without an empty list
        # (before the first device discovery) taking up unnecessary space.
        self._device_list.setMinimumHeight(120)
        device_layout.addLayout(discover_row)
        device_layout.addWidget(self._device_list)
        layout.addWidget(self._device_group)

        # --- Channel configuration ---
        self._channel_header = QLabel(t("channel_configuration"))
        layout.addWidget(self._channel_header)
        self._channel_group = QGroupBox()
        channel_layout = QVBoxLayout(self._channel_group)
        self._channel_table = ChannelTableWidget(sensor_database)
        channel_layout.addWidget(self._channel_table)
        layout.addWidget(self._channel_group, stretch=1)

        # --- Measurement settings ---
        self._measurement_header = QLabel(t("measurement_settings"))
        layout.addWidget(self._measurement_header)
        self._measurement_group = QGroupBox()
        self._measurement_layout = QFormLayout(self._measurement_group)

        self._sample_rate_spin = GroupedDoubleSpinBox()
        self._sample_rate_spin.setRange(1.0, 100_000.0)
        self._sample_rate_spin.setDecimals(1)
        self._sample_rate_spin.setSingleStep(100.0)
        # Thousands separator in the display (e.g. "100.000,0"), independent
        # of the operating system's locale.
        self._sample_rate_spin.setLocale(QLocale(QLocale.Language.German, QLocale.Country.Germany))
        self._sample_rate_spin.setGroupSeparatorShown(True)
        self._sample_rate_spin.setValue(
            configuration_manager.settings.default_sample_rate_hz
        )
        self._measurement_layout.addRow(f"{t('sample_rate_hz')}:", self._sample_rate_spin)

        # Non-blocking preview of the sample rate actually used per rate
        # group (see `_update_resolved_rate_preview`) - stays empty/
        # invisible in the normal case (exactly one group), only appears
        # once e.g. an NI9210 forces its own group. Unlike DIAdem/NI-MAX,
        # the deviating actual rate is thus reported back visibly here
        # instead of being silently clamped.
        self._resolved_rate_preview_label = QLabel()
        self._resolved_rate_preview_label.setWordWrap(True)
        self._measurement_layout.addRow("", self._resolved_rate_preview_label)
        self._sample_rate_spin.valueChanged.connect(self._update_resolved_rate_preview)

        measurement_row = QHBoxLayout()
        measurement_row.addWidget(self._measurement_group, stretch=1)
        measurement_row.addStretch(1)
        layout.addLayout(measurement_row)

        # Internal performance parameters are set automatically so the
        # user isn't burdened with technical details here. Goal: read
        # blocks derived purely from the BLOCK DURATION (not from a fixed
        # sample count), so the size scales dynamically with the sample
        # rate - see `_calculate_samples_per_read`. Do NOT shrink this
        # below 25ms: a test with 10ms led to "The application is not able
        # to keep up with the hardware acquisition" at high sample rates
        # (buffer overrun/data loss) - the pure Python/ctypes call overhead
        # per `device.read()` then dominates over the actual data transfer,
        # and the DAQ thread itself can no longer keep up. A fixed minimum
        # sample count (earlier approach) would miss the same target block
        # duration at low sample rates and produce long, bursty blocks
        # (e.g. 50 samples at 14 S/s = 3.6s instead of the intended 25ms) -
        # hence deliberately only an UPPER bound, no lower bound, on the
        # sample count.
        self._target_read_block_ms = 25.0
        self._max_samples_per_read = 2000
        self._default_ring_buffer_seconds = 30

        # --- Storage settings ---
        self._storage_header = QLabel(t("storage_settings"))
        layout.addWidget(self._storage_header)
        self._storage_group = QGroupBox()
        self._storage_layout = QFormLayout(self._storage_group)

        self._name_edit = QLineEdit(
            configuration_manager.settings.last_measurement_name or "Messung"
        )
        self._storage_layout.addRow(f"{t('measurement_name')}:", self._name_edit)

        naming_row = QHBoxLayout()
        settings = configuration_manager.settings
        self._naming_number_checkbox = QCheckBox(t("naming_number_suffix"))
        self._naming_number_checkbox.setChecked(settings.name_use_number_suffix)
        self._naming_digits_label = QLabel(f"{t('naming_digits')}:")
        self._naming_digits_spin = NoWheelSpinBox()
        self._naming_digits_spin.setRange(1, 6)
        self._naming_digits_spin.setValue(settings.name_number_suffix_digits)
        self._naming_digits_spin.setEnabled(settings.name_use_number_suffix)
        self._naming_date_checkbox = QCheckBox(t("naming_include_date"))
        self._naming_date_checkbox.setChecked(settings.name_include_date)
        self._naming_time_checkbox = QCheckBox(t("naming_include_time"))
        self._naming_time_checkbox.setChecked(settings.name_include_time)

        naming_row.addWidget(self._naming_number_checkbox)
        naming_row.addWidget(self._naming_digits_label)
        naming_row.addWidget(self._naming_digits_spin)
        naming_row.addWidget(self._naming_date_checkbox)
        naming_row.addWidget(self._naming_time_checkbox)
        naming_row.addStretch(1)
        self._naming_row_label = QLabel(f"{t('naming_scheme')}:")
        self._storage_layout.addRow(self._naming_row_label, naming_row)

        self._naming_number_checkbox.toggled.connect(self._on_naming_scheme_changed)
        self._naming_digits_spin.valueChanged.connect(self._on_naming_scheme_changed)
        self._naming_date_checkbox.toggled.connect(self._on_naming_scheme_changed)
        self._naming_time_checkbox.toggled.connect(self._on_naming_scheme_changed)

        self._storage_format_combo = QComboBox()
        self._populate_storage_format_combo(configuration_manager.settings.default_storage_format)
        self._storage_layout.addRow(f"{t('storage_format')}:", self._storage_format_combo)

        # Recording limit ("measurement cycle"): unlimited by default
        # (current behavior - run until manually stopped or the disk is
        # full). Unchecking the box allows entering a limit (measurement
        # values or time) at which the measurement stops automatically
        # (see `gui/live_view.py::_on_timer_tick`).
        self._recording_unlimited_checkbox = QCheckBox(t("recording_unlimited"))
        self._recording_unlimited_checkbox.setChecked(
            configuration_manager.settings.last_recording_unlimited
        )
        self._recording_unlimited_checkbox.toggled.connect(self._on_recording_unlimited_toggled)
        self._storage_layout.addRow("", self._recording_unlimited_checkbox)

        recording_limit_row = QHBoxLayout()
        self._recording_stop_spin = NoWheelSpinBox()
        self._recording_stop_spin.setRange(1, 1_000_000_000)
        self._recording_stop_spin.setValue(
            max(1, int(configuration_manager.settings.last_recording_stop_value))
        )
        self._recording_stop_unit_combo = QComboBox()
        self._populate_recording_stop_unit_combo(
            configuration_manager.settings.last_recording_stop_unit
        )
        recording_limit_row.addWidget(self._recording_stop_spin)
        recording_limit_row.addWidget(self._recording_stop_unit_combo)
        recording_limit_row.addStretch(1)
        self._recording_limit_row_label = QLabel(f"{t('recording_limit_label')}:")
        self._storage_layout.addRow(self._recording_limit_row_label, recording_limit_row)
        self._on_recording_unlimited_toggled(self._recording_unlimited_checkbox.isChecked())

        self._storage_path_label = QLabel(t("no_storage_location"))
        self._storage_button = QPushButton(t("choose_storage_location"))
        self._storage_button.clicked.connect(self.storage_path_requested.emit)
        self._storage_layout.addRow(f"{t('storage_location')}:", self._storage_path_label)
        self._storage_layout.addRow("", self._storage_button)

        storage_row = QHBoxLayout()
        storage_row.addWidget(self._storage_group, stretch=1)
        storage_row.addStretch(1)
        layout.addLayout(storage_row)

        # --- Start ---
        # Three buttons with icon AND text instead of the previous single
        # "Start" button + "live view only" checkbox (see `_storage_layout`
        # above): Play (green icon) starts ONLY the live view without
        # storage, Record (red circle icon) starts WITH storage - which
        # button is clicked directly determines `MeasurementConfig.
        # save_to_disk` (see `_on_play_clicked`/`_on_record_clicked`/
        # `build_current_config`). Stop stays disabled unless one of the
        # two variants is actually running (see `set_start_enabled`).
        # `ACTION_BUTTON_STYLE` deliberately sets NO `background-color` in
        # the normal state - the buttons normally follow the QPalette/
        # current theme, only the play/record icon color is fixed
        # (see `_retheme_start_button_icons`); hover/press get a subtle
        # palette-based effect.
        start_row = QHBoxLayout()
        self._status_label = QLabel("")

        self._play_button = QPushButton()
        self._play_button.setIconSize(QSize(24, 24))
        self._play_button.setStyleSheet(action_button_style())
        self._play_button.clicked.connect(self._on_play_clicked)
        start_row.addWidget(self._play_button)

        self._record_button = QPushButton()
        self._record_button.setIconSize(QSize(24, 24))
        self._record_button.setStyleSheet(action_button_style())
        self._record_button.clicked.connect(self._on_record_clicked)
        start_row.addWidget(self._record_button)

        self._stop_button = QPushButton()
        self._stop_button.setIconSize(QSize(24, 24))
        self._stop_button.setStyleSheet(action_button_style())
        self._stop_button.setEnabled(False)
        self._stop_button.clicked.connect(self.stop_requested.emit)
        start_row.addWidget(self._stop_button)

        self._retheme_start_button_icons()
        self._update_start_button_labels()

        # Arm button: enables the unattended trigger cycle (arm -> wait ->
        # record -> stop -> automatically arm again, see
        # `TriggerConfig.auto_rearm` and
        # `gui/main_window.py::_on_trigger_arm_toggled`) - stays pressed
        # until clicked again ("disarm"), regardless of how often the
        # cycle runs automatically in between. Only visible when a
        # trigger is actually configured (see
        # `set_trigger_arm_available`).
        self._trigger_arm_button = QPushButton()
        self._trigger_arm_button.setCheckable(True)
        self._trigger_arm_button.setIconSize(QSize(24, 24))
        self._retheme_trigger_arm_button_icon()
        self._trigger_arm_button.setStyleSheet(trigger_arm_button_style())
        # ONLY AFTER setting the icon/stylesheet: `_set_trigger_arm_button_text()`
        # fixes the button width via `fix_toggle_button_width()` based on
        # `sizeHint()`, which needs both the icon AND stylesheet padding to
        # measure correctly.
        self._set_trigger_arm_button_text()
        self._trigger_arm_button.setVisible(False)
        self._trigger_arm_button.toggled.connect(self._on_trigger_arm_button_toggled)
        start_row.addWidget(self._trigger_arm_button)

        start_row.addWidget(self._status_label, stretch=1)
        # DELIBERATELY not part of `layout` (which scrolls with the rest
        # of the content) - directly in `outer_layout`, so the buttons
        # ALWAYS stay visible AND at a fixed position, independent of the
        # scroll state/the number of channels/settings above them. As a
        # side effect, this reliably aligns the bottom edge with the
        # bottom edge of the navigation area on the left (see
        # `gui/main_window.py::_build_navigation_and_workspace` - both
        # are siblings in the same `root_layout` with a shared margin) -
        # matching `LiveView`, whose button row sits at the very top
        # (instead of in a scrolling area) for the same reason, aligning
        # it with the top edge of the navigation area.
        # Side/top padding matches `content`'s own default margin, but
        # NO bottom margin - that would otherwise defeat the alignment
        # guarantee again.
        start_row.setContentsMargins(9, 8, 9, 0)
        outer_layout.addLayout(start_row)

        self._apply_section_header_emphasis()

        # Automatically suggest the most recently used channel configuration.
        last_channels = configuration_manager.load_channel_configuration()
        if last_channels:
            self._channel_table.set_channels(last_channels)
        self._update_resolved_rate_preview()

        connect_language_changed(self.retranslate_ui)
        connect_theme_changed(self._retheme_start_button_icons)
        connect_theme_changed(self._retheme_trigger_arm_button_icon)

    # ------------------------------------------------------------------ #
    # Public API (called from main_window.py)
    # ------------------------------------------------------------------ #

    def retranslate_ui(self) -> None:
        """Updates all static texts after a language change."""
        self._device_header.setText(t("connected_devices"))
        self._discover_button.setText(
            t("searching_devices") if self._discovery_in_progress else t("search_devices")
        )
        self._open_ni_max_button.setText(t("open_ni_max_button"))
        self._channel_header.setText(t("channel_configuration"))
        self._measurement_header.setText(t("measurement_settings"))
        self._storage_header.setText(t("storage_settings"))

        for layout_, key, widget in (
            (self._measurement_layout, "sample_rate_hz", self._sample_rate_spin),
            (self._storage_layout, "measurement_name", self._name_edit),
            (self._storage_layout, "storage_format", self._storage_format_combo),
            (self._storage_layout, "storage_location", self._storage_path_label),
        ):
            label = layout_.labelForField(widget)
            if label is not None:
                label.setText(f"{t(key)}:")

        self._storage_button.setText(t("choose_storage_location"))
        if not self._storage_path_is_set:
            self._storage_path_label.setText(t("no_storage_location"))
        self._update_start_button_labels()
        self._set_trigger_arm_button_text()
        self._status_label.setText(t(self._status_reason_key))
        self._populate_storage_format_combo(self._storage_format_combo.currentData())

        self._recording_unlimited_checkbox.setText(t("recording_unlimited"))
        self._recording_limit_row_label.setText(f"{t('recording_limit_label')}:")
        self._populate_recording_stop_unit_combo(self._recording_stop_unit_combo.currentData())

        self._naming_row_label.setText(f"{t('naming_scheme')}:")
        self._naming_number_checkbox.setText(t("naming_number_suffix"))
        self._naming_digits_label.setText(f"{t('naming_digits')}:")
        self._naming_date_checkbox.setText(t("naming_include_date"))
        self._naming_time_checkbox.setText(t("naming_include_time"))

        self._channel_table.retranslate_ui()
        self._update_resolved_rate_preview()

    def get_naming_scheme(self) -> NamingScheme:
        """Returns the naming scheme currently set in the UI.

        Used by `gui/main_window.py` at measurement start to build the
        actual file/measurement name from the measurement name.
        """
        return NamingScheme(
            use_number_suffix=self._naming_number_checkbox.isChecked(),
            number_suffix_digits=self._naming_digits_spin.value(),
            include_date=self._naming_date_checkbox.isChecked(),
            include_time=self._naming_time_checkbox.isChecked(),
        )

    def _populate_storage_format_combo(self, selected_value: str) -> None:
        """Populates the storage format combo box with translated labels.

        The technical value (e.g. "parquet") is stored as `userData` per
        entry and thus remains retrievable independent of the UI
        language (see `build_current_config`/
        `get_current_measurement_parameters`).
        """
        self._storage_format_combo.blockSignals(True)
        self._storage_format_combo.clear()
        for storage_format in StorageFormat:
            self._storage_format_combo.addItem(
                t(_STORAGE_FORMAT_LABEL_KEYS[storage_format]), storage_format.value
            )
        index = self._storage_format_combo.findData(selected_value)
        self._storage_format_combo.setCurrentIndex(index if index >= 0 else 0)
        self._storage_format_combo.blockSignals(False)

    def _populate_recording_stop_unit_combo(self, selected_value: str) -> None:
        """Populates the recording-limit unit combo box with translated
        labels - analogous to `_populate_storage_format_combo`."""
        self._recording_stop_unit_combo.blockSignals(True)
        self._recording_stop_unit_combo.clear()
        for unit in RecordingStopUnit:
            self._recording_stop_unit_combo.addItem(
                t(_RECORDING_STOP_UNIT_LABEL_KEYS[unit]), unit.value
            )
        index = self._recording_stop_unit_combo.findData(selected_value)
        self._recording_stop_unit_combo.setCurrentIndex(index if index >= 0 else 0)
        self._recording_stop_unit_combo.blockSignals(False)

    def set_discovery_in_progress(self, in_progress: bool) -> None:
        """Locks/unlocks the "search devices" button while a device
        discovery is running in the background (see
        `gui/main_window.py::_on_discover_hardware`).

        Runs in a `BackgroundWorker` (see `gui/workers.py`) so that
        `nidaqmx.system.System.local()` - noticeably slow with multiple
        chassis/modules or driver timeouts - no longer blocks the GUI
        thread. This method gives the user visible feedback in the
        meantime and prevents a duplicate request from being started.
        """
        self._discovery_in_progress = in_progress
        self._discover_button.setEnabled(not in_progress)
        self._discover_button.setText(
            t("searching_devices") if in_progress else t("search_devices")
        )

    def get_discovered_devices(self) -> list[DeviceInfo] | None:
        """Returns the most recently detected devices (see
        `set_discovered_devices`).

        Passed through by `gui/main_window.py` at measurement start to
        `MeasurementController.start_measurement()`, so that
        `discover_hardware()` does NOT need to be called again there -
        per `_on_discover_hardware`, noticeably slow with multiple
        chassis/modules, and would otherwise block the GUI thread again
        on EVERY measurement start, even though the result (channel
        assignment comes from the channel configuration itself anyway,
        see `core/measurement.py::create_devices`) is only needed here
        for cosmetic metadata (`DeviceInfo.product_type`).
        """
        return self._discovered_devices

    def set_discovered_devices(self, devices: list[DeviceInfo]) -> None:
        """Displays the result of a device discovery.

        Passes ALL detected devices (not just one selected in the list)
        on to the channel table - which hardware channels are
        selectable and which module belongs to which channel is
        dictated by the actually connected hardware (see
        `gui/widgets/channel_table.py::set_available_devices`).

        Additionally reports via dialog if any of the detected devices
        have an UNSUPPORTED module type (`DeviceInfo.module_type is
        None`, see `hardware/nidaq_device.py::_map_product_type`) or do
        not respond at all - re-checked on EVERY device refresh, so a
        newly connected module that is (still) unsupported, or a device
        that has just dropped out, doesn't go unnoticed. Only devices
        NOT yet reported produce a dialog (see
        `_reported_problem_devices`): switching to this view triggers a
        refresh on its own, and an unchanged problem must not pop up a
        modal dialog every time - it stays visible in the device list. Their channels
        are already not selectable in the channel table (see
        `gui/widgets/channel_table.py::HardwareChannelPickerDialog`) -
        the message here additionally makes visible WHY/WHICH ones.

        The filter below deliberately uses `d.has_any_channels` IN
        ADDITION to `d.num_channels > 0`, NOT just the latter:
        `num_channels` counts only analog input channels (the only
        channel type supported by this app) - a pure analog output
        module like the NI9263 would thus have `num_channels == 0`,
        just like an empty chassis controller entry with no channels at
        all. Without `has_any_channels` (see `DeviceInfo`), such a
        genuinely connected but unsupported module would be wrongly
        treated here as "no hardware" and neither displayed nor
        reported.

        Devices that are merely configured in the driver but do not
        respond (`DeviceInfo.is_connected is False`, e.g. a reserved
        network cDAQ chassis whose cable was pulled) are deliberately
        still LISTED - grayed out and reported via dialog - rather than
        dropped: they are visible in NI-MAX too, and silently omitting
        them would look like the device had never been configured.
        Their channels are not offered for assignment (see
        `gui/widgets/channel_table.py::set_available_devices`).
        """
        self._device_list.clear()
        devices_with_channels = [
            d for d in devices if d.num_channels > 0 or d.has_any_channels
        ]
        self._discovered_devices = devices_with_channels
        self._channel_table.set_available_devices(devices_with_channels)
        if not devices_with_channels:
            self._device_list.addTopLevelItem(QTreeWidgetItem([t("no_devices_found")]))
            # Nothing left that could be a problem device - clearing the
            # memo means a device that comes back and is STILL a problem
            # is reported again rather than silently suppressed.
            self._reported_problem_devices.clear()
            return
        unsupported_devices: list[DeviceInfo] = []
        disconnected_devices: list[DeviceInfo] = []
        for device in devices_with_channels:
            if not device.is_connected:
                disconnected_devices.append(device)
                module_info = f" [{t('device_not_connected')}]"
            elif device.module_type is None:
                unsupported_devices.append(device)
                module_info = f" [{t('device_module_unsupported')}]"
            else:
                module_info = f" [{device.module_type.value}]"
            device_item = QTreeWidgetItem(
                [
                    f"{device.device_name} - {device.product_type}{module_info} "
                    f"({t('device_channel_count', count=device.num_channels)})"
                ]
            )
            channels = device.physical_channels or [
                f"{device.device_name}/ai{i}" for i in range(device.num_channels)
            ]
            for channel in channels:
                device_item.addChild(QTreeWidgetItem([channel]))
            if device.module_type is None or not device.is_connected:
                # Make it visually recognizable as unavailable - merely
                # disabling via item flags isn't enough for this (Qt
                # doesn't reliably apply the disabled palette color to
                # individual QTreeWidgetItems automatically), see
                # `gui/theme.py::disabled_text_color`. Recursive so the
                # (purely informational) channel child entries also
                # appear gray.
                brush = QBrush(disabled_text_color())
                device_item.setForeground(0, brush)
                for i in range(device_item.childCount()):
                    device_item.child(i).setForeground(0, brush)
            self._device_list.addTopLevelItem(device_item)
        # Collapsed by default (only device names visible) - saves
        # significant space with multiple modules that each have many
        # channels. The user expands a device individually as needed.
        self._device_list.collapseAll()

        # Only devices NOT already reported produce a dialog - see
        # `_reported_problem_devices`. The memo is replaced by the
        # current problem set (not merely extended), so a device that
        # recovers and drops out again is reported afresh.
        newly_disconnected = [
            d
            for d in disconnected_devices
            if (d.device_name, "disconnected") not in self._reported_problem_devices
        ]
        newly_unsupported = [
            d
            for d in unsupported_devices
            if (d.device_name, "unsupported") not in self._reported_problem_devices
        ]
        self._reported_problem_devices = {
            (d.device_name, "disconnected") for d in disconnected_devices
        } | {(d.device_name, "unsupported") for d in unsupported_devices}

        # Reported BEFORE the unsupported-module warning: a device that
        # doesn't answer at all is the more fundamental problem, and its
        # module type comes from the same stale driver cache that made it
        # look available in the first place - so a "not supported" notice
        # about it would be actively misleading. Hence a disconnected
        # device is never also counted as unsupported above.
        if newly_disconnected:
            device_list = "\n".join(
                f"- {d.device_name} ({d.product_type})" for d in newly_disconnected
            )
            QMessageBox.warning(
                self,
                t("disconnected_devices_title"),
                t("disconnected_devices_body", devices=device_list),
            )

        if newly_unsupported:
            module_list = "\n".join(
                f"- {d.device_name} ({d.product_type})" for d in newly_unsupported
            )
            QMessageBox.warning(
                self,
                t("unsupported_modules_title"),
                t("unsupported_modules_body", modules=module_list),
            )

    def show_discovery_error(self, message: str) -> None:
        """Displays a failed device discovery attempt (e.g. NI-DAQmx
        driver not installed) directly in the device browser instead of
        only in the log - the user thus sees the cause exactly where they
        look next (see
        `gui/main_window.py::_on_discover_hardware_failed`, calls this
        instead of `set_discovered_devices`).
        """
        self._device_list.clear()
        self._discovered_devices = None
        # Discovery failed as a whole, so nothing is known about problem
        # devices any more - report afresh after the next success.
        self._reported_problem_devices.clear()
        self._channel_table.set_available_devices([])
        self._device_list.addTopLevelItem(
            QTreeWidgetItem([f"{t('device_discovery_failed')}: {message}"])
        )

    def set_start_enabled(self, enabled: bool, reason: str = "") -> None:
        """Enables/disables play/record (e.g. while a measurement is
        running) - the stop button ALWAYS follows the exact opposite
        state: clickable only while something is actually running (live
        view or recording), grayed out otherwise.

        `reason` is an i18n key (not finished text), so the reason
        survives a language change (see `retranslate_ui`).
        """
        self._play_button.setEnabled(enabled)
        self._record_button.setEnabled(enabled)
        self._stop_button.setEnabled(not enabled)
        # The arm button follows the same enabled state as play/record -
        # EXCEPT when it is itself currently active (running an automatic
        # cycle, see `trigger_arm_toggled`): then it must always stay
        # clickable, so "disarm" works at any time, even while the
        # measurement is running.
        if not self._trigger_arm_button.isChecked():
            self._trigger_arm_button.setEnabled(enabled)
        self._status_reason_key = reason
        self._status_label.setText(t(reason))

    def set_trigger_arm_available(self, available: bool) -> None:
        """Shows/hides the arm button (see `main_window.py`, called after
        every change to the trigger settings) - without a configured
        start or stop trigger there would be nothing to arm. If the
        button is hidden while it was still pressed, it is additionally
        reset cleanly (without re-triggering the `trigger_arm_toggled`
        signal)."""
        self._trigger_arm_button.setVisible(available)
        if not available and self._trigger_arm_button.isChecked():
            self.set_trigger_armed(False)

    def set_trigger_armed(self, armed: bool) -> None:
        """Sets the arm button state PROGRAMMATICALLY (e.g. when
        `main_window.py` disarms due to an error) - blocks `toggled`
        while doing so, so this isn't wrongly interpreted as another
        user click (`trigger_arm_toggled`)."""
        self._trigger_arm_button.blockSignals(True)
        self._trigger_arm_button.setChecked(armed)
        self._trigger_arm_button.blockSignals(False)
        self._set_trigger_arm_button_text()

    def is_trigger_armed(self) -> bool:
        return self._trigger_arm_button.isChecked()

    def set_storage_path(self, path: str | None) -> None:
        self._storage_path_is_set = bool(path)
        self._storage_path_label.setText(path or t("no_storage_location"))

    def show_error(self, message: str) -> None:
        """Displays an error message (e.g. invalid configuration)."""
        QMessageBox.warning(self, t("error"), message)

    def get_current_measurement_parameters(
        self,
    ) -> tuple[str, float, str, bool, float, str]:
        """Returns the measurement parameters currently set in the UI.

        Returns:
            (measurement_name, sample_rate_hz, storage_format,
            recording_unlimited, recording_stop_value, recording_stop_unit)
        """
        return (
            self._name_edit.text().strip() or "Messung",
            self._sample_rate_spin.value(),
            self._storage_format_combo.currentData(),
            self._recording_unlimited_checkbox.isChecked(),
            float(self._recording_stop_spin.value()),
            self._recording_stop_unit_combo.currentData(),
        )

    def get_configured_channels(self) -> list[Channel]:
        """Returns the channels currently configured in the channel table.

        Unlike `build_current_config()`: no validation, no error
        message, also works WITHOUT a running measurement. Used e.g. by
        `gui/main_window.py` for the channel display dialog, which
        should already be usable before the measurement starts (the live
        view otherwise only knows its channels once a measurement is
        actually running).
        """
        return self._channel_table.get_channels()

    def apply_channel_display_settings(self, settings: dict[str, dict]) -> None:
        """Applies values set from the "channel display" dialog (see
        `gui/live_view.py::ChannelDisplayDialog`) into the channel
        table, so they are preserved when the configuration is saved
        (see `ChannelTableWidget.apply_display_settings`)."""
        self._channel_table.apply_display_settings(settings)

    def update_channel_display_setting(self, key: tuple[str, str], values: dict) -> None:
        """Updates ONLY the given fields for a channel (see
        `ChannelTableWidget.update_display_settings`) - for
        `gui/main_window.py`, to apply the current popout window
        position when the app is closed/explicitly saved, without
        overwriting the values set by the channel display dialog."""
        self._channel_table.update_display_settings(key, values)

    def build_current_config(self, live_only: bool = False) -> MeasurementConfig | None:
        """Builds a MeasurementConfig from the current UI inputs.

        Used by `_on_play_clicked`/`_on_record_clicked` (each with an
        explicit `live_only`), by `gui/main_window.py` for "save
        configuration" as well as for the arm/auto-rearm cycle (there
        without an argument - default `live_only=False`, i.e. WITH
        storage, matching the earlier behavior when the "live view only"
        checkbox was unchecked). Shows an error message and returns None
        on incomplete input (no active channel, no name, active channel
        without an assigned hardware channel).
        """
        channels = self._channel_table.get_channels()
        if not any(ch.enabled for ch in channels):
            self.show_error(t("error_no_active_channels"))
            return None

        # Without this check, an enabled but not-yet-assigned channel
        # (hardware_channel == "") would only surface deep in the
        # hardware layer as a cryptic "invalid channel name" error
        # (nidaqmx rejects an empty channel string) - here the actual
        # cause can be clearly named.
        unassigned = [ch for ch in channels if ch.enabled and not ch.hardware_channel.strip()]
        if unassigned:
            names = ", ".join(ch.display_name for ch in unassigned)
            self.show_error(t("error_channel_missing_hw_channel", names=names))
            return None

        name = self._name_edit.text().strip()
        if not name:
            self.show_error(t("error_no_name"))
            return None

        sample_rate = self._sample_rate_spin.value()
        # No more hard NI9210 block: an NI9210 combined with a faster
        # module is no longer an error case since `resolve_rate_groups()`
        # (data/models.py) - instead it results in two separate sampling
        # groups merged via RateMerger (see
        # core/controller.py::start_measurement). The remaining NI9234/
        # NI9213 checks below remain unchanged - those are intrinsic rate
        # violations, independent of the NI9210.
        if any(
            ch.enabled and ch.module_type == ModuleType.NI9234 for ch in channels
        ) and not is_valid_ni9234_sample_rate(sample_rate):
            self.show_error(
                t(
                    "error_ni9234_invalid_sample_rate",
                    suggestion=f"{next_ni9234_sample_rate_at_or_above(sample_rate):.1f}",
                )
            )
            return None

        if any(
            ch.enabled and ch.module_type == ModuleType.NI9235 for ch in channels
        ) and not is_valid_grid_sample_rate(ModuleType.NI9235, sample_rate):
            self.show_error(
                t(
                    "error_ni9235_invalid_sample_rate",
                    suggestion=f"{next_grid_sample_rate_at_or_above(ModuleType.NI9235, sample_rate):.1f}",
                )
            )
            return None

        for device_name, group_channels in ni9213_device_groups(channels).items():
            max_rate = max_ni9213_sample_rate_hz(group_channels)
            if sample_rate > max_rate + 0.05:
                mode_key = _ADC_TIMING_MODE_LABEL_KEYS.get(
                    group_channels[0].adc_timing_mode, "adc_timing_mode_high_resolution"
                )
                self.show_error(
                    t(
                        "error_ni9213_rate_too_high",
                        device=device_name,
                        count=len(group_channels),
                        mode=t(mode_key),
                        max_rate=f"{max_rate:.1f}",
                    )
                )
                return None

        active_channels = [ch for ch in channels if ch.enabled]
        # Ring buffer size/block size must be based on the ACTUAL tick
        # rate (= fastest rate group), not the raw target rate: with a
        # standalone NI9210, for example, the target rate is irrelevant
        # (always 14 S/s) - block sizes computed from the raw target rate
        # would be far too large there and would cause the first read
        # cycle to time out.
        try:
            rate_groups = resolve_rate_groups(active_channels, sample_rate)
            effective_tick_rate = max(
                (g.resolved_sample_rate_hz for g in rate_groups), default=sample_rate
            )
        except ValueError:
            effective_tick_rate = sample_rate

        ring_buffer_size = self._calculate_dynamic_buffer_size(
            effective_tick_rate, len(active_channels)
        )
        samples_per_read = self._calculate_samples_per_read(effective_tick_rate)

        return MeasurementConfig(
            name=name,
            sample_rate_hz=sample_rate,
            channels=channels,
            storage_format=StorageFormat(self._storage_format_combo.currentData()),
            samples_per_read=samples_per_read,
            ring_buffer_size=ring_buffer_size,
            save_to_disk=not live_only,
            recording_unlimited=self._recording_unlimited_checkbox.isChecked(),
            recording_stop_value=float(self._recording_stop_spin.value()),
            recording_stop_unit=RecordingStopUnit(self._recording_stop_unit_combo.currentData()),
            # trigger deliberately NOT set (default `TriggerConfig()`,
            # i.e. no trigger) - since the generalization to start AND
            # stop, the actually active trigger configuration lives in
            # `gui/main_window.py` (see
            # `gui/trigger_settings_dialog.py::TriggerSettingsDialog`),
            # no longer in the setup view - `MainWindow` feeds it
            # directly into `config.trigger` (see `_on_start_measurement`,
            # `_on_save_config`).
        )

    def apply_config(self, config: MeasurementConfig) -> None:
        """Transfers a loaded configuration into the UI fields.

        Called by `gui/main_window.py` after "load configuration".
        `samples_per_read`/`ring_buffer_size` are deliberately NOT
        applied: they are not editable UI fields and are always freshly
        recomputed at start from sample rate/channel count/available RAM
        (see `build_current_config`/`_calculate_dynamic_buffer_size`) -
        identical to the behavior with a manually entered configuration.
        """
        self._name_edit.setText(config.name)
        self._sample_rate_spin.setValue(config.sample_rate_hz)
        self._populate_storage_format_combo(config.storage_format.value)
        self._recording_unlimited_checkbox.setChecked(config.recording_unlimited)
        self._recording_stop_spin.setValue(max(1, int(config.recording_stop_value)))
        self._populate_recording_stop_unit_combo(config.recording_stop_unit.value)
        self._on_recording_unlimited_toggled(self._recording_unlimited_checkbox.isChecked())
        self._channel_table.set_channels(config.channels)
        self._update_resolved_rate_preview()
        # config.trigger is NOT applied here - `gui/main_window.py` reads
        # it directly from the loaded `MeasurementConfig` and sets it as
        # its own `_trigger_config` (see `_on_load_config`).

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _on_naming_scheme_changed(self) -> None:
        """Persists the naming scheme immediately on every change."""
        self._naming_digits_spin.setEnabled(self._naming_number_checkbox.isChecked())
        scheme = self.get_naming_scheme()
        self._configuration_manager.update_naming_scheme(
            use_number_suffix=scheme.use_number_suffix,
            number_suffix_digits=scheme.number_suffix_digits,
            include_date=scheme.include_date,
            include_time=scheme.include_time,
        )

    def _on_recording_unlimited_toggled(self, checked: bool) -> None:
        """Grays out the recording-limit value/unit while "unlimited" is
        active - purely UI feedback, no immediate persistence (as with
        the "live view only" checkbox, the value is only saved at start/
        close via `update_last_measurement_parameters`, see
        `get_current_measurement_parameters`)."""
        enabled = not checked
        self._recording_stop_spin.setEnabled(enabled)
        self._recording_stop_unit_combo.setEnabled(enabled)

    def _on_play_clicked(self) -> None:
        config = self.build_current_config(live_only=True)
        if config is None:
            return
        self.start_measurement_requested.emit(config)

    def _on_record_clicked(self) -> None:
        config = self.build_current_config(live_only=False)
        if config is None:
            return
        self.start_measurement_requested.emit(config)

    def _on_trigger_arm_button_toggled(self, checked: bool) -> None:
        self._set_trigger_arm_button_text()
        self.trigger_arm_toggled.emit(checked)

    def _set_trigger_arm_button_text(self) -> None:
        key = "trigger_disarm_button" if self._trigger_arm_button.isChecked() else "trigger_arm_button"
        self._trigger_arm_button.setText(f"  {t(key)}")
        fix_toggle_button_width(
            self._trigger_arm_button,
            f"  {t('trigger_arm_button')}",
            f"  {t('trigger_disarm_button')}",
        )

    def _retheme_start_button_icons(self) -> None:
        # Play/record have fixed, theme-independent icon colors (see
        # `gui/theme.py::PLAY_ICON_COLOR`/`RECORD_ICON_COLOR`). Stop has
        # NO hardcoded background (see `ACTION_BUTTON_STYLE`) and
        # therefore stays with the normal theme-dependent
        # `nav_icon_color()` (no `color=` passed).
        self._play_button.setIcon(QIcon(draw_play_icon(24, y_offset=0.6, color=PLAY_ICON_COLOR)))
        self._record_button.setIcon(
            QIcon(draw_record_icon(24, y_offset=0.6, color=RECORD_ICON_COLOR))
        )
        self._stop_button.setIcon(QIcon(draw_stop_icon(24, y_offset=0.6)))
        # `ACTION_BUTTON_STYLE` references `palette(...)` - without a
        # manual unpolish()/polish(), the border/background visually
        # stays stuck in the old theme after a live theme switch (same
        # finding as with the navigation tiles, see
        # `gui/main_window.py::_retheme_nav_icons`).
        for button in (self._play_button, self._record_button, self._stop_button):
            repolish(button)

    def _retheme_trigger_arm_button_icon(self) -> None:
        # No more hardcoded background (see `TRIGGER_ARM_BUTTON_STYLE`) -
        # the icon therefore stays with the normal theme-dependent
        # `nav_icon_color()` (no `color=` passed).
        # The icon itself is NOT separately recolored for the checked
        # (armed) state (only the text via `palette(highlighted-text)`
        # in the stylesheet) - black/white remains sufficiently readable
        # on the accent color (`palette(highlight)`) in both themes.
        self._trigger_arm_button.setIcon(QIcon(draw_trigger_icon(24, y_offset=0.6)))
        repolish(self._trigger_arm_button)

    def _update_start_button_labels(self) -> None:
        # Short button text (see `play_button_label`/`record_button_label`/
        # `stop_button_label`) AND a more detailed tooltip (existing
        # `live_only`/`start_measurement`/`stop_measurement` keys).
        self._play_button.setText(f"  {t('play_button_label')}")
        self._play_button.setToolTip(t("live_only"))
        self._record_button.setText(f"  {t('record_button_label')}")
        self._record_button.setToolTip(t("start_measurement"))
        self._stop_button.setText(f"  {t('stop_button_label')}")
        self._stop_button.setToolTip(t("stop_measurement"))

    def _apply_section_header_emphasis(self) -> None:
        """Emphasizes only section labels and stays fully theme-safe."""
        header_font = QFont(self.font())
        if header_font.pointSize() > 0:
            header_font.setPointSize(header_font.pointSize() + 2)
        header_font.setBold(True)

        for header in (
            self._device_header,
            self._channel_header,
            self._measurement_header,
            self._storage_header,
        ):
            header.setFont(header_font)
            margins = header.contentsMargins()
            header.setContentsMargins(
                margins.left(),
                8,
                margins.right(),
                4,
            )

    def _update_resolved_rate_preview(self) -> None:
        """Updates the non-blocking rate-group preview.

        Deliberately tolerant: a `ValueError` (e.g. an invalid NI9234
        rate momentarily while typing) is swallowed here and the preview
        simply cleared - `build_current_config()` remains the sole
        binding validation (at start/record), this preview is purely
        supplementary information.
        """
        active_channels = [ch for ch in self._channel_table.get_channels() if ch.enabled]
        sample_rate = self._sample_rate_spin.value()
        try:
            rate_groups = resolve_rate_groups(active_channels, sample_rate)
        except ValueError:
            self._resolved_rate_preview_label.setText("")
            return

        if len(rate_groups) <= 1:
            # Normal case: exactly one group, target rate == actual
            # rate - no extra info needed, label stays empty/invisible.
            self._resolved_rate_preview_label.setText("")
            return

        parts = []
        for group in rate_groups:
            if group.reason == "Zielrate":
                parts.append(t("resolved_rate_preview_target", rate=f"{group.resolved_sample_rate_hz:.1f}"))
            else:
                module_names = sorted({ch.module_type.value for ch in group.channels})
                parts.append(
                    t(
                        "resolved_rate_preview_fixed",
                        modules="/".join(module_names),
                        rate=f"{group.resolved_sample_rate_hz:.1f}",
                    )
                )
        self._resolved_rate_preview_label.setText(" · ".join(parts))

    def _calculate_samples_per_read(self, sample_rate_hz: float) -> int:
        """Computes an adaptive block size per DAQ read.

        Derived purely from the target BLOCK DURATION
        (`_target_read_block_ms`), not from a fixed sample count -
        thereby scaling dynamically with the sample rate: at a high rate,
        many samples per block (keeps the call frequency of
        `device.read()` consistently low, see `__init__`), at a low rate
        (e.g. NI9210 at 14 S/s) correspondingly few - this keeps the live
        view fluid even there, instead of updating in rare but large
        bursts. Capped from above by `_max_samples_per_read`, floored at
        a minimum of 1 sample.
        """
        target = int(sample_rate_hz * (self._target_read_block_ms / 1000.0))
        return max(1, min(self._max_samples_per_read, target))

    def _calculate_dynamic_buffer_size(self, sample_rate_hz: float, num_active_channels: int) -> int:
        """Computes the buffer size dynamically based on available RAM.

        Uses ~10% of the available RAM for the ring buffer, capped at
        120s. With very little free RAM, the otherwise usual 10s minimum
        size is deliberately undercut (with a warning) rather than
        exceeding the RAM limit - a fixed minimum buffer would otherwise
        cause a MemoryError at measurement start when memory is tight.
        """
        try:
            import psutil
            available_ram_bytes = psutil.virtual_memory().available
            bytes_per_sample = 8.0 * num_active_channels  # float64 per channel
            max_duration_from_ram = (available_ram_bytes * 0.1) / bytes_per_sample / sample_rate_hz

            duration_seconds = min(120.0, max_duration_from_ram)
            if duration_seconds < 10.0:
                logger.warning(
                    "Wenig freier Arbeitsspeicher (%.0f MB verfügbar): Ring Buffer "
                    "wird auf %.1f s begrenzt statt der üblichen Mindestgröße von 10 s.",
                    available_ram_bytes / (1024 ** 2),
                    duration_seconds,
                )
            return max(1, int(sample_rate_hz * duration_seconds))
        except Exception:
            # Fall back to a static size on error
            logger.debug("Fehler bei dynamischer RAM-Berechnung, nutze Fallback")
            return int(sample_rate_hz * self._default_ring_buffer_seconds)

