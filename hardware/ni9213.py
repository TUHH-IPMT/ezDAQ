"""
hardware/ni9213.py

Konkrete Implementierung für das NI 9213 Modul (16-Kanal
Thermoelement-Eingang, ±78 mV, mit eingebauter
Kaltstellenkompensation/CJC).

Erbt die Kanalerzeugung von `hardware/ni9210.py::NI9210` - beide Module
nutzen dafür identisch `add_ai_thrmcpl_chan` und unterscheiden sich
softwareseitig nur durch Modultyp/Kanalzahl (die Kanalzahl ergibt sich
bereits automatisch aus den von der Hardware gemeldeten physischen
Kanälen, siehe `hardware/nidaq_device.py::discover_devices`).

Eine echte Abweichung gibt es beim ADC-Timing-Modus: NUR das NI9213
unterstützt hardwareseitig einen konfigurierbaren Kompromiss zwischen
Geschwindigkeit und effektiver Auflösung (das NI9210 hat eine feste
Abtastrate von 14 S/s ohne diese Option) - daher hier zusätzlich zur
geerbten Kanalerzeugung gesetzt.
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
    """NI 9213: 16-Kanal Thermoelement-Eingang (J/K/T/E/N/R/S/B), ±78 mV.

    Siehe `NI9210` für Details zur Kanalerzeugung/Kaltstellenkompensation -
    zusätzlich wird hier `channel.adc_timing_mode` gesetzt (siehe
    `data/models.py::ADC_TIMING_MODES`).

    WICHTIG: nidaqmx verlangt denselben ADC-Timing-Modus für ALLE Kanäle
    desselben physischen Moduls ("You must use the same ADC timing mode
    for all channels on a device") - die Kanaltabelle
    (`gui/widgets/channel_table.py`) überträgt eine Änderung deshalb
    automatisch auf alle Kanäle desselben Moduls. Hier selbst wird das
    NICHT geprüft/erzwungen; bei widersprüchlichen Werten meldet der
    NI-DAQmx-Treiber einen Fehler beim Konfigurieren des Tasks.
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
