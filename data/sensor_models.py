"""
data/sensor_models.py

Data models for the sensor catalog (sensor database) - independent of
the measurement configuration (see data/models.py::Channel).

Deliberately separate from the measurement configuration:
    The sensor catalog is a standalone data set ("which sensors with
    which characteristic values do I physically have on hand") that
    exists and is maintained independently of individual measurements
    (see config/sensor_database.py). The user looks up values in the
    sensor catalog manually (see gui/sensor_database_dialog.py, reachable
    via the quick-access button from `gui/widgets/channel_table.py::
    ChannelParameterDialog`) and enters them into the channel settings
    themselves via copy & paste - there is NO automatic takeover and NO
    reference/ID to a sensor catalog entry in the channel configuration.
    This keeps a saved measurement configuration fully self-contained,
    even if the sensor catalog is later changed or the entry is deleted.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

from data.models import SignalType


@dataclass
class SensorRangeVariant:
    """One of possibly several value variants of a sensor axis for
    different measurement ranges.

    Some sensors offer several switchable measurement ranges for one
    axis, each with its own sensitivity (e.g. an accelerometer with
    ±50g/±500g) - others have only a single, fixed measurement range.
    That's why `SensorChannelDefinition` holds a LIST of variants instead
    of individual values, with at least one entry. If an axis has only
    one variant, `label` can stay empty.

    `label` describes the measurement range as free text/number (e.g.
    "±50") - deliberately NO separate numeric min/max fields, that would
    be unnecessary complexity for the actual purpose (recognizing the
    right variant when looking it up). `unit` is the corresponding unit
    of the measurement range (e.g. "g") - its own field, analogous to
    `sensitivity_unit` for the sensitivity.

    `sensitivity_unit` is deliberately free text instead of a fixed
    "mV/g" (as `data.models.Channel.sensitivity_mv_per_unit` fixedly
    assumes for IEPE channels) - the sensor catalog is NOT limited to
    IEPE sensitivity, other sensor types have other sensitivity units
    (e.g. "pC/N" for charge-based force sensors).
    """

    label: str = ""
    unit: str = ""
    sensitivity_value: Optional[float] = None
    sensitivity_unit: str = ""

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "unit": self.unit,
            "sensitivity_value": self.sensitivity_value,
            "sensitivity_unit": self.sensitivity_unit,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SensorRangeVariant":
        return cls(
            label=data.get("label", ""),
            unit=data.get("unit", ""),
            sensitivity_value=data.get("sensitivity_value"),
            sensitivity_unit=data.get("sensitivity_unit", ""),
        )


@dataclass
class SensorChannelDefinition:
    """One axis/measurement channel of a sensor (e.g. "X" for a triaxial
    accelerometer), with at least one value variant (see
    `SensorRangeVariant`)."""

    label: str = ""
    signal_type: SignalType = SignalType.IEPE_ACCELERATION
    ranges: list[SensorRangeVariant] = field(default_factory=lambda: [SensorRangeVariant()])

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "signal_type": self.signal_type.value,
            "ranges": [r.to_dict() for r in self.ranges],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SensorChannelDefinition":
        ranges = [SensorRangeVariant.from_dict(r) for r in data.get("ranges", [])]
        return cls(
            label=data.get("label", ""),
            signal_type=SignalType(data.get("signal_type", SignalType.IEPE_ACCELERATION.value)),
            ranges=ranges or [SensorRangeVariant()],
        )


@dataclass
class SensorEntry:
    """A sensor in the catalog, with any number of axes/measurement
    channels.

    `id` is deliberately decoupled from `name` (stable UUID) - renaming
    the sensor must not break any existing references (even though
    nothing currently references `id` persistently, see module docstring
    above).

    `category` is deliberately free text instead of a fixed selection
    list (e.g. "acceleration measurement", "force measurement",
    "temperature measurement") - it only serves to group entries in the
    sensor list (see `gui/sensor_database_dialog.py`), so the application
    doesn't have to define up front which categories even exist.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    category: str = ""
    manufacturer: str = ""
    serial_number: str = ""
    notes: str = ""
    channels: list[SensorChannelDefinition] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "manufacturer": self.manufacturer,
            "serial_number": self.serial_number,
            "notes": self.notes,
            "channels": [c.to_dict() for c in self.channels],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SensorEntry":
        return cls(
            id=data.get("id") or str(uuid.uuid4()),
            name=data.get("name", ""),
            category=data.get("category", ""),
            manufacturer=data.get("manufacturer", ""),
            serial_number=data.get("serial_number", ""),
            notes=data.get("notes", ""),
            channels=[SensorChannelDefinition.from_dict(c) for c in data.get("channels", [])],
        )
