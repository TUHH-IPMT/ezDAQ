"""
data/sensor_models.py

Datenmodelle für den Sensor-Katalog (Sensor-Datenbank) - unabhängig von
der Messkonfiguration (siehe data/models.py::Channel).

Bewusst getrennt von der Messkonfiguration:
    Der Sensor-Katalog ist ein eigenständiger Datenbestand ("welche
    Sensoren mit welchen Kennwerten habe ich physisch vorliegen"), der
    unabhängig von einzelnen Messungen existiert und gepflegt wird (siehe
    config/sensor_database.py). Der Nutzer schlägt Werte im Sensor-
    Katalog manuell nach (siehe gui/sensor_database_dialog.py, per
    Schnellzugriff-Button aus `gui/widgets/channel_table.py::
    ChannelParameterDialog` erreichbar) und trägt sie per Copy&Paste
    selbst in die Kanaleinstellungen ein - es gibt KEINE automatische
    Übernahme und KEINE Referenz/ID auf einen Sensor-Katalog-Eintrag in
    der Kanalkonfiguration. Damit bleibt eine gespeicherte
    Messkonfiguration vollständig eigenständig, auch wenn der
    Sensor-Katalog später geändert oder der Eintrag gelöscht wird.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

from data.models import SignalType


@dataclass
class SensorRangeVariant:
    """Eine von ggf. mehreren Werte-Varianten einer Sensor-Achse für
    unterschiedliche Messbereiche.

    Manche Sensoren bieten für eine Achse mehrere umschaltbare
    Messbereiche mit jeweils eigener Sensitivität (z. B. ein
    Beschleunigungssensor mit ±50g/±500g) - andere haben nur einen
    einzigen, festen Messbereich. Deshalb hält `SensorChannelDefinition`
    eine LISTE von Varianten statt einzelner Werte, mit mindestens einem
    Eintrag. Hat eine Achse nur eine Variante, kann `label` leer bleiben.

    `label` beschreibt den Messbereich als freien Text/Zahl (z. B. "±50")
    - bewusst KEINE separaten numerischen Min/Max-Felder, das wäre für
    den eigentlichen Zweck (Wiedererkennen der richtigen Variante beim
    Nachschlagen) unnötige Komplexität. `unit` ist die zugehörige Einheit
    des Messbereichs (z. B. "g") - eigenes Feld, analog zu
    `sensitivity_unit` bei der Sensitivität.

    `sensitivity_unit` ist bewusst freier Text statt eines festen "mV/g"
    (wie es `data.models.Channel.sensitivity_mv_per_unit` für IEPE-Kanäle
    fest annimmt) - der Sensor-Katalog ist NICHT auf IEPE-Sensitivität
    beschränkt, andere Sensortypen haben andere Sensitivitäts-Einheiten
    (z. B. "pC/N" bei ladungsbasierten Kraftsensoren).
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
    """Eine Achse/ein Messkanal eines Sensors (z. B. "X" bei einem
    triaxialen Beschleunigungssensor), mit mindestens einer Werte-Variante
    (siehe `SensorRangeVariant`)."""

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
    """Ein Sensor im Katalog, mit beliebig vielen Achsen/Messkanälen.

    `id` ist bewusst von `name` entkoppelt (stabile UUID) - ein
    Umbenennen des Sensors darf keine bereits bestehenden Referenzen
    brechen (auch wenn aktuell nichts dauerhaft auf `id` verweist, siehe
    Moduldoc oben).

    `category` ist bewusst freier Text statt einer festen Auswahlliste
    (z. B. "Beschleunigungsmessung", "Kraftmessung", "Temperaturmessung")
    - dient nur der Gruppierung in der Sensor-Liste (siehe
    `gui/sensor_database_dialog.py`), damit die Anwendung nicht selbst
    festlegen muss, welche Kategorien es überhaupt gibt.
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
