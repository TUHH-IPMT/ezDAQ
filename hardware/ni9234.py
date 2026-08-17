"""
hardware/ni9234.py

Konkrete Implementierung für das NI 9234 Modul (4-Kanal IEPE-fähiger
dynamischer Signaleingang, typischerweise für Beschleunigungssensoren
und Mikrofone).

Siehe `hardware/nidaq_device.py` für den gemeinsamen Task-Lebenszyklus
und den Hinweis zum Hardware-Testvorbehalt.
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

# IEPE-Konstantstromspeisung des NI 9234 laut NI-Datenblatt/Betriebsanleitung:
# Minimum 2.0 mA, typisch 2.1 mA (software-schaltbar an/aus, nicht in der
# Höhe konfigurierbar). Quelle: NI 9234 Operating Instructions and
# Specifications, Abschnitt "IEPE excitation current".
NI9234_IEPE_EXCITATION_AMPS = 0.002

# Physikalischer Eingangsbereich des NI 9234 laut Datenblatt (±5 V).
NI9234_MIN_VOLTAGE = -5.0
NI9234_MAX_VOLTAGE = 5.0


class NI9234(NIDAQDevice):
    """NI 9234: 4-Kanal Eingang für Spannung oder IEPE-Beschleunigung/Mikrofone.

    Unterstützt zwei Betriebsarten pro Kanal:
        * `SignalType.IEPE_ACCELERATION`: interne IEPE-Konstantstromspeisung
          aktiv, der NI-DAQmx-Treiber rechnet über `sensitivity_mv_per_unit`
          (Sensorempfindlichkeit in mV/g) bereits hardwareseitig in g um -
          dieses Feld ist dafür zwingend erforderlich.
        * `SignalType.VOLTAGE`: normaler Spannungseingang ohne IEPE-Speisung,
          identisch zum NI9215-Verhalten (lineare Skalierung über
          `channel.scale`/`channel.offset`), nur mit dem kleineren
          ±5-V-Bereich des NI9234.
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

        # Das NI9234 hat als Delta-Sigma-ADC eine feste Filterverzögerung
        # zwischen analogem Abtastzeitpunkt und digital ausgelesenem Sample
        # (laut Datenblatt ca. 40 Sample-Clock-Perioden + 3,2 µs) - anders
        # als z. B. das NI9215 (SAR-ADC, praktisch keine Verzögerung).
        # NI-DAQmx kompensiert das bei C-Series-DSA-Modulen NICHT automatisch,
        # auch nicht in einem gemeinsamen Task mit anderen Modulen (Channel
        # Expansion) - ohne diese Property wären NI9234-Kanäle gegenüber
        # z. B. NI9215-Kanälen im selben Task zeitlich versetzt (~1
        # Sample-Periode bei niedrigen Abtastraten). Quelle: NI-Knowledgebase
        # "Synchronized Data Delayed When Using NI DAQ Devices with
        # Delta-Sigma-ADC".
        #
        # BEST EFFORT: gegen echte Hardware (cDAQ-9185 + NI9234, aktuelle
        # Treiberversion) lehnt DAQmx diese Property IMMER ab (-200452
        # "Specified property is not supported by the device or is not
        # applicable to the task") - reproduzierbar sowohl für Voltage- als
        # auch für IEPE-Accel-Kanäle und unabhängig davon, ob sie vor oder
        # nach der Sample-Clock-Timing-Konfiguration gesetzt wird. Offenbar
        # von dieser Treiber-/Firmware-Kombination schlicht nicht
        # unterstützt. Bricht die Messung deshalb NICHT ab, wenn das
        # Setzen fehlschlägt - nur die (kleine, bei niedrigen Abtastraten
        # spürbare) Zeitversatz-Korrektur entfällt dann.
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
