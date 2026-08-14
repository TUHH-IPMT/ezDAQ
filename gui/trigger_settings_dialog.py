"""
gui/trigger_settings_dialog.py

Dialog "Trigger-Einstellungen": Konfiguration von Start- UND Stopp-Trigger
für Messungen (siehe `data/models.py::TriggerConfig`), erreichbar über
Einstellungen -> "Trigger-Einstellungen..." (siehe
`gui/main_window.py::_on_open_trigger_settings_dialog`).

Aufgeschobene Übernahme wie `gui/live_view.py::ChannelDisplayDialog`:
nichts wirkt vor OK, Abbrechen verwirft rückstandslos (inkl. eines
laufenden Test-Lauschers, siehe `reject()`).

Start und Stopp sind unabhängig konfigurierbar (je eine `TriggerCondition`,
Modus Manuell/Schwellwert/Seriell) - beide Sektionen werden über denselben
Baustein `_build_condition_section()` erzeugt, EIN Codepfad statt zwei
Kopien. Das Test-Panel hat bewusst eigene, unabhängige Port-/Baudrate-/
Signal-Felder (nicht an Start oder Stopp gebunden) - der Zweck ("kommt das
Signal wirklich an") ist unabhängig davon, wofür der Port später genutzt
wird, und ohnehin ist immer nur ein Port gleichzeitig sinnvoll testbar.
"""

from __future__ import annotations

import logging

import serial.tools.list_ports
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from data.models import Channel, TriggerCondition, TriggerConfig, TriggerDirection, TriggerKind
from gui.i18n import t
from gui.serial_trigger import SerialTriggerListener
from gui.widgets.spinbox import PrecisionDoubleSpinBox

logger = logging.getLogger(__name__)

_TRIGGER_KIND_LABEL_KEYS: dict[TriggerKind, str] = {
    TriggerKind.NONE: "trigger_mode_manual",
    TriggerKind.THRESHOLD: "trigger_mode_threshold",
    TriggerKind.SERIAL: "trigger_mode_serial",
}
_TRIGGER_DIRECTION_LABEL_KEYS: dict[TriggerDirection, str] = {
    TriggerDirection.RISES_ABOVE: "trigger_direction_rises_above",
    TriggerDirection.FALLS_BELOW: "trigger_direction_falls_below",
    TriggerDirection.ABS_EXCEEDS: "trigger_direction_abs_exceeds",
}
# Von `gui/setup_view.py` hierher übernommen (dort entfällt die Trigger-UI
# komplett, siehe Rückbau in derselben Umsetzungsrunde).
TRIGGER_SERIAL_BAUD_RATES = ["9600", "19200", "38400", "57600", "115200"]


class _ConditionSectionWidgets:
    """Hält die Widget-Referenzen EINER Start- oder Stopp-Sektion (siehe
    `TriggerSettingsDialog._build_condition_section`)."""

    kind_combo: QComboBox
    threshold_widget: QWidget
    channel_combo: QComboBox
    threshold_spin: PrecisionDoubleSpinBox
    direction_combo: QComboBox
    pretrigger_spin: QDoubleSpinBox | None
    serial_widget: QWidget
    serial_port_combo: QComboBox
    serial_baud_combo: QComboBox
    serial_message_edit: QLineEdit


