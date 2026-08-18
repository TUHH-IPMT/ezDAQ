"""
core/measurement.py

Verbindet die Messkonfiguration (Datenmodelle aus `data/models.py`) mit
der Hardware-Schicht:

    * Gruppiert aktive Kanäle nach ihrem physischen Gerät/Modul
      (z. B. alle Kanäle von "cDAQ1Mod1" gehören zu einem NI9215).
    * Erzeugt daraus die passenden konkreten Hardware-Objekte
      (`NI9215`, `NI9234`, `NI9210`, `NI9213`, `NI9235`).
    * Wendet die lineare Kanal-Skalierung (`scale`, `offset`) auf
      Rohdaten-Blöcke an.

Dieses Modul enthält bewusst KEINE Thread- oder Task-Logik - das ist
Aufgabe von `core/acquisition.py` (Erfassung) und `hardware/*`
(Hardware-Kommunikation).
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

from data.models import Channel, DeviceInfo, ModuleType
from hardware.base_device import BaseDevice
from hardware.ni9210 import NI9210
from hardware.ni9213 import NI9213
from hardware.ni9215 import NI9215
from hardware.ni9234 import NI9234
from hardware.ni9235 import NI9235

logger = logging.getLogger(__name__)


class MeasurementConfigError(Exception):
    """Wird bei ungültiger oder inkonsistenter Messkonfiguration geworfen.

    Beispiele: gemischte Modultypen auf einem physischen Gerät,
    nicht unterstützter Modultyp, ungültiges Kanalformat.
    """


# Zuordnung Modultyp -> konkrete Hardware-Klasse. Neue Module werden hier
# eingetragen, ohne dass `create_devices()` selbst angepasst werden muss.
_DEVICE_CLASSES: dict[ModuleType, type[BaseDevice]] = {
    ModuleType.NI9215: NI9215,
    ModuleType.NI9234: NI9234,
    ModuleType.NI9210: NI9210,
    ModuleType.NI9213: NI9213,
    ModuleType.NI9235: NI9235,
}


def device_name_from_hw_channel(hw_channel: str) -> str:
    """Extrahiert den Gerätenamen aus einem Hardwarekanal.

    Gibt bei einem leeren oder unvollständigen Kanalnamen einen leeren
    String zurück; die strengere Konfigurationsprüfung bleibt in
    `_device_name_from_channel()` erhalten.
    """
    return hw_channel.split("/", 1)[0] if hw_channel else ""


def _device_name_from_channel(channel: Channel) -> str:
    """Extrahiert den Gerätenamen aus einem Hardwarekanal.

    Beispiel: "cDAQ1Mod1/ai0" -> "cDAQ1Mod1".

    Raises:
        MeasurementConfigError: falls `hardware_channel` nicht dem
            erwarteten Format "Gerät/Kanal" entspricht.
    """
    device_name = device_name_from_hw_channel(channel.hardware_channel)
    if not device_name or "/" not in channel.hardware_channel:
        raise MeasurementConfigError(
            f"Ungültiger hardware_channel '{channel.hardware_channel}' "
            f"(erwartetes Format 'Gerät/Kanal', z. B. 'cDAQ1Mod1/ai0')."
        )
    return device_name


def group_channels_by_device(channels: list[Channel]) -> dict[str, list[Channel]]:
    """Gruppiert Kanäle nach ihrem physischen Gerät/Modul.

    Die Reihenfolge der Kanäle innerhalb jeder Gruppe sowie die
    Reihenfolge, in der Geräte zum ersten Mal auftreten, bleibt erhalten.
    Diese Reihenfolge bestimmt später die Kanal-Reihenfolge im Ring
    Buffer (siehe `core/acquisition.py`) - sie MUSS deterministisch sein.
    """
    groups: dict[str, list[Channel]] = {}
    for channel in channels:
        device_name = _device_name_from_channel(channel)
        groups.setdefault(device_name, []).append(channel)
    return groups


def create_devices(
    channels: list[Channel],
    discovered_devices: Optional[list[DeviceInfo]] = None,
) -> list[BaseDevice]:
    """Erzeugt für jede Kanalgruppe (physisches Modul) das passende Hardware-Objekt.

    Args:
        channels: Aktive Kanäle der Messkonfiguration, typischerweise das
            Ergebnis von `MeasurementConfig.active_channels()`.
        discovered_devices: Optionales Ergebnis von
            `hardware.nidaq_device.discover_devices()`, um reale
            Produktbezeichnungen in `DeviceInfo.product_type` zu
            übernehmen. Ohne Angabe wird ein Platzhalter aus dem
            Modultyp der Kanäle erzeugt.

    Returns:
        Liste von `BaseDevice`-Instanzen. Die Reihenfolge der Kanäle über
        alle Geräte hinweg (Gerät für Gerät, siehe
        `group_channels_by_device`) bestimmt die Kanal-Reihenfolge des
        späteren Ring Buffers - das ist wichtig für die korrekte
        Kanalzuordnung in Live View/Storage.

    Raises:
        MeasurementConfigError: falls Kanäle desselben Geräts
            unterschiedliche Modultypen angeben, oder ein Modultyp nicht
            unterstützt wird.
    """
    discovered_by_name = {d.device_name: d for d in (discovered_devices or [])}
    groups = group_channels_by_device(channels)

    devices: list[BaseDevice] = []
    for device_name, group_channels in groups.items():
        module_types = {ch.module_type for ch in group_channels}
        if len(module_types) > 1:
            raise MeasurementConfigError(
                f"Gerät '{device_name}' hat Kanäle mit unterschiedlichen "
                f"Modultypen ({sorted(m.value for m in module_types)}) - "
                f"ein physisches Modul kann nur einen Typ haben."
            )
        module_type = next(iter(module_types))

        device_class = _DEVICE_CLASSES.get(module_type)
        if device_class is None:
            raise MeasurementConfigError(
                f"Modultyp {module_type} wird aktuell nicht unterstützt."
            )

        device_info = discovered_by_name.get(device_name) or DeviceInfo(
            device_name=device_name,
            product_type=module_type.value,
            module_type=module_type,
            num_channels=len(group_channels),
        )

        devices.append(device_class(device_info, group_channels))
        logger.debug(
            "Gerät erzeugt: %s (%s), %d Kanäle",
            device_name,
            module_type.value,
            len(group_channels),
        )

    return devices


def apply_scaling(raw_block: np.ndarray, channels: list[Channel]) -> np.ndarray:
    """Wendet die lineare Kanal-Skalierung auf einen Rohdaten-Block an.

    Berechnet für jede Zeile (Kanal) i:
        physikalischer_wert[i] = raw_block[i] * channels[i].scale + channels[i].offset

    Args:
        raw_block: Array der Form (num_channels, num_samples). Die
            Zeilen-Reihenfolge MUSS der Reihenfolge von `channels`
            entsprechen (siehe `create_devices`/`AcquisitionThread`).
        channels: Kanäle in derselben Reihenfolge wie die Zeilen von `raw_block`.

    Returns:
        Neues Array derselben Form mit physikalisch skalierten Werten.

    Raises:
        ValueError: falls die Anzahl Kanäle nicht zur Anzahl Zeilen passt.
    """
    if raw_block.shape[0] != len(channels):
        raise ValueError(
            f"raw_block hat {raw_block.shape[0]} Zeilen, aber es wurden "
            f"{len(channels)} Kanäle übergeben."
        )
    scales = np.array([ch.scale for ch in channels], dtype=np.float64).reshape(-1, 1)
    offsets = np.array([ch.offset for ch in channels], dtype=np.float64).reshape(-1, 1)
    return raw_block * scales + offsets
