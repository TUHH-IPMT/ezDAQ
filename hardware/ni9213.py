"""
hardware/ni9213.py

Konkrete Implementierung für das NI 9213 Modul (16-Kanal
Thermoelement-Eingang, ±78 mV, mit eingebauter
Kaltstellenkompensation/CJC).

Erbt die komplette Kanalerzeugung von `hardware/ni9210.py::NI9210` - beide
Module nutzen zur Kanalerzeugung identisch `add_ai_thrmcpl_chan` und
unterscheiden sich softwareseitig nur durch Modultyp/Kanalzahl (die
Kanalzahl ergibt sich bereits automatisch aus den von der Hardware
gemeldeten physischen Kanälen, siehe `hardware/nidaq_device.py::discover_devices`).
"""

from __future__ import annotations

from data.models import ModuleType
from hardware.ni9210 import NI9210


class NI9213(NI9210):
    """NI 9213: 16-Kanal Thermoelement-Eingang (J/K/T/E/N/R/S/B), ±78 mV.

    Siehe `NI9210` für Details zur Kanalerzeugung/Kaltstellenkompensation -
    hier ausschließlich Modultyp und Fehlermeldungstext angepasst.
    """

    _MODULE_TYPE = ModuleType.NI9213
    _MODULE_LABEL = "NI9213"
