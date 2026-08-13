"""
data/models.py

Zentrale Datenmodelle der Anwendung.

Dieses Modul enthält reine Datenstrukturen (keine Hardware- oder GUI-Logik).
Alle anderen Schichten (hardware, core, gui, analysis) verwenden diese
Modelle als gemeinsame "Sprache", um Kanäle, Geräte und Messungen zu
beschreiben.

Design-Entscheidung:
    Die Modelle sind bewusst als `dataclasses` mit Type Hints umgesetzt.
    Das hält sie leichtgewichtig, JSON-serialisierbar (siehe data/metadata.py)
    und einfach erweiterbar, ohne dass GUI- oder Hardware-Code sie kennen muss.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class ModuleType(str, Enum):
    """Unterstützte NI-cDAQ-Modultypen.

    Wird als String-Enum umgesetzt, damit der Wert direkt und lesbar in
    JSON-Konfigurationen und Metadaten gespeichert werden kann.
    """

    NI9215 = "NI9215"
    NI9234 = "NI9234"
    NI9210 = "NI9210"
    NI9213 = "NI9213"


class SignalType(str, Enum):
    """Physikalischer Signaltyp eines Kanals.

    Wird u. a. von der Hardware-Schicht genutzt, um zu entscheiden, welche
    nidaqmx-Kanalfunktion (z. B. `ai_voltage_chan` vs. `ai_accel_chan`)
    für einen Kanal aufgerufen werden muss.
    """

    VOLTAGE = "voltage"
    IEPE_ACCELERATION = "iepe_acceleration"
    THERMOCOUPLE = "thermocouple"


# Von der Anwendung angebotene Thermoelement-Typen (NI9210/NI9213, siehe
# `hardware/ni9210.py`). Werte entsprechen direkt den Mitgliedsnamen von
# `nidaqmx.constants.ThermocoupleType` (z. B. `ThermocoupleType["K"]"),
# damit hier keine zusätzliche Übersetzungstabelle gepflegt werden muss.
# Die selteneren Typen A/C (Wolfram-Rhenium) sind bewusst nicht enthalten.
THERMOCOUPLE_TYPES = ["K", "J", "T", "E", "N", "R", "S", "B"]

# Praxisnahe Messbereiche je Thermoelement-Typ in °C (grobe Richtwerte
# gemäß IEC 60584 für den Regelmessbereich), verwendet als min_val/max_val
# für `add_ai_thrmcpl_chan` (siehe `hardware/ni9210.py`). Kein exaktes
# Kalibrierlabor-Datenblatt - für die unterstützte Anwendung (Temperatur-
# Überwachung, keine metrologische Präzisionsmessung) ausreichend.
THERMOCOUPLE_TEMPERATURE_RANGES_C: dict[str, tuple[float, float]] = {
    "K": (-200.0, 1372.0),
    "J": (-210.0, 1200.0),
    "T": (-200.0, 400.0),
    "E": (-200.0, 1000.0),
    "N": (-200.0, 1300.0),
    "R": (-50.0, 1768.0),
    "S": (-50.0, 1768.0),
    "B": (250.0, 1820.0),
}

# ADC-Timing-Modi, die den Kompromiss zwischen Geschwindigkeit und
# effektiver Auflösung steuern (manche Modi verbessern zusätzlich die
# Netzbrumm-Unterdrückung) - NUR beim NI9213 hardwareseitig verfügbar,
# NICHT beim NI9210 (dieses hat eine feste Abtastrate von 14 S/s ohne
# konfigurierbaren Timing-Modus). Werte entsprechen direkt den
# Mitgliedsnamen von `nidaqmx.constants.ADCTimingMode`, siehe
# `hardware/ni9213.py`. "CUSTOM" ist bewusst nicht enthalten, da es
# zusätzliche, hier nicht abgebildete Parameter erfordert.
ADC_TIMING_MODES = [
    "AUTOMATIC",
    "HIGH_RESOLUTION",
    "HIGH_SPEED",
    "BEST_50_HZ_REJECTION",
    "BEST_60_HZ_REJECTION",
]


class StorageFormat(str, Enum):
    """Von der Anwendung unterstützte Speicherformate für Messdaten."""

    PARQUET = "parquet"
    CSV = "csv"


@dataclass
class Channel:
    """Repräsentiert einen einzelnen Messkanal.

    Attributes:
        hardware_channel: Physischer Hardwarekanal, z. B. "cDAQ1Mod1/ai0".
        display_name: Frei wählbarer Anzeigename für GUI und Auswertung,
            z. B. "Kraft Zylinder 1".
        unit: Physikalische Einheit des skalierten Werts, z. B. "N", "m/s^2".
        scale: Skalierungsfaktor der linearen Transformation.
        offset: Offset der linearen Transformation.
        signal_type: Physikalischer Signaltyp (Spannung, IEPE-Beschleunigung, ...).
        module_type: Modul, an dem der Kanal hängt (NI9215, NI9234, ...).
        enabled: Ob der Kanal für die nächste Messung aktiv ist.
        min_range: Optionaler unterer Messbereich (z. B. -10.0 V bei NI9215).
        max_range: Optionaler oberer Messbereich (z. B. +10.0 V bei NI9215).
        sensitivity_mv_per_unit: Sensorempfindlichkeit in mV/Einheit,
            relevant für IEPE-Beschleunigungssensoren (NI9234).
        thermocouple_type: Thermoelement-Typ (z. B. "K", "J", "T", ...),
            relevant für Thermoelement-Kanäle (NI9210/NI9213), siehe
            `THERMOCOUPLE_TYPES`.
        cal_point1_measured / cal_point1_reference: Erster Referenzpunkt
            einer optionalen 2-Punkt-Kalibrierung (gemessener Rohwert vs.
            bekannter Sollwert, z. B. Eispunkt 0 °C bei einem
            Thermoelement) - `None`, solange nicht kalibriert. Werden
            zusammen mit `cal_point2_*` nur zur Nachvollziehbarkeit
            gespeichert; `scale`/`offset` bleiben die tatsächlich
            angewendeten Werte (siehe
            `gui/widgets/channel_table.py::TwoPointCalibrationDialog`).
        cal_point2_measured / cal_point2_reference: Zweiter Referenzpunkt
            der 2-Punkt-Kalibrierung, z. B. Siedepunkt 100 °C.
        adc_timing_mode: ADC-Timing-Modus (siehe `ADC_TIMING_MODES`), NUR
            beim NI9213 hardwareseitig verfügbar (NI9210 hat eine feste
            Abtastrate). Muss laut nidaqmx für alle Kanäle desselben
            physischen Moduls identisch sein - die Kanaltabelle überträgt
            eine Änderung deshalb automatisch auf alle Kanäle desselben
            Moduls, siehe `gui/widgets/channel_table.py`.
        plot_color: Individuelle Kurvenfarbe in der Live View (z. B.
            "#64b5f6"), `None` = Theme-Standardfarbe (siehe
            `gui/live_view.py::ChannelDisplayDialog`).
        plot_background: Individuelle Plot-Hintergrundfarbe, `None` =
            Theme-Standardhintergrund.
        plot_y_min: Unterer Y-Achsen-Anzeigebereich der Live View. Anders
            als `min_range`/`max_range` (Hardware-Messbereich) rein eine
            Darstellungseinstellung - `None` fällt auf `min_range` bzw.
            -10.0 zurück.
        plot_y_max: Oberer Y-Achsen-Anzeigebereich der Live View, `None`
            fällt auf `max_range` bzw. 10.0 zurück.
        plot_autoscale: Ob die Y-Achse bei Über-/Unterschreiten von
            `plot_y_min`/`plot_y_max` automatisch auf den tatsächlichen
            Wertebereich umschaltet - ist dies `False`, bleibt der feste
            Bereich immer aktiv.
        plot_visible: Ob der Kanal in der Live View als eigener Subplot
            angezeigt wird. Betrifft NUR die Anzeige, nicht die Erfassung/
            Speicherung - ein Kanal mit `plot_visible=False` wird
            weiterhin normal aufgezeichnet, taucht aber nicht im
            Live-View-Raster auf (siehe `gui/live_view.py::_rebuild_plots`).

    Die physikalische Umrechnung erfolgt gemäß:
        physikalischer_wert = rohwert * scale + offset
    """

    hardware_channel: str
    display_name: str
    unit: str = ""
    scale: float = 1.0
    offset: float = 0.0
    signal_type: SignalType = SignalType.VOLTAGE
    module_type: ModuleType = ModuleType.NI9215
    enabled: bool = True
    min_range: Optional[float] = -10.0
    max_range: Optional[float] = 10.0
    sensitivity_mv_per_unit: Optional[float] = None
    thermocouple_type: str = "K"
    cal_point1_measured: Optional[float] = None
    cal_point1_reference: Optional[float] = None
    cal_point2_measured: Optional[float] = None
    cal_point2_reference: Optional[float] = None
    adc_timing_mode: str = "AUTOMATIC"
    plot_color: Optional[str] = None
    plot_background: Optional[str] = None
    plot_y_min: Optional[float] = None
    plot_y_max: Optional[float] = None
    plot_autoscale: bool = True
    plot_visible: bool = True
    # Zeigt den Kanal (statt im Hauptraster der Live View) in einem
    # eigenen Fenster an (siehe `gui/live_view.py::ChannelPopoutWindow`) -
    # schliesst sich mit `plot_visible` nicht aus: ein Kanal ist entweder
    # gar nicht (plot_visible=False), im Hauptraster (plot_popout=False)
    # oder in seinem eigenen Fenster (plot_popout=True) sichtbar, nie an
    # zwei Stellen gleichzeitig.
    plot_popout: bool = False

    def to_physical(self, raw_value: float) -> float:
        """Wandelt einen Rohwert in den skalierten physikalischen Wert um."""
        return raw_value * self.scale + self.offset

    def to_dict(self) -> dict:
        """Serialisiert den Kanal in ein JSON-kompatibles Dictionary."""
        return {
            "hardware_channel": self.hardware_channel,
            "display_name": self.display_name,
            "unit": self.unit,
            "scale": self.scale,
            "offset": self.offset,
            "signal_type": self.signal_type.value,
            "module_type": self.module_type.value,
            "enabled": self.enabled,
            "min_range": self.min_range,
            "max_range": self.max_range,
            "sensitivity_mv_per_unit": self.sensitivity_mv_per_unit,
            "thermocouple_type": self.thermocouple_type,
            "cal_point1_measured": self.cal_point1_measured,
            "cal_point1_reference": self.cal_point1_reference,
            "cal_point2_measured": self.cal_point2_measured,
            "cal_point2_reference": self.cal_point2_reference,
            "adc_timing_mode": self.adc_timing_mode,
            "plot_color": self.plot_color,
            "plot_background": self.plot_background,
            "plot_y_min": self.plot_y_min,
            "plot_y_max": self.plot_y_max,
            "plot_autoscale": self.plot_autoscale,
            "plot_visible": self.plot_visible,
            "plot_popout": self.plot_popout,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Channel":
        """Erstellt einen Channel aus einem Dictionary (z. B. aus JSON)."""
        return cls(
            hardware_channel=data["hardware_channel"],
            display_name=data.get("display_name", data["hardware_channel"]),
            unit=data.get("unit", ""),
            scale=data.get("scale", 1.0),
            offset=data.get("offset", 0.0),
            signal_type=SignalType(data.get("signal_type", SignalType.VOLTAGE.value)),
            module_type=ModuleType(data.get("module_type", ModuleType.NI9215.value)),
            enabled=data.get("enabled", True),
            min_range=data.get("min_range", -10.0),
            max_range=data.get("max_range", 10.0),
            sensitivity_mv_per_unit=data.get("sensitivity_mv_per_unit"),
            thermocouple_type=data.get("thermocouple_type", "K"),
            cal_point1_measured=data.get("cal_point1_measured"),
            cal_point1_reference=data.get("cal_point1_reference"),
            cal_point2_measured=data.get("cal_point2_measured"),
            cal_point2_reference=data.get("cal_point2_reference"),
            adc_timing_mode=data.get("adc_timing_mode", "AUTOMATIC"),
            plot_color=data.get("plot_color"),
            plot_background=data.get("plot_background"),
            plot_y_min=data.get("plot_y_min"),
            plot_y_max=data.get("plot_y_max"),
            plot_autoscale=data.get("plot_autoscale", True),
            plot_visible=data.get("plot_visible", True),
            plot_popout=data.get("plot_popout", False),
        )


@dataclass
class DeviceInfo:
    """Beschreibt ein erkanntes physisches NI-cDAQ-Modul/Gerät.

    Attributes:
        device_name: Von nidaqmx vergebener Gerätename, z. B. "cDAQ1Mod1".
        product_type: Produktbezeichnung, z. B. "NI 9215".
        module_type: Zugeordneter ModuleType, falls vom System unterstützt.
        num_channels: Anzahl physisch verfügbarer Kanäle auf dem Modul.
    """

    device_name: str
    product_type: str
    module_type: Optional[ModuleType] = None
    num_channels: int = 0
    # Liste der physischen Kanalnamen, z. B. ["cDAQ1Mod1/ai0", ...]
    physical_channels: list[str] = field(default_factory=list)


@dataclass
class MeasurementConfig:
    """Konfiguration für eine einzelne Messung/Aufnahme.

    Attributes:
        name: Bezeichner der Messung, z. B. "measurement_001".
        sample_rate_hz: Abtastrate in Hz (gilt für alle Kanäle der Messung).
        channels: Liste der aktiven Kanäle für diese Messung.
        storage_format: Gewähltes Speicherformat (Parquet/CSV).
        samples_per_read: Blockgröße pro Lesevorgang vom DAQ-Gerät.
        ring_buffer_size: Kapazität des Ring Buffers in Samples pro Kanal.
    """

    name: str
    sample_rate_hz: float
    channels: list[Channel] = field(default_factory=list)
    storage_format: StorageFormat = StorageFormat.PARQUET
    samples_per_read: int = 1000
    ring_buffer_size: int = 100_000
    save_to_disk: bool = True

    def active_channels(self) -> list[Channel]:
        """Gibt nur die aktivierten Kanäle zurück."""
        return [ch for ch in self.channels if ch.enabled]

    def to_dict(self) -> dict:
        """Serialisiert die Konfiguration in ein JSON-kompatibles Dictionary."""
        return {
            "name": self.name,
            "sample_rate_hz": self.sample_rate_hz,
            "channels": [ch.to_dict() for ch in self.channels],
            "storage_format": self.storage_format.value,
            "samples_per_read": self.samples_per_read,
            "ring_buffer_size": self.ring_buffer_size,
            "save_to_disk": self.save_to_disk,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MeasurementConfig":
        """Erstellt eine MeasurementConfig aus einem Dictionary (z. B. aus JSON)."""
        return cls(
            name=data["name"],
            sample_rate_hz=data.get("sample_rate_hz", 1000.0),
            channels=[Channel.from_dict(ch) for ch in data.get("channels", [])],
            storage_format=StorageFormat(
                data.get("storage_format", StorageFormat.PARQUET.value)
            ),
            samples_per_read=data.get("samples_per_read", 1000),
            ring_buffer_size=data.get("ring_buffer_size", 100_000),
            save_to_disk=data.get("save_to_disk", True),
        )


@dataclass
class MeasurementSession:
    """Repräsentiert eine konkrete, laufende oder abgeschlossene Messung.

    Trennt bewusst die statische Konfiguration (`MeasurementConfig`) von den
    Laufzeit-/Ergebnisinformationen einer Aufnahme (Start-/Endzeit, Pfad).

    Attributes:
        config: Die verwendete Messkonfiguration.
        start_time: Zeitpunkt des Messstarts.
        end_time: Zeitpunkt des Messendes (None solange die Messung läuft).
        file_path: Pfad zur gespeicherten Messdatei, sobald vorhanden.
    """

    config: MeasurementConfig
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    file_path: Optional[str] = None

    @property
    def is_running(self) -> bool:
        """True, solange die Messung gestartet, aber nicht beendet ist."""
        return self.start_time is not None and self.end_time is None

    @property
    def duration_seconds(self) -> Optional[float]:
        """Dauer der Messung in Sekunden, falls Start- und Endzeit vorliegen."""
        if self.start_time is None:
            return None
        end = self.end_time or datetime.now()
        return (end - self.start_time).total_seconds()
