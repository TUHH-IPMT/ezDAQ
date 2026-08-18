"""
hardware/ni9235.py

Konkrete Implementierung für das NI 9235 Modul (8-Kanal
Dehnungsmessstreifen-Eingang, ausschließlich 120-Ω-Viertelbrücke,
simultane Abtastung).

Siehe `hardware/nidaq_device.py` für den gemeinsamen Task-Lebenszyklus
und den Hinweis zum Hardware-Testvorbehalt.
"""

from __future__ import annotations

import logging

from data.models import Channel, DeviceInfo, ModuleType, NI9235_BRIDGE_TYPES, SignalType
from hardware.base_device import AcquisitionError
from hardware.nidaq_device import DaqmxError, NIDAQDevice, NIDAQMX_AVAILABLE

if NIDAQMX_AVAILABLE:
    from nidaqmx.constants import ExcitationSource, StrainGageBridgeType, StrainUnits

logger = logging.getLogger(__name__)

# Erregerspannung des NI9235 laut Datenblatt: konstant 2,0 V ±1 %, IMMER
# intern, nicht abschaltbar und nicht auf einen anderen Wert einstellbar
# (anders als z. B. beim NI9234s IEPE-Konstantstrom gibt es hier keine
# Software-Option, die Speisung abzuschalten). Der nidaqmx-Library-Default
# für `voltage_excit_val` (2.5 V) ist für DIESES Modul falsch und wird
# deshalb hier explizit überschrieben. Quelle: NI-9235 Specifications
# (ni.com, 2022-07-11), Abschnitt "Excitation Characteristics".
NI9235_EXCITATION_VOLTS = 2.0

# Fester Wert des eingebauten Viertelbrücken-Ergänzungswiderstands - kein
# externer/anderer Wert wählbar. Der nidaqmx-Library-Default für
# `nominal_gage_resistance` (350.0 Ω, für andere Module gedacht) ist hier
# ebenfalls falsch. Quelle: NI-9235 Specifications, Abschnitt "Input
# Characteristics" ("Quarter-bridge completion: 120 Ω").
NI9235_NOMINAL_GAGE_RESISTANCE_OHM = 120.0

# Physikalischer Eingangsbereich des NI9235 laut Datenblatt: ±29,4 mV/V,
# was bei einem k-Faktor (Gage-Faktor) von 2,0 grob ±0,0625/-0,0555 STRAIN
# entspricht (siehe Datenblatt: "+62.500 µε/-55.500 µε"). Analog zu
# `hardware/ni9210.py` wird dieser Bereich NICHT aus `channel.min_range`/
# `max_range` übernommen (deren Dataclass-Default -10.0/10.0 V ist für
# STRAIN-Einheiten bedeutungslos und in der Kanaltabelle nicht editierbar -
# derselbe Fallstrick, der beim NI9234 bereits real zu falsch konfigurierten
# ±10-V- statt ±5-V-Kanälen geführt hat), sondern als feste, konservative
# Modulkonstante verwendet. An echter Hardware verifiziert: ein offener
# (nicht angeschlossener) Kanal liest bei diesen Werten trotzdem den
# vollen physischen Datenblatt-Bereich (+0,0625 STRAIN) aus, min_val/
# max_val beschränken die zurückgegebenen Messwerte also NICHT (das Modul
# hat nur einen einzigen, festen physischen Bereich, keine wählbare
# Verstärkungsstufe wie z. B. das NI9234) - die konservativen Werte hier
# dienen daher rein der Plausibilitätsprüfung, nicht der Signalbegrenzung.
NI9235_MIN_STRAIN = -0.03
NI9235_MAX_STRAIN = 0.03


class NI9235(NIDAQDevice):
    """NI 9235: 8-Kanal Dehnungsmessstreifen-Eingang, 120-Ω-Viertelbrücke.

    Unterstützt AUSSCHLIESSLICH `SignalType.STRAIN` - Halb-/Vollbrücke sind
    auf diesem Modul physisch nicht verdrahtet (siehe
    `data.models.NI9235_BRIDGE_TYPES`, nur QUARTER_BRIDGE_I/II). Die
    Erregerspannung (`NI9235_EXCITATION_VOLTS`) und der
    Ergänzungswiderstand (`NI9235_NOMINAL_GAGE_RESISTANCE_OHM`) sind fest
    verdrahtete Hardwareeigenschaften, keine `Channel`-Felder - anders als
    z. B. `sensitivity_mv_per_unit` beim NI9234 gibt es hier keine
    physische Wahlmöglichkeit.
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

        # Das NI9235 hat wie das NI9234 einen Delta-Sigma-ADC und damit
        # dieselbe feste Filterverzögerung zwischen analogem
        # Abtastzeitpunkt und digital ausgelesenem Sample (siehe
        # hardware/ni9234.py für die ausführliche Begründung). An echter
        # Hardware (cDAQ-9185 + NI9235) verifiziert: DAQmx lehnt diese
        # Property genau wie beim NI9234 ab (Status -200452) - der
        # Best-Effort-Ansatz greift also wie vorgesehen, die Messung läuft
        # trotzdem weiter, nur ohne die Zeitversatz-Korrektur.
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
