"""
hardware/ni9234.py

Concrete implementation for the NI 9234 module (4-channel IEPE-capable
dynamic signal input, typically for accelerometers and microphones).

See `hardware/nidaq_device.py` for the shared task lifecycle
and the note on the hardware testing caveat.
"""

from __future__ import annotations

import logging

from data.models import Channel, DeviceInfo, ModuleType, SignalType
from hardware.base_device import AcquisitionError
from hardware.nidaq_device import DaqmxError, NIDAQDevice, NIDAQMX_AVAILABLE

if NIDAQMX_AVAILABLE:
    from nidaqmx.constants import (
        AccelSensitivityUnits,
        AccelUnits,
        ExcitationSource,
        TerminalConfiguration,
        VoltageUnits,
    )

logger = logging.getLogger(__name__)

# IEPE constant-current excitation of the NI 9234 per NI datasheet/operating
# instructions: minimum 2.0 mA, typically 2.1 mA (software-switchable
# on/off, not configurable in magnitude). Source: NI 9234 Operating
# Instructions and Specifications, "IEPE excitation current" section.
NI9234_IEPE_EXCITATION_AMPS = 0.002

# Physical input range of the NI 9234 per datasheet (±5 V).
NI9234_MIN_VOLTAGE = -5.0
NI9234_MAX_VOLTAGE = 5.0


class NI9234(NIDAQDevice):
    """NI 9234: 4-channel input for voltage or IEPE acceleration/microphones.

    Supports two operating modes per channel:
        * `SignalType.IEPE_ACCELERATION`: internal IEPE constant-current
          excitation active, the NI-DAQmx driver already converts to g in
          hardware via `sensitivity_mv_per_unit` (sensor sensitivity in
          mV/g) - this field is mandatory for that.
        * `SignalType.VOLTAGE`: normal voltage input without IEPE
          excitation, identical to NI9215 behavior (linear scaling via
          `channel.scale`/`channel.offset`), just with the NI9234's
          smaller ±5 V range.
    """

    def __init__(self, device_info: DeviceInfo, channels: list[Channel]) -> None:
        for channel in channels:
            if channel.signal_type not in (SignalType.VOLTAGE, SignalType.IEPE_ACCELERATION):
                raise AcquisitionError(
                    f"NI9234 unterstützt nur SignalType.VOLTAGE oder "
                    f"SignalType.IEPE_ACCELERATION, Kanal '{channel.display_name}' "
                    f"hat {channel.signal_type}."
                )
            if (
                channel.signal_type == SignalType.IEPE_ACCELERATION
                and channel.sensitivity_mv_per_unit is None
            ):
                raise AcquisitionError(
                    f"Kanal '{channel.display_name}' benötigt "
                    f"sensitivity_mv_per_unit (Sensorempfindlichkeit in mV/g) "
                    f"für IEPE-Beschleunigung am NI9234."
                )
        device_info.module_type = ModuleType.NI9234
        super().__init__(device_info, channels)

    def _add_channel_to_task(self, task, channel: Channel) -> None:
        min_val = channel.min_range if channel.min_range is not None else NI9234_MIN_VOLTAGE
        max_val = channel.max_range if channel.max_range is not None else NI9234_MAX_VOLTAGE

        if channel.signal_type == SignalType.IEPE_ACCELERATION:
            ai_channel = task.ai_channels.add_ai_accel_chan(
                physical_channel=channel.hardware_channel,
                name_to_assign_to_channel=channel.display_name,
                terminal_config=TerminalConfiguration.DEFAULT,
                min_val=min_val,
                max_val=max_val,
                units=AccelUnits.G,
                sensitivity=channel.sensitivity_mv_per_unit,
                sensitivity_units=AccelSensitivityUnits.MILLIVOLTS_PER_G,
                current_excit_source=ExcitationSource.INTERNAL,
                current_excit_val=NI9234_IEPE_EXCITATION_AMPS,
            )
        else:
            ai_channel = task.ai_channels.add_ai_voltage_chan(
                physical_channel=channel.hardware_channel,
                name_to_assign_to_channel=channel.display_name,
                terminal_config=TerminalConfiguration.DEFAULT,
                min_val=min_val,
                max_val=max_val,
                units=VoltageUnits.VOLTS,
            )

        # Being a delta-sigma ADC, the NI9234 has a fixed filter delay
        # between the analog sampling instant and the digitally read-out
        # sample (per datasheet approx. 40 sample clock periods + 3.2 us) -
        # unlike, say, the NI9215 (SAR ADC, practically no delay).
        # NI-DAQmx does NOT compensate for this automatically on C Series
        # DSA modules, not even in a shared task with other modules (channel
        # expansion) - without this property, NI9234 channels would be
        # time-offset relative to, say, NI9215 channels in the same task
        # (~1 sample period at low sample rates). Source: NI Knowledge Base
        # "Synchronized Data Delayed When Using NI DAQ Devices with
        # Delta-Sigma-ADC".
        #
        # BEST EFFORT: against real hardware (cDAQ-9185 + NI9234, current
        # driver version) DAQmx ALWAYS rejects this property (-200452
        # "Specified property is not supported by the device or is not
        # applicable to the task") - reproducible for both voltage and
        # IEPE accel channels alike, and regardless of whether it is set
        # before or after the sample clock timing configuration.
        # Apparently simply not supported by this driver/firmware
        # combination. Therefore does NOT abort the measurement if
        # setting it fails - only the (small, but noticeable at low
        # sample rates) time-offset correction is then skipped.
        try:
            ai_channel.ai_remove_filter_delay = True
        except DaqmxError:
            logger.warning(
                "AI_RemoveFilterDelay wird von %s (Kanal '%s') nicht "
                "unterstützt - Filterverzögerungs-Kompensation entfällt, "
                "Messung läuft trotzdem weiter.",
                self.device_info.device_name,
                channel.display_name,
            )
