"""
core/measurement.py

Connects the measurement configuration (data models from
`data/models.py`) with the hardware layer:

    * Groups active channels by their physical device/module (e.g. all
      channels of "cDAQ1Mod1" belong to one NI9215).
    * Creates the matching concrete hardware objects from them
      (`NI9215`, `NI9234`, `NI9210`, `NI9213`, `NI9235`).
    * Applies linear channel scaling (`scale`, `offset`) to raw data
      blocks.

This module deliberately contains NO thread or task logic - that is the
responsibility of `core/acquisition.py` (acquisition) and `hardware/*`
(hardware communication).
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
    """Raised on an invalid or inconsistent measurement configuration.

    Examples: mixed module types on one physical device, unsupported
    module type, invalid channel format.
    """


# Mapping module type -> concrete hardware class. New modules are added
# here without having to modify `create_devices()` itself.
_DEVICE_CLASSES: dict[ModuleType, type[BaseDevice]] = {
    ModuleType.NI9215: NI9215,
    ModuleType.NI9234: NI9234,
    ModuleType.NI9210: NI9210,
    ModuleType.NI9213: NI9213,
    ModuleType.NI9235: NI9235,
}


def device_name_from_hw_channel(hw_channel: str) -> str:
    """Extracts the device name from a hardware channel.

    Returns an empty string for an empty or incomplete channel name; the
    stricter configuration check remains in `_device_name_from_channel()`.
    """
    return hw_channel.split("/", 1)[0] if hw_channel else ""


def _device_name_from_channel(channel: Channel) -> str:
    """Extracts the device name from a hardware channel.

    Example: "cDAQ1Mod1/ai0" -> "cDAQ1Mod1".

    Raises:
        MeasurementConfigError: if `hardware_channel` doesn't match the
            expected "device/channel" format.
    """
    device_name = device_name_from_hw_channel(channel.hardware_channel)
    if not device_name or "/" not in channel.hardware_channel:
        raise MeasurementConfigError(
            f"Ungültiger hardware_channel '{channel.hardware_channel}' "
            f"(erwartetes Format 'Gerät/Kanal', z. B. 'cDAQ1Mod1/ai0')."
        )
    return device_name


def group_channels_by_device(channels: list[Channel]) -> dict[str, list[Channel]]:
    """Groups channels by their physical device/module.

    The order of channels within each group, as well as the order in
    which devices first appear, is preserved. This order later
    determines the channel order in the ring buffer (see
    `core/acquisition.py`) - it MUST be deterministic.
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
    """Creates the matching hardware object for each channel group (physical module).

    Args:
        channels: Active channels of the measurement configuration,
            typically the result of `MeasurementConfig.active_channels()`.
        discovered_devices: Optional result of
            `hardware.nidaq_device.discover_devices()`, to adopt real
            product designations into `DeviceInfo.product_type`. Without
            it, a placeholder is generated from the channels' module type.

    Returns:
        List of `BaseDevice` instances. The channel order across all
        devices (device by device, see `group_channels_by_device`)
        determines the channel order of the resulting ring buffer -
        this matters for correct channel assignment in the live
        view/storage.

    Raises:
        MeasurementConfigError: if channels of the same device specify
            different module types, or a module type is not supported.
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
    """Applies linear channel scaling to a raw data block.

    Computes for each row (channel) i:
        physical_value[i] = raw_block[i] * channels[i].scale + channels[i].offset

    Args:
        raw_block: Array of shape (num_channels, num_samples). The row
            order MUST match the order of `channels` (see
            `create_devices`/`AcquisitionThread`).
        channels: Channels in the same order as the rows of `raw_block`.

    Returns:
        New array of the same shape with physically scaled values.

    Raises:
        ValueError: if the number of channels doesn't match the number
            of rows.
    """
    if raw_block.shape[0] != len(channels):
        raise ValueError(
            f"raw_block hat {raw_block.shape[0]} Zeilen, aber es wurden "
            f"{len(channels)} Kanäle übergeben."
        )
    scales = np.array([ch.scale for ch in channels], dtype=np.float64).reshape(-1, 1)
    offsets = np.array([ch.offset for ch in channels], dtype=np.float64).reshape(-1, 1)
    return raw_block * scales + offsets
