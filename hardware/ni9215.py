"""
hardware/ni9215.py

Konkrete Implementierung für das NI 9215 Modul (4 analoge
Spannungseingänge, ±10 V, IEPE nicht unterstützt).

Siehe `hardware/nidaq_device.py` für den gemeinsamen Task-Lebenszyklus
(Konfiguration, Start/Stop, Lesen) und den Hinweis zum Hardware-Testvorbehalt.
"""

from __future__ import annotations

import logging

from data.models import Channel, DeviceInfo, ModuleType, SignalType
from hardware.base_device import AcquisitionError
from hardware.nidaq_device import NIDAQDevice, NIDAQMX_AVAILABLE

if NIDAQMX_AVAILABLE:
    from nidaqmx.constants import TerminalConfiguration, VoltageUnits

logger = logging.getLogger(__name__)

# Physikalische Grenzen des NI 9215 laut Datenblatt.
NI9215_MIN_VOLTAGE = -10.0
NI9215_MAX_VOLTAGE = 10.0


class NI9215(NIDAQDevice):
    """NI 9215: 4-Kanal analoger Spannungseingang, ±10 V.

    Erwartet, dass alle übergebenen Kanäle `signal_type == SignalType.VOLTAGE`
    verwenden. `min_range`/`max_range` der Kanäle werden auf den zulässigen
    Bereich des Moduls begrenzt (Clamping) und geloggt, falls sie außerhalb liegen.
    """

    def __init__(self, device_info: DeviceInfo, channels: list[Channel]) -> None:
        for channel in channels:
            if channel.signal_type != SignalType.VOLTAGE:
                raise AcquisitionError(
                    f"NI9215 unterstützt nur SignalType.VOLTAGE, "
                    f"Kanal '{channel.display_name}' hat {channel.signal_type}."
                )
        device_info.module_type = ModuleType.NI9215
        super().__init__(device_info, channels)

    def _add_channel_to_task(self, task, channel: Channel) -> None:
        min_val = channel.min_range if channel.min_range is not None else NI9215_MIN_VOLTAGE
        max_val = channel.max_range if channel.max_range is not None else NI9215_MAX_VOLTAGE

        clamped_min = max(min_val, NI9215_MIN_VOLTAGE)
        clamped_max = min(max_val, NI9215_MAX_VOLTAGE)
        if clamped_min != min_val or clamped_max != max_val:
            logger.warning(
                "Kanal '%s': Messbereich [%.2f, %.2f] V liegt außerhalb des "
                "NI9215-Bereichs, wird auf [%.2f, %.2f] V begrenzt.",
                channel.display_name,
                min_val,
                max_val,
                clamped_min,
                clamped_max,
            )

        task.ai_channels.add_ai_voltage_chan(
            physical_channel=channel.hardware_channel,
            name_to_assign_to_channel=channel.display_name,
            terminal_config=TerminalConfiguration.DEFAULT,
            min_val=clamped_min,
            max_val=clamped_max,
            units=VoltageUnits.VOLTS,
        )
