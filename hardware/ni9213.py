"""
hardware/ni9213.py

Concrete implementation for the NI 9213 module (16-channel
thermocouple input, ±78 mV, with built-in
cold-junction compensation/CJC).

Inherits channel creation from `hardware/ni9210.py::NI9210` - both modules
use `add_ai_thrmcpl_chan` identically for this and differ in software
only by module type/channel count (the channel count already results
automatically from the physical channels reported by the hardware, see
`hardware/nidaq_device.py::discover_devices`).

There is one real difference in the ADC timing mode: ONLY the NI9213
supports a hardware-configurable trade-off between speed and effective
resolution (the NI9210 has a fixed sample rate of 14 S/s with no such
option) - hence set here in addition to the inherited channel creation.
"""

from __future__ import annotations

import logging

from data.models import Channel, ModuleType
from hardware.base_device import AcquisitionError
from hardware.ni9210 import NI9210
from hardware.nidaq_device import NIDAQMX_AVAILABLE

if NIDAQMX_AVAILABLE:
    from nidaqmx.constants import ADCTimingMode

logger = logging.getLogger(__name__)


class NI9213(NI9210):
    """NI 9213: 16-channel thermocouple input (J/K/T/E/N/R/S/B), ±78 mV.

    See `NI9210` for details on channel creation/cold-junction
    compensation - additionally, `channel.adc_timing_mode` is set here
    (see `data/models.py::ADC_TIMING_MODES`).

    IMPORTANT: nidaqmx requires the same ADC timing mode for ALL channels
    of the same physical module ("You must use the same ADC timing mode
    for all channels on a device") - the channel table
    (`gui/widgets/channel_table.py`) therefore automatically propagates a
    change to all channels of the same module. This is NOT checked/
    enforced here itself; for conflicting values the NI-DAQmx driver
    reports an error when configuring the task.
    """

    _MODULE_TYPE = ModuleType.NI9213
    _MODULE_LABEL = "NI9213"

    def _add_channel_to_task(self, task, channel: Channel) -> None:
        ai_channel = super()._add_channel_to_task(task, channel)
        try:
            timing_mode = ADCTimingMode[channel.adc_timing_mode]
        except KeyError as exc:
            raise AcquisitionError(
                f"Unbekannter ADC-Timing-Modus '{channel.adc_timing_mode}' für "
                f"Kanal '{channel.display_name}'."
            ) from exc
        ai_channel.ai_adc_timing_mode = timing_mode
