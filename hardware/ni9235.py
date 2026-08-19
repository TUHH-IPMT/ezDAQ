"""
hardware/ni9235.py

Concrete implementation for the NI 9235 module (8-channel
strain gauge input, exclusively 120 Ω quarter-bridge,
simultaneous sampling).

See `hardware/nidaq_device.py` for the shared task lifecycle
and the note on the hardware testing caveat.
"""

from __future__ import annotations

import logging

from data.models import Channel, DeviceInfo, ModuleType, NI9235_BRIDGE_TYPES, SignalType
from hardware.base_device import AcquisitionError
from hardware.nidaq_device import DaqmxError, NIDAQDevice, NIDAQMX_AVAILABLE

if NIDAQMX_AVAILABLE:
    from nidaqmx.constants import ExcitationSource, StrainGageBridgeType, StrainUnits

logger = logging.getLogger(__name__)

# NI9235 excitation voltage per datasheet: constant 2.0 V ±1 %, ALWAYS
# internal, cannot be switched off and cannot be set to a different value
# (unlike, say, the NI9234's IEPE constant current, there is no software
# option here to disable the excitation). The nidaqmx library default for
# `voltage_excit_val` (2.5 V) is wrong for THIS module and is therefore
# explicitly overridden here. Source: NI-9235 Specifications
# (ni.com, 2022-07-11), "Excitation Characteristics" section.
NI9235_EXCITATION_VOLTS = 2.0

# Fixed value of the built-in quarter-bridge completion resistor - no
# external/other value selectable. The nidaqmx library default for
# `nominal_gage_resistance` (350.0 Ω, intended for other modules) is also
# wrong here. Source: NI-9235 Specifications, "Input
# Characteristics" section ("Quarter-bridge completion: 120 Ω").
NI9235_NOMINAL_GAGE_RESISTANCE_OHM = 120.0

# Physical input range of the NI9235 per datasheet: ±29.4 mV/V, which at
# a gage factor (k-factor) of 2.0 corresponds roughly to ±0.0625/-0.0555
# STRAIN (see datasheet: "+62.500 µε/-55.500 µε"). Analogous to
# `hardware/ni9210.py`, this range is NOT taken from `channel.min_range`/
# `max_range` (their dataclass default of -10.0/10.0 V is meaningless for
# STRAIN units and not editable in the channel table - the same pitfall
# that already led, in practice, to NI9234 channels being misconfigured
# with a ±10 V instead of ±5 V range), but is used as a fixed,
# conservative module constant. Verified against real hardware: an open
# (unconnected) channel still reads out the full physical datasheet range
# (+0.0625 STRAIN) at these values, so min_val/max_val do NOT limit the
# returned measurement values (the module has only a single, fixed
# physical range, no selectable gain stage like, say, the NI9234) - the
# conservative values here therefore serve purely for plausibility
# checking, not for signal limiting.
NI9235_MIN_STRAIN = -0.03
NI9235_MAX_STRAIN = 0.03


class NI9235(NIDAQDevice):
    """NI 9235: 8-channel strain gauge input, 120 Ω quarter-bridge.

    Supports EXCLUSIVELY `SignalType.STRAIN` - half-/full-bridge are not
    physically wired on this module (see `data.models.NI9235_BRIDGE_TYPES`,
    only QUARTER_BRIDGE_I/II). The excitation voltage
    (`NI9235_EXCITATION_VOLTS`) and the completion resistor
    (`NI9235_NOMINAL_GAGE_RESISTANCE_OHM`) are hard-wired hardware
    properties, not `Channel` fields - unlike, say,
    `sensitivity_mv_per_unit` on the NI9234, there is no physical choice
    here.
    """

    def __init__(self, device_info: DeviceInfo, channels: list[Channel]) -> None:
        for channel in channels:
            if channel.signal_type != SignalType.STRAIN:
                raise AcquisitionError(
                    f"NI9235 unterstützt nur SignalType.STRAIN, Kanal "
                    f"'{channel.display_name}' hat {channel.signal_type}."
                )
            if channel.strain_gage_factor is None:
                raise AcquisitionError(
                    f"Kanal '{channel.display_name}' benötigt strain_gage_factor "
                    f"(k-Faktor des Dehnungsmessstreifens) für den NI9235."
                )
            if channel.strain_bridge_type not in NI9235_BRIDGE_TYPES:
                raise AcquisitionError(
                    f"Kanal '{channel.display_name}' hat einen ungültigen "
                    f"strain_bridge_type '{channel.strain_bridge_type}' - das NI9235 "
                    f"unterstützt nur {NI9235_BRIDGE_TYPES}."
                )
        device_info.module_type = ModuleType.NI9235
        super().__init__(device_info, channels)

    def _add_channel_to_task(self, task, channel: Channel) -> None:
        ai_channel = task.ai_channels.add_ai_strain_gage_chan(
            physical_channel=channel.hardware_channel,
            name_to_assign_to_channel=channel.display_name,
            min_val=NI9235_MIN_STRAIN,
            max_val=NI9235_MAX_STRAIN,
            units=StrainUnits.STRAIN,
            strain_config=StrainGageBridgeType[channel.strain_bridge_type],
            voltage_excit_source=ExcitationSource.INTERNAL,
            voltage_excit_val=NI9235_EXCITATION_VOLTS,
            gage_factor=channel.strain_gage_factor,
            nominal_gage_resistance=NI9235_NOMINAL_GAGE_RESISTANCE_OHM,
            lead_wire_resistance=channel.lead_wire_resistance_ohm,
        )

        # Like the NI9234, the NI9235 has a delta-sigma ADC and therefore
        # the same fixed filter delay between the analog sampling instant
        # and the digitally read-out sample (see hardware/ni9234.py for
        # the detailed rationale). Verified against real hardware
        # (cDAQ-9185 + NI9235): DAQmx rejects this property exactly like
        # on the NI9234 (status -200452) - so the best-effort approach
        # kicks in as intended, the measurement keeps running regardless,
        # just without the time-offset correction.
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
