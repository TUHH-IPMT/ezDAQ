"""
hardware/ni9210.py

Konkrete Implementierung für das NI 9210 Modul (4-Kanal
Thermoelement-Eingang, ±78 mV, mit eingebauter
Kaltstellenkompensation/CJC).

Siehe `hardware/nidaq_device.py` für den gemeinsamen Task-Lebenszyklus
und den Hinweis zum Hardware-Testvorbehalt.

Basis auch für `hardware/ni9213.py` (NI 9213, 16 Kanäle): beide Module
nutzen zur Kanalerzeugung identisch `add_ai_thrmcpl_chan` und
unterscheiden sich softwareseitig nur durch Modultyp/Kanalzahl - NI9213
erbt daher direkt von `NI9210`, statt die Kanal-Logik zu duplizieren.
"""

from __future__ import annotations

import logging

from data.models import (
    THERMOCOUPLE_TEMPERATURE_RANGES_C,
    Channel,
    DeviceInfo,
    ModuleType,
    SignalType,
)
from hardware.base_device import AcquisitionError
from hardware.nidaq_device import NIDAQDevice, NIDAQMX_AVAILABLE

if NIDAQMX_AVAILABLE:
    from nidaqmx.constants import CJCSource, TemperatureUnits, ThermocoupleType

logger = logging.getLogger(__name__)

# Fallback-Messbereich, falls ein unbekannter thermocouple_type-Wert
# auftritt (z. B. aus einer älteren Konfigurationsdatei) - deckt den
# größten praktischen Bereich der unterstützten Typen ab (siehe
# `data.models.THERMOCOUPLE_TEMPERATURE_RANGES_C`).
_DEFAULT_TEMPERATURE_RANGE_C = (-200.0, 1372.0)


class NI9210(NIDAQDevice):
    """NI 9210: 4-Kanal Thermoelement-Eingang (J/K/T/E/N/R/S/B), ±78 mV.

    Erwartet, dass alle übergebenen Kanäle `signal_type ==
    SignalType.THERMOCOUPLE` verwenden. Die Kaltstellenkompensation
    erfolgt über den eingebauten CJC-Sensor des Moduls
    (`CJCSource.BUILT_IN`) - keine externe CJC-Quelle konfigurierbar, da
    das Modul dafür keine zusätzlichen Anschlüsse bietet.

    Der Temperaturmessbereich (`min_val`/`max_val` für
    `add_ai_thrmcpl_chan`) wird NICHT aus `channel.min_range`/`max_range`
    übernommen (deren Dataclass-Default -10.0/10.0 V ist für °C
    bedeutungslos und in der Kanaltabelle nicht editierbar), sondern aus
    `THERMOCOUPLE_TEMPERATURE_RANGES_C` anhand von
    `channel.thermocouple_type` abgeleitet.
    """

    # Von `NI9213` überschrieben (siehe hardware/ni9213.py) - steuert
    # sowohl den zugewiesenen ModuleType als auch die Fehlermeldungstexte.
    _MODULE_TYPE = ModuleType.NI9210
    _MODULE_LABEL = "NI9210"

    def __init__(self, device_info: DeviceInfo, channels: list[Channel]) -> None:
        for channel in channels:
            if channel.signal_type != SignalType.THERMOCOUPLE:
                raise AcquisitionError(
                    f"{self._MODULE_LABEL} unterstützt nur SignalType.THERMOCOUPLE, "
                    f"Kanal '{channel.display_name}' hat {channel.signal_type}."
                )
        device_info.module_type = self._MODULE_TYPE
        super().__init__(device_info, channels)

    def _add_channel_to_task(self, task, channel: Channel) -> None:
        try:
            thermocouple_type = ThermocoupleType[channel.thermocouple_type]
        except KeyError as exc:
            raise AcquisitionError(
                f"Unbekannter Thermoelement-Typ '{channel.thermocouple_type}' für "
                f"Kanal '{channel.display_name}'."
            ) from exc

        min_val, max_val = THERMOCOUPLE_TEMPERATURE_RANGES_C.get(
            channel.thermocouple_type, _DEFAULT_TEMPERATURE_RANGE_C
        )

        task.ai_channels.add_ai_thrmcpl_chan(
            physical_channel=channel.hardware_channel,
            name_to_assign_to_channel=channel.display_name,
            min_val=min_val,
            max_val=max_val,
            units=TemperatureUnits.DEG_C,
            thermocouple_type=thermocouple_type,
            cjc_source=CJCSource.BUILT_IN,
        )