class TriggerSettingsDialog(QDialog):
    """Dialog zur Konfiguration von Start- UND Stopp-Trigger, inkl. eines
    Test-Panels für den seriellen Trigger.

    Args:
        trigger_config: Aktuelle Konfiguration (Vorbelegung der Felder).
        channels: Aktive Kanäle, aus denen für einen Schwellwert-Trigger
            gewählt werden kann (siehe `gui/main_window.py`, übergibt die
            im Setup konfigurierten AKTIVEN Kanäle).

    Ergebnis nach `exec() == QDialog.DialogCode.Accepted` über `results()`.
    """

    def __init__(
        self,
        trigger_config: TriggerConfig,
        channels: list[Channel],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("trigger_settings_dialog_title"))
        self._channels = channels
        self._test_listener: SerialTriggerListener | None = None
        self._result: TriggerConfig | None = None

        layout = QVBoxLayout(self)

        sections_row = QHBoxLayout()
        self._start_group, self._start_widgets = self._build_condition_section(
            t("trigger_start_section_title"), trigger_config.start, channels, show_pretrigger=True
        )
        self._stop_group, self._stop_widgets = self._build_condition_section(
            t("trigger_stop_section_title"), trigger_config.stop, channels, show_pretrigger=False
        )
        self._start_widgets.kind_combo.currentIndexChanged.connect(self._update_test_panel)
        self._stop_widgets.kind_combo.currentIndexChanged.connect(self._update_test_panel)
        if self._start_widgets.pretrigger_spin is not None:
            self._start_widgets.pretrigger_spin.setValue(trigger_config.pretrigger_seconds)
        sections_row.addWidget(self._start_group)
        sections_row.addWidget(self._stop_group)
        layout.addLayout(sections_row)

        # --- Test-Panel (siehe Klassendoc oben) ---
        self._test_group = QGroupBox(t("trigger_test_group_title"))
        test_layout = QVBoxLayout(self._test_group)
        test_form = QFormLayout()
        self._test_source_combo = QComboBox()
        test_form.addRow(
            f"{t('trigger_test_source_label')}:", self._test_source_combo
        )
        test_layout.addLayout(test_form)

        self._test_toggle_button = QPushButton(t("trigger_test_start_button"))
        self._test_toggle_button.clicked.connect(self._on_test_toggle_clicked)
        test_layout.addWidget(self._test_toggle_button)

        # Zeigt fortlaufend, was auf dem Test-Port tatsächlich ankommt
        # (siehe `gui/serial_trigger.py::SerialTriggerListener.data_received`)
        # - so kann der Nutzer VOR dem eigentlichen Einsatz als Trigger
        # prüfen, ob das erwartete Signal wirklich eintrifft.
        self._test_log = QPlainTextEdit()
        self._test_log.setReadOnly(True)
        self._test_log.setMaximumBlockCount(500)
        test_layout.addWidget(self._test_log)

        layout.addWidget(self._test_group)

        self._refresh_ports(self._start_widgets.serial_port_combo)
        self._refresh_ports(self._stop_widgets.serial_port_combo)
        self._update_test_panel()

        button_box = QDialogButtonBox()
        ok_button = button_box.addButton(t("ok"), QDialogButtonBox.ButtonRole.AcceptRole)
        cancel_button = button_box.addButton(t("cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        ok_button.clicked.connect(self.accept)
        cancel_button.clicked.connect(self.reject)
        layout.addWidget(button_box)

    # ------------------------------------------------------------------ #
    # Aufbau
    # ------------------------------------------------------------------ #

    def _build_condition_section(
        self,
        title: str,
        condition: TriggerCondition,
        channels: list[Channel],
        show_pretrigger: bool,
    ) -> tuple[QGroupBox, _ConditionSectionWidgets]:
        """Baut EINE Start- oder Stopp-Sektion (Modus-Combo + je nach Modus
        sichtbare Schwellwert-/Seriell-Felder) - gemeinsam genutzt für
        beide Seiten, siehe Klassendoc oben."""
        group = QGroupBox(title)
        v = QVBoxLayout(group)
        widgets = _ConditionSectionWidgets()

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel(f"{t('trigger_mode_label')}:"))
        kind_combo = QComboBox()
        for kind in TriggerKind:
            kind_combo.addItem(t(_TRIGGER_KIND_LABEL_KEYS[kind]), kind.value)
        index = kind_combo.findData(condition.kind.value)
        kind_combo.setCurrentIndex(index if index >= 0 else 0)
        mode_row.addWidget(kind_combo, stretch=1)
        v.addLayout(mode_row)
        widgets.kind_combo = kind_combo

        # Schwellwert-Felder - nur sichtbar bei kind=THRESHOLD.
        threshold_widget = QWidget()
        threshold_form = QFormLayout(threshold_widget)
        threshold_form.setContentsMargins(0, 0, 0, 0)
        channel_combo = QComboBox()
        for channel in channels:
            channel_combo.addItem(channel.display_name, channel.hardware_channel)
        idx = channel_combo.findData(condition.threshold_channel_hardware_id)
        if idx >= 0:
            channel_combo.setCurrentIndex(idx)
        threshold_form.addRow(f"{t('trigger_channel_label')}:", channel_combo)
        threshold_spin = PrecisionDoubleSpinBox()
        threshold_spin.setRange(-1e9, 1e9)
        threshold_spin.setValue(condition.threshold_value)
        threshold_form.addRow(f"{t('trigger_threshold_value_label')}:", threshold_spin)
        direction_combo = QComboBox()
        for direction in TriggerDirection:
            direction_combo.addItem(t(_TRIGGER_DIRECTION_LABEL_KEYS[direction]), direction.value)
        idx = direction_combo.findData(condition.threshold_direction.value)
        direction_combo.setCurrentIndex(idx if idx >= 0 else 0)
        threshold_form.addRow(f"{t('trigger_direction_label')}:", direction_combo)
        pretrigger_spin: QDoubleSpinBox | None = None
        if show_pretrigger:
            pretrigger_spin = QDoubleSpinBox()
            pretrigger_spin.setRange(0.0, 3600.0)
            pretrigger_spin.setDecimals(1)
            threshold_form.addRow(
                f"{t('trigger_pretrigger_seconds_label')}:", pretrigger_spin
            )
        v.addWidget(threshold_widget)
        widgets.threshold_widget = threshold_widget
        widgets.channel_combo = channel_combo
        widgets.threshold_spin = threshold_spin
        widgets.direction_combo = direction_combo
        widgets.pretrigger_spin = pretrigger_spin

        # Seriell-Felder - nur sichtbar bei kind=SERIAL.
        serial_widget = QWidget()
        serial_form = QFormLayout(serial_widget)
        serial_form.setContentsMargins(0, 0, 0, 0)
        port_row = QHBoxLayout()
        serial_port_combo = QComboBox()
        serial_port_combo.setEditable(True)
        serial_port_combo.setCurrentText(condition.serial_port)
        refresh_button = QPushButton(t("trigger_serial_refresh_button"))
        refresh_button.clicked.connect(lambda: self._refresh_ports(serial_port_combo))
        port_row.addWidget(serial_port_combo, stretch=1)
        port_row.addWidget(refresh_button)
        serial_form.addRow(f"{t('trigger_serial_port_label')}:", port_row)
        serial_baud_combo = QComboBox()
        serial_baud_combo.setEditable(True)
        serial_baud_combo.addItems(TRIGGER_SERIAL_BAUD_RATES)
        serial_baud_combo.setCurrentText(str(condition.serial_baud_rate))
        serial_form.addRow(f"{t('trigger_serial_baud_label')}:", serial_baud_combo)
        serial_message_edit = QLineEdit(condition.serial_expected_message)
        serial_form.addRow(f"{t('trigger_serial_message_label')}:", serial_message_edit)
        v.addWidget(serial_widget)
        widgets.serial_widget = serial_widget
        widgets.serial_port_combo = serial_port_combo
        widgets.serial_baud_combo = serial_baud_combo
        widgets.serial_message_edit = serial_message_edit

        def _update_visibility(*_args) -> None:
            kind = TriggerKind(kind_combo.currentData())
            threshold_widget.setVisible(kind == TriggerKind.THRESHOLD)
            serial_widget.setVisible(kind == TriggerKind.SERIAL)

        kind_combo.currentIndexChanged.connect(_update_visibility)
        _update_visibility()

        return group, widgets

    @staticmethod
    def _refresh_ports(combo: QComboBox) -> None:
        """Listet die aktuell verfügbaren seriellen Schnittstellen neu auf
        - editierbare Combo, der Wert muss beim Konfigurieren nicht
        zwingend bereits angeschlossen sein (siehe vormals
        `gui/setup_view.py::_refresh_serial_ports`)."""
        previous = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        for port in serial.tools.list_ports.comports():
            combo.addItem(port.device)
        if previous:
            index = combo.findText(previous)
            if index >= 0:
                combo.setCurrentIndex(index)
            else:
                combo.setCurrentText(previous)
        combo.blockSignals(False)

    # ------------------------------------------------------------------ #
    # Test-Panel
    # ------------------------------------------------------------------ #

    def _serial_conditions(
        self,
    ) -> list[tuple[str, TriggerCondition, _ConditionSectionWidgets]]:
        conditions = []
        for label, widgets in (
            (t("trigger_test_source_start"), self._start_widgets),
            (t("trigger_test_source_stop"), self._stop_widgets),
        ):
            condition = self._condition_for_test(widgets)
            if condition.kind == TriggerKind.SERIAL:
                conditions.append((label, condition, widgets))
        return conditions

    @staticmethod
    def _condition_for_test(widgets: _ConditionSectionWidgets) -> TriggerCondition:
        kind = TriggerKind(widgets.kind_combo.currentData())
        if kind != TriggerKind.SERIAL:
            return TriggerCondition(kind=kind)
        try:
            baud_rate = int(widgets.serial_baud_combo.currentText().strip())
        except ValueError:
            baud_rate = 0
        return TriggerCondition(
            kind=kind,
            serial_port=widgets.serial_port_combo.currentText().strip(),
            serial_baud_rate=baud_rate,
            serial_expected_message=widgets.serial_message_edit.text(),
        )

    def _update_test_panel(self) -> None:
        if not hasattr(self, "_test_group"):
            return
        serial_conditions = self._serial_conditions()
        previous_source = self._test_source_combo.currentData()
        self._test_source_combo.blockSignals(True)
        self._test_source_combo.clear()
        for index, (label, _condition, _widgets) in enumerate(serial_conditions):
            self._test_source_combo.addItem(label, index)
        if serial_conditions:
            selected_index = previous_source if isinstance(previous_source, int) else 0
            self._test_source_combo.setCurrentIndex(
                min(selected_index, len(serial_conditions) - 1)
            )
        self._test_source_combo.blockSignals(False)
        self._test_group.setVisible(bool(serial_conditions))

    def _on_test_toggle_clicked(self) -> None:
        if self._test_listener is not None:
            self._stop_test_listener()
            return
        serial_conditions = self._serial_conditions()
        if not serial_conditions:
            return
        selected_index = self._test_source_combo.currentIndex()
        label, condition, _widgets = serial_conditions[selected_index]
        port = condition.serial_port
        if not port:
            self._append_log(t("error_trigger_serial_no_port"))
            return
        message = condition.serial_expected_message
        if not message:
            self._append_log(t("error_trigger_serial_empty_message"))
            return
        baud_rate = condition.serial_baud_rate
        if baud_rate <= 0:
            self._append_log(t("error_trigger_serial_invalid_baud"))
            return

        listener = SerialTriggerListener(port, baud_rate, message.encode("utf-8"))
        listener.data_received.connect(self._on_test_data_received)
        listener.message_matched.connect(self._on_test_message_matched)
        listener.connection_failed.connect(self._on_test_connection_failed)
        self._test_listener = listener
        listener.start()
        self._test_toggle_button.setText(t("trigger_test_stop_button"))
        self._append_log(f"--- {label}: {port} @ {baud_rate} ---")

    def _stop_test_listener(self) -> None:
        if self._test_listener is not None:
            self._test_listener.stop()
            self._test_listener.deleteLater()
            self._test_listener = None
        self._test_toggle_button.setText(t("trigger_test_start_button"))

    def _on_test_data_received(self, chunk: bytes) -> None:
        self._append_log(f"RX: {chunk.hex(' ')}")

    def _on_test_message_matched(self) -> None:
        self._append_log(f"*** {t('trigger_test_matched_log')} ***")

    def _on_test_connection_failed(self, message: str) -> None:
        self._append_log(f"!!! {message} !!!")
        self._stop_test_listener()

    def _append_log(self, text: str) -> None:
        self._test_log.appendPlainText(text)

    # ------------------------------------------------------------------ #
    # OK / Abbrechen
    # ------------------------------------------------------------------ #

    def reject(self) -> None:
        """Stoppt einen ggf. laufenden Test-Lauscher (siehe Klassendoc
        oben) - deckt Abbrechen-Button, Esc UND Fenster-X gleichermaßen ab."""
        self._stop_test_listener()
        super().reject()

    def accept(self) -> None:
        start = self._read_condition(self._start_widgets)
        if start is None:
            return
        stop = self._read_condition(self._stop_widgets)
        if stop is None:
            return
        pretrigger_seconds = (
            self._start_widgets.pretrigger_spin.value()
            if self._start_widgets.pretrigger_spin is not None
            else 5.0
        )
        self._result = TriggerConfig(start=start, stop=stop, pretrigger_seconds=pretrigger_seconds)
        self._stop_test_listener()
        super().accept()

    def _read_condition(self, widgets: _ConditionSectionWidgets) -> TriggerCondition | None:
        """Baut eine `TriggerCondition` aus den UI-Feldern EINER Sektion,
        mit Validierung je Modus - dieselben 4 Regeln wie vormals
        `gui/setup_view.py::_build_trigger_config`."""
        kind = TriggerKind(widgets.kind_combo.currentData())

        if kind == TriggerKind.THRESHOLD:
            channel_hw = widgets.channel_combo.currentData()
            if not channel_hw:
                QMessageBox.warning(self, t("error"), t("error_trigger_channel_not_active"))
                return None
            return TriggerCondition(
                kind=kind,
                threshold_channel_hardware_id=channel_hw,
                threshold_value=widgets.threshold_spin.value(),
                threshold_direction=TriggerDirection(widgets.direction_combo.currentData()),
            )

        if kind == TriggerKind.SERIAL:
            port = widgets.serial_port_combo.currentText().strip()
            if not port:
                QMessageBox.warning(self, t("error"), t("error_trigger_serial_no_port"))
                return None
            message = widgets.serial_message_edit.text()
            if not message:
                QMessageBox.warning(self, t("error"), t("error_trigger_serial_empty_message"))
                return None
            try:
                baud_rate = int(widgets.serial_baud_combo.currentText().strip())
            except ValueError:
                QMessageBox.warning(self, t("error"), t("error_trigger_serial_invalid_baud"))
                return None
            return TriggerCondition(
                kind=kind,
                serial_port=port,
                serial_baud_rate=baud_rate,
                serial_expected_message=message,
            )

        return TriggerCondition(kind=TriggerKind.NONE)

    def results(self) -> TriggerConfig:
        """Gibt die im Dialog eingestellte `TriggerConfig` zurück - nur
        gültig, wenn `exec()` zuvor `QDialog.DialogCode.Accepted` ergab."""
        assert self._result is not None
        return self._result
