"""
hardware/ni9210.py

Concrete implementation for the NI 9210 module (4-channel
thermocouple input, ±78 mV, with built-in
cold-junction compensation/CJC).

See `hardware/nidaq_device.py` for the shared task lifecycle
and the note on the hardware testing caveat.

Also the base for `hardware/ni9213.py` (NI 9213, 16 channels): both
modules use `add_ai_thrmcpl_chan` identically for channel creation and
differ in software only by module type/channel count - NI9213 therefore
inherits directly from `NI9210` instead of duplicating the channel logic.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

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
if TYPE_CHECKING:
    from nidaqmx.task.channels import AIChannel

logger = logging.getLogger(__name__)

# Fallback measurement range in case an unknown thermocouple_type value
# occurs (e.g. from an older config file) - covers the largest practical
# range across the supported types (see
# `data.models.THERMOCOUPLE_TEMPERATURE_RANGES_C`).
_DEFAULT_TEMPERATURE_RANGE_C = (-200.0, 1372.0)


class NI9210(NIDAQDevice):
    """NI 9210: 4-channel thermocouple input (J/K/T/E/N/R/S/B), ±78 mV.

    Expects all passed-in channels to use `signal_type ==
    SignalType.THERMOCOUPLE`. Cold-junction compensation is provided by
    the module's built-in CJC sensor (`CJCSource.BUILT_IN`) - no external
    CJC source can be configured, since the module offers no additional
    terminals for that.

    The temperature measurement range (`min_val`/`max_val` for
    `add_ai_thrmcpl_chan`) is NOT taken from `channel.min_range`/
    `max_range` (their dataclass default of -10.0/10.0 V is meaningless
    for °C and not editable in the channel table), but derived from
    `THERMOCOUPLE_TEMPERATURE_RANGES_C` based on
    `channel.thermocouple_type`.

    NOTE ON ADC TIMING MODE: unlike the NI9213, the NI9210 has a fixed
    sample rate (14 S/s total) with no configurable ADC timing mode;
    `channel.adc_timing_mode` is therefore deliberately NOT evaluated
    here (see `hardware/ni9213.py`, where it is).
    """

    # Overridden by `NI9213` (see hardware/ni9213.py) - controls both the
    # assigned ModuleType and the error message texts.
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

    def _add_channel_to_task(self, task, channel: Channel) -> "AIChannel":
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

        # Return value (the created AIChannel object) is used by NI9213
        # to additionally set the ADC timing mode (see
        # hardware/ni9213.py) - unused here itself.
        return task.ai_channels.add_ai_thrmcpl_chan(
            physical_channel=channel.hardware_channel,
            name_to_assign_to_channel=channel.display_name,
            min_val=min_val,
            max_val=max_val,
            units=TemperatureUnits.DEG_C,
            thermocouple_type=thermocouple_type,
            cjc_source=CJCSource.BUILT_IN,
        )
