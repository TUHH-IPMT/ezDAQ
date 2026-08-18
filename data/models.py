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
    NI9235 = "NI9235"


NI9210_FIXED_SAMPLE_RATE_HZ = 14.0

# Das NI9234 hat (anders als der SAR-ADC des NI9215) einen Delta-Sigma-ADC:
# die Abtastrate ist nicht frei wählbar, sondern nur als ganzzahliger Teiler
# des internen Master-Timebase (13,1072 MHz, intern bereits durch 256
# geteilt) möglich: fs = 51.200 Hz / n, n ganzzahlig 1..31 (51.200 S/s bis
# ca. 1.651,6 S/s). Quelle: NI 9234 Operating Instructions and
# Specifications, Abschnitt "Understanding NI 9234 Data Rates". Der
# NI-DAQmx-Treiber nimmt zwar auch abweichende Werte entgegen (und rundet
# intern auf die nächste gültige Rate), ohne hier vorab zu validieren würde
# die App aber weiterhin mit der ungerundeten, tatsächlich nicht
# gemessenen Rate rechnen (Metadaten, Zeitachse, FFT in der Analyse-Ansicht).
NI9234_BASE_SAMPLE_RATE_HZ = 51_200.0
NI9234_MIN_RATE_DIVISOR = 1
NI9234_MAX_RATE_DIVISOR = 31

# Das NI9235 (120-Ω-Viertelbrücke-DMS-Modul) hat ebenfalls einen
# Delta-Sigma-ADC mit demselben Rastermuster, aber einem ANDEREN Master-
# Timebase (12,8 MHz statt 13,1072 MHz): fs = (12.8 MHz / 256) / n =
# 50.000 Hz / n. Am internen Timebase ist n auf 5..63 begrenzt (10.000 S/s
# bis ca. 793,65 S/s) - anders als beim NI9234 NICHT bei n=1, da die
# Hardware oberhalb von 10 kS/s keine gültige Rate mehr liefert. Quelle:
# NI-9235 Specifications (ni.com, 2022-07-11), Abschnitt "Data Rates" /
# "Data rate range (fs) using internal master timebase".
NI9235_BASE_SAMPLE_RATE_HZ = 50_000.0
NI9235_MIN_RATE_DIVISOR = 5
NI9235_MAX_RATE_DIVISOR = 63


@dataclass(frozen=True)
class GridRateSpec:
    """Beschreibt das Abtastraten-Raster eines Delta-Sigma-Moduls (fs =
    `base_hz` / n, n ganzzahlig `min_divisor`..`max_divisor`).

    Ermöglicht es, dieselbe Rasterprüf-/Rundungslogik für mehrere Module
    mit strukturell gleichem, aber zahlenmäßig unterschiedlichem Raster
    (aktuell NI9234 und NI9235) zu teilen, siehe
    `_GRID_SAMPLE_RATE_SPEC_BY_MODULE`.
    """

    module_label: str
    base_hz: float
    min_divisor: int
    max_divisor: int


# Module mit einem Delta-Sigma-Raster (fs = base_hz / n), das sich NICHT
# frei auf eine beliebige Zielrate einstellen lässt, sondern nur snappen.
# Ein künftiges weiteres Rastermodul braucht nur einen Eintrag hier - siehe
# `resolve_rate_groups()`, das generisch über alle vorhandenen Einträge
# iteriert (nicht mehr NI9234-spezifisch).
_GRID_SAMPLE_RATE_SPEC_BY_MODULE: dict[ModuleType, GridRateSpec] = {
    ModuleType.NI9234: GridRateSpec("NI9234", NI9234_BASE_SAMPLE_RATE_HZ, NI9234_MIN_RATE_DIVISOR, NI9234_MAX_RATE_DIVISOR),
    ModuleType.NI9235: GridRateSpec("NI9235", NI9235_BASE_SAMPLE_RATE_HZ, NI9235_MIN_RATE_DIVISOR, NI9235_MAX_RATE_DIVISOR),
}


def _grid_valid_sample_rates(spec: GridRateSpec) -> list[float]:
    """Alle gültigen Abtastraten eines Rastermoduls (absteigend sortiert)."""
    return [spec.base_hz / n for n in range(spec.min_divisor, spec.max_divisor + 1)]


def _nearest_grid_sample_rate(sample_rate_hz: float, spec: GridRateSpec) -> float:
    """Die gültige Rasterrate von `spec`, die `sample_rate_hz` am nächsten liegt.

    Ausschließlich für die Rasterprüfung in `_is_valid_grid_sample_rate`
    gedacht (symmetrische Toleranz um einen gültigen Wert). Für den
    Vorschlag in Fehlermeldungen wird bewusst NICHT diese Funktion,
    sondern `_next_grid_sample_rate_at_or_above` verwendet - siehe dort.
    """
    return min(_grid_valid_sample_rates(spec), key=lambda rate: abs(rate - sample_rate_hz))


def _next_grid_sample_rate_at_or_above(sample_rate_hz: float, spec: GridRateSpec) -> float:
    """Die kleinste gültige Rasterrate von `spec`, die `sample_rate_hz`
    nicht unterschreitet - also aufrunden statt auf den nächstgelegenen Wert.

    Begründung: Eine zu hohe Abtastrate kostet nur Speicherplatz, eine zu
    niedrige verliert dagegen unwiederbringlich Signalanteile (das
    Antialiasing-Filter des Moduls zieht mit der Rate mit, ein zu niedrig
    gewähltes Raster schneidet also echte Frequenzanteile weg). Bei
    Vibrations-/Dehnungsmessungen ist die Bandbreite meist die eigentliche
    Anforderung - deshalb im Zweifel nach oben.

    Wird NUR als Vorschlag in der Fehlermeldung verwendet, NICHT
    automatisch angewendet: der Nutzer muss die gültige Rate selbst
    eintragen, damit nie stillschweigend etwas anderes gemessen wird als
    eingestellt (genau der DIAdem-/NI-MAX-Fallstrick).

    Liegt die Anfrage über der höchsten unterstützten Rate, wird diese
    zurückgegeben - darüber geht hardwareseitig nichts.
    """
    candidates = [rate for rate in _grid_valid_sample_rates(spec) if rate >= sample_rate_hz]
    return min(candidates) if candidates else spec.base_hz / spec.min_divisor


def _is_valid_grid_sample_rate(sample_rate_hz: float, spec: GridRateSpec, tolerance_hz: float = 0.05) -> bool:
    """Prüft `sample_rate_hz` gegen das Raster von `spec` (fs = base_hz / n).

    `tolerance_hz` deckt die Rundung der GUI-Eingabe ab (Spinbox mit einer
    Nachkommastelle, siehe `gui/setup_view.py::_sample_rate_spin`) - viele
    gültige Raten (z. B. 51200/3 = 17066,666...) sind mit einer
    Nachkommastelle ohnehin nicht exakt darstellbar.
    """
    return abs(sample_rate_hz - _nearest_grid_sample_rate(sample_rate_hz, spec)) <= tolerance_hz


def grid_valid_sample_rates(module_type: ModuleType) -> list[float]:
    """Öffentliche, modultyp-generische Fassung von `_grid_valid_sample_rates`."""
    return _grid_valid_sample_rates(_GRID_SAMPLE_RATE_SPEC_BY_MODULE[module_type])


def nearest_grid_sample_rate(module_type: ModuleType, sample_rate_hz: float) -> float:
    """Öffentliche, modultyp-generische Fassung von `_nearest_grid_sample_rate`."""
    return _nearest_grid_sample_rate(sample_rate_hz, _GRID_SAMPLE_RATE_SPEC_BY_MODULE[module_type])


def next_grid_sample_rate_at_or_above(module_type: ModuleType, sample_rate_hz: float) -> float:
    """Öffentliche, modultyp-generische Fassung von `_next_grid_sample_rate_at_or_above`."""
    return _next_grid_sample_rate_at_or_above(sample_rate_hz, _GRID_SAMPLE_RATE_SPEC_BY_MODULE[module_type])


def is_valid_grid_sample_rate(module_type: ModuleType, sample_rate_hz: float, tolerance_hz: float = 0.05) -> bool:
    """Öffentliche, modultyp-generische Fassung von `_is_valid_grid_sample_rate`."""
    return _is_valid_grid_sample_rate(sample_rate_hz, _GRID_SAMPLE_RATE_SPEC_BY_MODULE[module_type], tolerance_hz)


def ni9234_valid_sample_rates() -> list[float]:
    """Alle 31 gültigen Abtastraten des NI9234 (absteigend sortiert).

    Dünner Wrapper um die generische Rasterlogik (siehe
    `_GRID_SAMPLE_RATE_SPEC_BY_MODULE`) - Verhalten unverändert gegenüber
    der Zeit, als diese Funktion NI9234-spezifisch implementiert war.
    """
    return grid_valid_sample_rates(ModuleType.NI9234)


def nearest_ni9234_sample_rate(sample_rate_hz: float) -> float:
    """Die gültige NI9234-Abtastrate, die `sample_rate_hz` am nächsten liegt.

    Ausschließlich für die Rasterprüfung in `is_valid_ni9234_sample_rate`
    gedacht (symmetrische Toleranz um einen gültigen Wert). Für den
    Vorschlag in Fehlermeldungen wird bewusst NICHT diese Funktion,
    sondern `next_ni9234_sample_rate_at_or_above` verwendet - siehe dort.
    """
    return nearest_grid_sample_rate(ModuleType.NI9234, sample_rate_hz)


def next_ni9234_sample_rate_at_or_above(sample_rate_hz: float) -> float:
    """Die kleinste gültige NI9234-Abtastrate, die `sample_rate_hz` nicht
    unterschreitet - also aufrunden statt auf den nächstgelegenen Wert.

    Wird NUR als Vorschlag in der Fehlermeldung verwendet, NICHT
    automatisch angewendet: der Nutzer muss die gültige Rate selbst
    eintragen, damit nie stillschweigend etwas anderes gemessen wird als
    eingestellt (genau der DIAdem-/NI-MAX-Fallstrick).
    """
    return next_grid_sample_rate_at_or_above(ModuleType.NI9234, sample_rate_hz)


def is_valid_ni9234_sample_rate(sample_rate_hz: float, tolerance_hz: float = 0.05) -> bool:
    """Prüft `sample_rate_hz` gegen das NI9234-Raster (fs = 51200 Hz / n)."""
    return is_valid_grid_sample_rate(ModuleType.NI9234, sample_rate_hz, tolerance_hz)


class SignalType(str, Enum):
    """Physikalischer Signaltyp eines Kanals.

    Wird u. a. von der Hardware-Schicht genutzt, um zu entscheiden, welche
    nidaqmx-Kanalfunktion (z. B. `ai_voltage_chan` vs. `ai_accel_chan`)
    für einen Kanal aufgerufen werden muss.
    """

    VOLTAGE = "voltage"
    IEPE_ACCELERATION = "iepe_acceleration"
    THERMOCOUPLE = "thermocouple"
    STRAIN = "strain"


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
# effektiver Auflösung steuern - NUR beim NI9213 hardwareseitig verfügbar,
# NICHT beim NI9210 (dieses hat eine feste Abtastrate von 14 S/s ohne
# konfigurierbaren Timing-Modus). Werte entsprechen direkt den
# Mitgliedsnamen von `nidaqmx.constants.ADCTimingMode`, siehe
# `hardware/ni9213.py`. Der volle DAQmx-Treiber kennt zusätzlich
# "AUTOMATIC", "BEST_50_HZ_REJECTION", "BEST_60_HZ_REJECTION" und "CUSTOM"
# - bewusst nicht angeboten, da weder NI-MAX noch DIAdem diese in ihrer
# Bedienoberfläche zur Auswahl stellen (nur HIGH_RESOLUTION/HIGH_SPEED)
# und für die drei erstgenannten auch keine verifizierten Wandlungszeiten
# auffindbar waren (siehe Git-Historie/doc/offene_punkte.md).
ADC_TIMING_MODES = [
    "HIGH_RESOLUTION",
    "HIGH_SPEED",
]

# Vom NI9235 hardwareseitig unterstützte Brückenvarianten - AUSSCHLIESSLICH
# Viertelbrücke (siehe hardware/ni9235.py), Halb-/Vollbrücke sind auf diesem
# Modul physisch nicht verdrahtet. Werte entsprechen direkt den
# Mitgliedsnamen von `nidaqmx.constants.StrainGageBridgeType`.
#   QUARTER_BRIDGE_I:  ein aktiver Messgitter (Standardfall).
#   QUARTER_BRIDGE_II: ein aktives Messgitter + ein Dummy-Gitter.
NI9235_BRIDGE_TYPES = ["QUARTER_BRIDGE_I", "QUARTER_BRIDGE_II"]

# Wandlungszeit pro Kanal je ADC-Timing-Modus (Sekunden) - der NI9213-ADC
# ist zwischen den Kanälen EINES physischen Moduls multiplext, die
# maximal erreichbare Abtastrate ergibt sich laut NI-9213-Datenblatt aus
# fs_max = min(1 / (Wandlungszeit * aktive Kanalzahl), 100 S/s) - "if you
# are using fewer than all channels, the sample rate might be faster".
# Beide Werte sind über mehrere unabhängige NI-Community-Zitate aus dem
# Datenblatt bestätigt.
NI9213_CONVERSION_TIME_S: dict[str, float] = {
    "HIGH_RESOLUTION": 0.055,
    "HIGH_SPEED": 0.00074,
}
NI9213_MAX_SAMPLE_RATE_HZ = 100.0


def ni9213_device_groups(channels: list["Channel"]) -> dict[str, list["Channel"]]:
    """Gruppiert die aktiven NI9213-Kanäle nach physischem Gerät (z. B.
    "cDAQ1Mod3"), da der ADC pro Modul (nicht pro Messkonfiguration)
    multiplext ist - zwei separate NI9213-Module teilen sich keine
    gemeinsame Wandlerbandbreite.

    Eigenständige, bewusst einfache String-Gruppierung statt eines
    Imports aus `core/measurement.py::group_channels_by_device` -
    `data/models.py` hängt bewusst nicht von `core/` ab (siehe
    Moduldocstring oben).
    """
    groups: dict[str, list[Channel]] = {}
    for channel in channels:
        if not (channel.enabled and channel.module_type == ModuleType.NI9213):
            continue
        device_name = channel.hardware_channel.split("/", 1)[0]
        groups.setdefault(device_name, []).append(channel)
    return groups


def max_ni9213_sample_rate_hz(channels_on_device: list["Channel"]) -> float:
    """Maximal erreichbare Abtastrate für EIN physisches NI9213-Modul.

    `channels_on_device`: nur die aktiven Kanäle dieses einen Geräts
    (siehe `ni9213_device_groups`). Nimmt bei uneinheitlichem
    `adc_timing_mode` innerhalb der Gruppe (sollte über die GUI nicht
    vorkommen, siehe `hardware/ni9213.py`, aber z. B. bei einer von Hand
    bearbeiteten Konfigurationsdatei möglich) defensiv den langsamsten
    beteiligten Modus an.
    """
    if not channels_on_device:
        return NI9213_MAX_SAMPLE_RATE_HZ
    conversion_time_s = max(
        NI9213_CONVERSION_TIME_S.get(ch.adc_timing_mode, NI9213_CONVERSION_TIME_S["HIGH_RESOLUTION"])
        for ch in channels_on_device
    )
    return min(NI9213_MAX_SAMPLE_RATE_HZ, 1.0 / (conversion_time_s * len(channels_on_device)))


# Modultypen mit einer hardwareseitig FESTEN Abtastrate, die sich nicht an
# eine gemeinsame Zielrate anpassen lässt (aktuell nur der NI9210 mit
# 14 S/s). NI9234/NI9235 (Raster-Module, siehe
# `_GRID_SAMPLE_RATE_SPEC_BY_MODULE` weiter oben) und NI9213 (max.
# erreichbare Rate abhängig von Kanalzahl/Timing-Modus) fehlen hier
# bewusst: alle drei können EINE gemeinsame Zielrate erfüllen, solange
# diese auf ihrem jeweiligen Raster bzw. unterhalb ihres Maximums liegt -
# sie bleiben deshalb grundsätzlich in der gemeinsamen "Zielraten"-Gruppe
# (Ausnahme: zwei GLEICHZEITIG vorhandene Raster-Module mit für die
# Zielrate unterschiedlich gesnappten Raten, siehe `resolve_rate_groups()`).
# Ein künftiges Modul mit einer ähnlich starren EINZELNEN Rate muss nur
# hier ergänzt werden - `resolve_rate_groups()` bildet daraus automatisch
# eine eigene Gruppe, ohne dass die Gruppierungslogik selbst angepasst
# werden muss.
_FIXED_SAMPLE_RATE_HZ_BY_MODULE: dict[ModuleType, float] = {
    ModuleType.NI9210: NI9210_FIXED_SAMPLE_RATE_HZ,
}

# Toleranz für den Vergleich "feste Modul-Rate == Zielrate" - deckt
# Rundung der GUI-Eingabe ab (siehe `is_valid_ni9234_sample_rate` für
# dieselbe Toleranz an anderer Stelle).
_FIXED_RATE_TOLERANCE_HZ = 0.05


@dataclass
class RateGroup:
    """Eine Menge aktiver Kanäle, die hardwareseitig dieselbe Abtastrate
    teilen können und daher (der bevorzugte Fall) in einem einzigen
    nidaqmx-Task mit echter Sample-Clock-Synchronität laufen.

    Mehrere `RateGroup`s in einer Messung entstehen NUR, wenn (a) ein Modul
    eine mit den übrigen Kanälen unvereinbare, hardwareseitig fixe Rate hat
    (siehe `_FIXED_SAMPLE_RATE_HZ_BY_MODULE`, aktuell: NI9210, feste
    14 S/s), oder (b) zwei gleichzeitig vorhandene Raster-Module (siehe
    `_GRID_SAMPLE_RATE_SPEC_BY_MODULE`, aktuell NI9234/NI9235) für die
    Zielrate auf unterschiedliche Raten snappen - das ist die Ausnahme,
    nicht der Regelfall. Siehe `resolve_rate_groups`.
    """

    channels: list["Channel"]
    resolved_sample_rate_hz: float
    reason: str


def resolve_rate_groups(
    channels: list["Channel"], target_sample_rate_hz: float
) -> list[RateGroup]:
    """Teilt aktive Kanäle in Gruppen mit gemeinsam nutzbarer Abtastrate auf.

    Ein Kanal von einem Modul aus `_FIXED_SAMPLE_RATE_HZ_BY_MODULE`
    (feste, nicht an eine Zielrate ANPASSBARE Rate) bekommt NUR dann
    eine eigene Gruppe, wenn seine feste Rate von `target_sample_rate_hz`
    abweicht - entspricht die Zielrate zufällig genau der festen Rate
    (z. B. ein NI9210 bei einer Zielrate von exakt 14 S/s), gibt es
    keinen Konflikt und der Kanal bleibt in der gemeinsamen
    "Zielraten"-Gruppe. Mehrere tatsächlich abweichende feste Raten
    werden NACH IHRER JEWEILIGEN RATE gruppiert (nicht nach Modultyp) -
    zwei unterschiedliche Module mit zufällig derselben festen Rate
    landen so in derselben Gruppe. Alle übrigen (an eine Zielrate
    anpassbaren) Module bleiben IMMER in der gemeinsamen
    "Zielraten"-Gruppe (der bevorzugte, echt synchronisierte Fall) -
    diese Gruppe wird niemals aus Bequemlichkeit aufgeteilt.

    Args:
        channels: Aktive Kanäle (z. B. `MeasurementConfig.active_channels()`).
        target_sample_rate_hz: Vom Nutzer eingestellte Zielrate.

    Returns:
        Eine Gruppe pro tatsächlich vorkommender Rate (Zielraten-Gruppe
        zuerst, falls vorhanden, danach die Gruppen fester Rate in der
        Reihenfolge ihres ersten Auftretens in `channels`) - diese
        Reihenfolge bestimmt später die Kanalreihenfolge im Ring Buffer
        (siehe `core/controller.py::start_measurement`).

    Raises:
        ValueError: falls die Zielrate für ein Modul OHNE feste Rate
            (NI9234-Raster, NI9213-Maximalrate) intrinsisch unerreichbar
            ist - das ist unabhängig davon, welche anderen Module in der
            Messung sind, also KEIN "Teilen"-Problem, sondern eine echte
            Fehlkonfiguration.
    """
    fixed_channels: list[Channel] = []
    adaptive_channels: list[Channel] = []
    for ch in channels:
        fixed_rate = _FIXED_SAMPLE_RATE_HZ_BY_MODULE.get(ch.module_type)
        if fixed_rate is not None and abs(target_sample_rate_hz - fixed_rate) > _FIXED_RATE_TOLERANCE_HZ:
            fixed_channels.append(ch)
        else:
            # Kein Konflikt: entweder kein Modul mit fester Rate, oder
            # die feste Rate entspricht bereits der Zielrate - der Kanal
            # kann im gemeinsamen Task bleiben (siehe Docstring oben).
            adaptive_channels.append(ch)

    groups: list[RateGroup] = []

    if adaptive_channels:
        # Alle Raster-Modultypen (siehe `_GRID_SAMPLE_RATE_SPEC_BY_MODULE`),
        # die unter den adaptiven Kanälen tatsächlich vorkommen - sortiert,
        # damit die Gruppen-Reihenfolge bei gleicher Eingabe deterministisch
        # bleibt. Jeder wird GEGEN SEIN EIGENES Raster geprüft/gesnappt,
        # nicht mehr nur das NI9234 (siehe Docstring oben, Fall (b)).
        grid_module_types = sorted(
            (
                {ch.module_type for ch in adaptive_channels}
                & _GRID_SAMPLE_RATE_SPEC_BY_MODULE.keys()
            ),
            key=lambda m: m.value,
        )
        resolved_by_module: dict[ModuleType, float] = {}
        if len(grid_module_types) == 1:
            # Regelfall: GENAU EIN Rastermodultyp vorhanden. Verhalten
            # unveraendert gegenueber vor dieser Generalisierung: eine
            # Zielrate, die nicht (innerhalb Toleranz) auf DIESEM Raster
            # liegt, ist ein echter Konfigurationsfehler - der Nutzer soll
            # bewusst einen gueltigen Wert eintragen statt dass die App
            # unbemerkt auf einen ganz anderen Wert ausweicht.
            module_type = grid_module_types[0]
            spec = _GRID_SAMPLE_RATE_SPEC_BY_MODULE[module_type]
            if not _is_valid_grid_sample_rate(target_sample_rate_hz, spec):
                suggestion = _next_grid_sample_rate_at_or_above(target_sample_rate_hz, spec)
                raise ValueError(
                    f"Das {spec.module_label} unterstützt nur Abtastraten nach der Formel "
                    f"{spec.base_hz:.0f} Hz / n (n = {spec.min_divisor}..{spec.max_divisor}); "
                    f"nächster gültiger Wert nach oben: {suggestion:.1f} S/s."
                )
            # Auf die EXAKTE gültige Rasterrate einrasten, NICHT die rohe
            # (z. B. auf eine Nachkommastelle gerundete) Zielrate
            # verwenden: DAQmx rundet einen Wert, der auch nur minimal
            # ÜBER einer gültigen Rate liegt, auf die NÄCHSTHÖHERE
            # gültige Rate auf (nicht auf die nächstgelegene) - z. B.
            # würde 17066,7 Hz (0,03 Hz über der exakt gültigen
            # 17066,67 Hz) intern auf 25600 Hz hochspringen, ohne dass
            # App/Metadaten/Live-View das bemerken. Hier bereits an der
            # Quelle auf den exakten Wert einrasten stellt sicher, dass
            # die überall angezeigte/gespeicherte Rate tatsächlich die
            # ist, die DAQmx auch wirklich konfiguriert - an echter
            # Hardware verifiziert (`task.timing.samp_clk_rate`).
            resolved_by_module[module_type] = _nearest_grid_sample_rate(target_sample_rate_hz, spec)
        else:
            # >=2 Rastermodultypen GLEICHZEITIG vorhanden (z. B. NI9234 +
            # NI9235): die beiden Raster überschneiden sich rechnerisch nie
            # (siehe Modul-Kommentar bei `NI9235_BASE_SAMPLE_RATE_HZ`) -
            # die rohe Zielrate kann also grundsätzlich nur für HÖCHSTENS
            # eines der beiden Raster "gültig" sein. Anders als im
            # Ein-Modul-Fall ist das hier KEIN Konfigurationsfehler,
            # sondern der Regelfall (exakt wie beim NI9210-Fixed-Rate-Fall:
            # jedes Modul bekommt einfach die für sein eigenes Raster
            # nächstgelegene Rate, ohne Fehlermeldung) - deshalb hier KEINE
            # `_is_valid_grid_sample_rate`-Gate, nur bedingungslos snappen.
            for module_type in grid_module_types:
                spec = _GRID_SAMPLE_RATE_SPEC_BY_MODULE[module_type]
                resolved_by_module[module_type] = _nearest_grid_sample_rate(target_sample_rate_hz, spec)

        for device_name, group_channels in ni9213_device_groups(adaptive_channels).items():
            max_rate = max_ni9213_sample_rate_hz(group_channels)
            if target_sample_rate_hz > max_rate + 0.05:
                raise ValueError(
                    f"Das NI9213 ({device_name}, {len(group_channels)} aktive(r) Kanal/"
                    f"Kanäle, Timing-Modus '{group_channels[0].adc_timing_mode}') "
                    f"unterstützt bei dieser Kanalzahl maximal {max_rate:.1f} S/s."
                )

        distinct_rates = set(resolved_by_module.values())
        if len(distinct_rates) <= 1:
            # Regelfall: 0 oder 1 Raster-Modultyp vorhanden -> EXAKT eine
            # gemeinsame "Zielrate"-Gruppe, wie schon immer.
            resolved_rate = next(iter(distinct_rates), target_sample_rate_hz)
            groups.append(
                RateGroup(
                    channels=adaptive_channels,
                    resolved_sample_rate_hz=resolved_rate,
                    reason="Zielrate",
                )
            )
        else:
            # >=2 Raster-Modultypen (z. B. NI9234 + NI9235) snappen für
            # DIESE Zielrate auf unterschiedliche Raten - ein gemeinsamer
            # Task würde eines der beiden Module stillschweigend falsch
            # takten. Aufteilung in separate Gruppen, analog zum
            # bestehenden Fixed-Rate-Split unten. Kanäle OHNE eigenes
            # Rasterlimit (z. B. NI9215) wandern in die Gruppe, deren
            # gesnappte Rate der rohen Zielrate am nächsten liegt - eine
            # bewusste, aber nicht hardwareseitig erzwungene Wahl (das
            # Modul toleriert jede Taktrate).
            per_module: dict[ModuleType, list[Channel]] = {}
            for ch in adaptive_channels:
                key = (
                    ch.module_type
                    if ch.module_type in resolved_by_module
                    else min(
                        grid_module_types,
                        key=lambda m: abs(resolved_by_module[m] - target_sample_rate_hz),
                    )
                )
                per_module.setdefault(key, []).append(ch)
            for module_type in grid_module_types:
                rate = resolved_by_module[module_type]
                group_channels = per_module.get(module_type, [])
                module_names = sorted({ch.module_type.value for ch in group_channels})
                groups.append(
                    RateGroup(
                        channels=group_channels,
                        resolved_sample_rate_hz=rate,
                        reason=f"{'/'.join(module_names)} (Raster {rate:.1f} S/s)",
                    )
                )

    fixed_groups: dict[float, list[Channel]] = {}
    for ch in fixed_channels:
        fixed_groups.setdefault(_FIXED_SAMPLE_RATE_HZ_BY_MODULE[ch.module_type], []).append(ch)

    for rate, group_channels in fixed_groups.items():
        module_names = sorted({ch.module_type.value for ch in group_channels})
        groups.append(
            RateGroup(
                channels=group_channels,
                resolved_sample_rate_hz=rate,
                reason=f"{'/'.join(module_names)} (feste {rate:.1f} S/s)",
            )
        )

    return groups


class StorageFormat(str, Enum):
    """Von der Anwendung unterstützte Speicherformate für Messdaten."""

    PARQUET = "parquet"
    CSV = "csv"


class RecordingStopUnit(str, Enum):
    """Einheit für das konfigurierte Aufnahme-Limit (siehe
    `MeasurementConfig.recording_stop_value`/`recording_unlimited`)."""

    SAMPLES = "samples"
    SECONDS = "seconds"
    MINUTES = "minutes"
    HOURS = "hours"


# Umrechnungsfaktor auf Sekunden je Zeiteinheit - SAMPLES bewusst nicht
# enthalten, da dafür Messwerte statt Sekunden verglichen werden (siehe
# `MeasurementConfig.is_recording_limit_reached`).
_RECORDING_STOP_UNIT_TO_SECONDS: dict[RecordingStopUnit, float] = {
    RecordingStopUnit.SECONDS: 1.0,
    RecordingStopUnit.MINUTES: 60.0,
    RecordingStopUnit.HOURS: 3600.0,
}


class TriggerKind(str, Enum):
    """Art einer einzelnen Trigger-Bedingung (siehe `TriggerCondition`).

    NONE (Standard) = keine automatische Bedingung (manuelles Verhalten).
    THRESHOLD/SERIAL lösen automatisch aus, sobald die jeweils
    konfigurierte Bedingung eintritt - der "Scharf"-Zustand (Hardware
    läuft bereits, wartet auf die Start-Bedingung) lebt in
    `gui/live_view.py::LiveView.enter_armed_state`.
    """

    NONE = "none"
    THRESHOLD = "threshold"
    SERIAL = "serial"


class TriggerDirection(str, Enum):
    """Vergleichsrichtung des Schwellwert-Triggers (siehe
    `TriggerCondition.threshold_direction`)."""

    RISES_ABOVE = "rises_above"
    FALLS_BELOW = "falls_below"
    ABS_EXCEEDS = "abs_exceeds"


@dataclass
class TriggerCondition:
    """Eine einzelne Trigger-Bedingung - wird sowohl für den Start als auch
    für das Stopp einer Messung verwendet (siehe `TriggerConfig.start`/
    `TriggerConfig.stop`), jeweils unabhängig konfigurierbar.

    Attributes:
        kind: Art der Bedingung.
        threshold_channel_hardware_id: Hardwarekanal (`Channel.hardware_channel`)
            des zu überwachenden Kanals - nur bei `kind=THRESHOLD` relevant.
        threshold_value: Schwellwert in der physikalischen Einheit des Kanals.
        threshold_direction: Vergleichsrichtung (siehe `TriggerDirection`).
        serial_port: Serielle Schnittstelle (z. B. "COM3") - nur bei
            `kind=SERIAL` relevant.
        serial_baud_rate: Baudrate der seriellen Verbindung.
        serial_expected_message: Exaktes Byte-/Text-Signal, dessen Empfang
            die Bedingung auslöst (kein beliebiges Byte) - siehe
            `gui/serial_trigger.py::SerialTriggerListener`.
    """

    kind: TriggerKind = TriggerKind.NONE
    threshold_channel_hardware_id: str = ""
    threshold_value: float = 0.0
    threshold_direction: TriggerDirection = TriggerDirection.RISES_ABOVE
    serial_port: str = ""
    serial_baud_rate: int = 9600
    serial_expected_message: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "threshold_channel_hardware_id": self.threshold_channel_hardware_id,
            "threshold_value": self.threshold_value,
            "threshold_direction": self.threshold_direction.value,
            "serial_port": self.serial_port,
            "serial_baud_rate": self.serial_baud_rate,
            "serial_expected_message": self.serial_expected_message,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TriggerCondition":
        return cls(
            kind=TriggerKind(data.get("kind", TriggerKind.NONE.value)),
            threshold_channel_hardware_id=data.get("threshold_channel_hardware_id", ""),
            threshold_value=data.get("threshold_value", 0.0),
            threshold_direction=TriggerDirection(
                data.get("threshold_direction", TriggerDirection.RISES_ABOVE.value)
            ),
            serial_port=data.get("serial_port", ""),
            serial_baud_rate=data.get("serial_baud_rate", 9600),
            serial_expected_message=data.get("serial_expected_message", ""),
        )


@dataclass
class TriggerConfig:
    """Konfiguration für automatischen Mess-Start UND/ODER -Stopp.

    Bewusst als eigenes, verschachteltes Dataclass statt flacher Felder
    auf `MeasurementConfig`.

    `start.kind == NONE` = manueller Start (Klick auf "Messung starten",
    bisheriges Standardverhalten). `stop.kind == NONE` = kein
    Trigger-Stopp - das bestehende, separate Aufnahme-Limit
    (`MeasurementConfig.recording_unlimited`/`recording_stop_value`/
    `recording_stop_unit`) sowie der manuelle Stopp-Button wirken davon
    UNABHÄNGIG weiter (wer zuerst eintrifft, stoppt die Messung - gleiche
    "oder"-Beziehung wie schon zwischen manuellem Stopp und Aufnahme-Limit).

    Attributes:
        start: Bedingung für den automatischen Start.
        stop: Bedingung für den automatischen Stopp.
        pretrigger_seconds: Wie viele Sekunden VOR dem Start-Trigger-
            Zeitpunkt zusätzlich rückwirkend aufgezeichnet werden sollen
            (wie ein Oszilloskop-Trigger) - nur bei `start.kind=THRESHOLD`
            relevant, siehe `core/ringbuffer.py::RingBuffer.register_reader`.
            Für den Stopp gibt es bewusst KEINEN Vorlauf - ein Stopp-Trigger
            beendet die Aufzeichnung einfach zum Zeitpunkt des Auslösens.
        auto_rearm: Ob nach JEDEM Stopp (manuell, per Trigger oder
            Aufnahme-Limit) automatisch eine neue Messung mit derselben
            Konfiguration gestartet wird, statt auf einen erneuten
            manuellen Klick auf "Messung starten" zu warten - macht
            `start`/`stop` erst zu einem echten, unbeaufsichtigt
            durchlaufenden Trigger-Zyklus (siehe
            `gui/main_window.py::_on_stop_measurement`). Nur relevant,
            wenn mindestens `start.kind` oder `stop.kind` != NONE ist.
    """

    start: TriggerCondition = field(default_factory=TriggerCondition)
    stop: TriggerCondition = field(default_factory=TriggerCondition)
    pretrigger_seconds: float = 5.0
    auto_rearm: bool = False

    def to_dict(self) -> dict:
        return {
            "start": self.start.to_dict(),
            "stop": self.stop.to_dict(),
            "pretrigger_seconds": self.pretrigger_seconds,
            "auto_rearm": self.auto_rearm,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TriggerConfig":
        return cls(
            start=TriggerCondition.from_dict(data.get("start", {}) or {}),
            stop=TriggerCondition.from_dict(data.get("stop", {}) or {}),
            pretrigger_seconds=data.get("pretrigger_seconds", 5.0),
            auto_rearm=data.get("auto_rearm", False),
        )


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
        strain_gage_factor: k-Faktor des angeschlossenen Dehnungsmessstreifens
            (typisch ~2.0), relevant für NI9235-Kanäle. `None` = nicht
            gesetzt (analog `sensitivity_mv_per_unit`).
        strain_bridge_type: Viertelbrücken-Variante (siehe
            `NI9235_BRIDGE_TYPES`) - hängt von der physischen Verkabelung
            ab (mit/ohne Dummy-Gitter), relevant für NI9235-Kanäle.
        lead_wire_resistance_ohm: Zuleitungswiderstand in Ω zur
            Kompensation der Leitungslänge (siehe NI9235 "Lead Wire
            Desensitization"), relevant für NI9235-Kanäle. 0.0 = keine
            Kompensation (Standard).
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
        plot_grid_color: Individuelle Gitterlinienfarbe, `None` =
            Theme-Standard (Vordergrundfarbe, siehe
            `gui/live_view.py::_channel_grid_color`).
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
    strain_gage_factor: Optional[float] = None
    strain_bridge_type: str = "QUARTER_BRIDGE_I"
    lead_wire_resistance_ohm: float = 0.0
    cal_point1_measured: Optional[float] = None
    cal_point1_reference: Optional[float] = None
    cal_point2_measured: Optional[float] = None
    cal_point2_reference: Optional[float] = None
    adc_timing_mode: str = "HIGH_RESOLUTION"
    plot_color: Optional[str] = None
    plot_background: Optional[str] = None
    plot_grid_color: Optional[str] = None
    plot_y_min: Optional[float] = None
    plot_y_max: Optional[float] = None
    plot_autoscale: bool = True
    plot_time_window_seconds: float = 5.0
    # Ob der eigentliche Kurvenverlauf angezeigt wird (Hauptraster UND
    # eigenes Fenster) - unabhaengig von `plot_show_value` unten: beide
    # zusammen abgeschaltet zeigt gar nichts (siehe `plot_visible` dafuer).
    # Standard ist NUR das Diagramm (siehe `plot_show_value`), NUR dieses
    # Feld aus zeigt ausschliesslich den Zahlenwert ohne Diagramm (siehe
    # `gui/live_view.py::ChannelDisplayDialog`/`_rebuild_plots`).
    plot_show_graph: bool = True
    # Grosse, aktuelle Messwertanzeige neben dem Subplot im Hauptraster
    # (siehe `gui/live_view.py::ChannelDisplayDialog`/`_rebuild_plots`) -
    # standardmaessig AUS: pro Kanal bewusst zuschaltbar statt bei vielen
    # Kanälen von vornherein unnötig Platz zu kosten.
    plot_show_value: bool = False
    # Anzahl Vorkommastellen fuer `plot_show_value` - passt ein Messwert
    # NICHT hinein, wird statt einer irrefuehrend abgeschnittenen Zahl ein
    # Rauten-Platzhalter angezeigt (wie in DIAdem/LabVIEW-Digitalanzeigen),
    # statt die Anzeigebreite laufend nachzuziehen. Im Dialog gemeinsam mit
    # `plot_value_decimal_digits` als EIN Format-Muster editierbar (z. B.
    # "000.0000"), siehe `gui/live_view.py::ChannelDisplayDialog`.
    plot_value_integer_digits: int = 3
    # Anzahl Nachkommastellen - siehe `plot_value_integer_digits`.
    plot_value_decimal_digits: int = 3
    plot_visible: bool = True
    # Zeigt den Kanal (statt im Hauptraster der Live View) in einem
    # eigenen Fenster an (siehe `gui/live_view.py::ChannelPopoutWindow`) -
    # schliesst sich mit `plot_visible` nicht aus: ein Kanal ist entweder
    # gar nicht (plot_visible=False), im Hauptraster (plot_popout=False)
    # oder in seinem eigenen Fenster (plot_popout=True) sichtbar, nie an
    # zwei Stellen gleichzeitig.
    plot_popout: bool = False
    # Zuletzt bekannte Position/Groesse des eigenen Fensters (siehe
    # `gui/live_view.py::ChannelPopoutWindow`) - wird kontinuierlich
    # aktualisiert, waehrend das Fenster offen ist, und beim naechsten
    # App-Start/Messstart wiederverwendet, damit die Anordnung erhalten
    # bleibt. `None` (alle vier), solange das Fenster noch nie
    # verschoben/in der Groesse geaendert wurde - dann gilt die
    # Standardposition/-groesse aus `ChannelPopoutWindow.__init__`. Wird
    # beim Wiederherstellen gegen die AKTUELL angeschlossenen Bildschirme
    # geprueft (siehe `gui/theme.py::is_position_on_screen`), damit ein
    # Fenster, das zuletzt auf einem inzwischen entfernten zweiten Monitor
    # stand, nicht unerreichbar wird.
    plot_popout_x: Optional[int] = None
    plot_popout_y: Optional[int] = None
    plot_popout_width: Optional[int] = None
    plot_popout_height: Optional[int] = None

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
            "strain_gage_factor": self.strain_gage_factor,
            "strain_bridge_type": self.strain_bridge_type,
            "lead_wire_resistance_ohm": self.lead_wire_resistance_ohm,
            "cal_point1_measured": self.cal_point1_measured,
            "cal_point1_reference": self.cal_point1_reference,
            "cal_point2_measured": self.cal_point2_measured,
            "cal_point2_reference": self.cal_point2_reference,
            "adc_timing_mode": self.adc_timing_mode,
            "plot_color": self.plot_color,
            "plot_background": self.plot_background,
            "plot_grid_color": self.plot_grid_color,
            "plot_y_min": self.plot_y_min,
            "plot_y_max": self.plot_y_max,
            "plot_autoscale": self.plot_autoscale,
            "plot_time_window_seconds": self.plot_time_window_seconds,
            "plot_show_graph": self.plot_show_graph,
            "plot_show_value": self.plot_show_value,
            "plot_value_integer_digits": self.plot_value_integer_digits,
            "plot_value_decimal_digits": self.plot_value_decimal_digits,
            "plot_visible": self.plot_visible,
            "plot_popout": self.plot_popout,
            "plot_popout_x": self.plot_popout_x,
            "plot_popout_y": self.plot_popout_y,
            "plot_popout_width": self.plot_popout_width,
            "plot_popout_height": self.plot_popout_height,
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
            strain_gage_factor=data.get("strain_gage_factor"),
            strain_bridge_type=data.get("strain_bridge_type", "QUARTER_BRIDGE_I"),
            lead_wire_resistance_ohm=data.get("lead_wire_resistance_ohm", 0.0),
            cal_point1_measured=data.get("cal_point1_measured"),
            cal_point1_reference=data.get("cal_point1_reference"),
            cal_point2_measured=data.get("cal_point2_measured"),
            cal_point2_reference=data.get("cal_point2_reference"),
            adc_timing_mode=data.get("adc_timing_mode", "HIGH_RESOLUTION"),
            plot_color=data.get("plot_color"),
            plot_background=data.get("plot_background"),
            plot_grid_color=data.get("plot_grid_color"),
            plot_y_min=data.get("plot_y_min"),
            plot_y_max=data.get("plot_y_max"),
            plot_autoscale=data.get("plot_autoscale", True),
            plot_time_window_seconds=max(
                0.1, float(data.get("plot_time_window_seconds", 5.0))
            ),
            plot_show_graph=data.get("plot_show_graph", True),
            plot_show_value=data.get("plot_show_value", False),
            plot_value_integer_digits=max(
                1, int(data.get("plot_value_integer_digits", 3))
            ),
            plot_value_decimal_digits=max(
                0, int(data.get("plot_value_decimal_digits", 3))
            ),
            plot_visible=data.get("plot_visible", True),
            plot_popout=data.get("plot_popout", False),
            plot_popout_x=cls._optional_int(data.get("plot_popout_x")),
            plot_popout_y=cls._optional_int(data.get("plot_popout_y")),
            plot_popout_width=cls._optional_int(data.get("plot_popout_width")),
            plot_popout_height=cls._optional_int(data.get("plot_popout_height")),
        )

    @staticmethod
    def _optional_int(value) -> Optional[int]:
        """Wandelt einen aus JSON geladenen Wert (kann `None`, `int` oder
        `float` sein) robust in `Optional[int]` um - siehe
        `plot_popout_x`/`plot_popout_y`/`plot_popout_width`/
        `plot_popout_height`."""
        return None if value is None else int(value)


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
        sample_rate_hz: Zielrate in Hz. Gilt direkt für alle Kanäle außer
            dem NI9210 (feste 14 S/s, siehe `resolve_rate_groups`).
        channels: Liste der aktiven Kanäle für diese Messung.
        storage_format: Gewähltes Speicherformat (Parquet/CSV).
        samples_per_read: Blockgröße pro Lesevorgang vom DAQ-Gerät.
        ring_buffer_size: Kapazität des Ring Buffers in Samples pro Kanal.
        recording_unlimited: True (Standard/bisheriges Verhalten) = die
            Messung läuft, bis der Nutzer manuell stoppt oder der
            Speicherplatz ausgeht. False = die Messung stoppt automatisch,
            sobald `recording_stop_value`/`recording_stop_unit` erreicht ist
            (siehe `is_recording_limit_reached`).
        recording_stop_value: Grenzwert in der Einheit `recording_stop_unit`
            - nur relevant, wenn `recording_unlimited` False ist.
        recording_stop_unit: Einheit des Grenzwerts (Messwerte oder Zeit).
        trigger: Konfiguration für automatischen Mess-Start UND/ODER
            -Stopp (siehe `TriggerConfig`) - das Aufnahme-Limit oben gilt
            unabhängig davon zusätzlich weiter (wer zuerst greift, stoppt).
    """

    name: str
    sample_rate_hz: float
    channels: list[Channel] = field(default_factory=list)
    storage_format: StorageFormat = StorageFormat.PARQUET
    samples_per_read: int = 1000
    ring_buffer_size: int = 100_000
    save_to_disk: bool = True
    recording_unlimited: bool = True
    recording_stop_value: float = 0.0
    recording_stop_unit: RecordingStopUnit = RecordingStopUnit.SAMPLES
    trigger: TriggerConfig = field(default_factory=TriggerConfig)

    def __post_init__(self) -> None:
        if not self.recording_unlimited and self.recording_stop_value <= 0:
            raise ValueError(
                "recording_stop_value muss bei begrenzten Messungen größer als 0 sein."
            )
        # Wirft ValueError NUR noch bei intrinsisch unerreichbaren Raten
        # (NI9234-Raster, NI9213-Maximalrate) - ein NI9210 zusammen mit
        # schnelleren Modulen ist KEIN Fehler mehr, sondern führt zu zwei
        # getrennten Abtast-Gruppen (siehe `resolve_rate_groups` und
        # `core/controller.py::start_measurement`).
        resolve_rate_groups(self.active_channels(), self.sample_rate_hz)

    def active_channels(self) -> list[Channel]:
        """Gibt nur die aktivierten Kanäle zurück."""
        return [ch for ch in self.channels if ch.enabled]

    def target_recording_stop_samples(self) -> int:
        """Rechnet das konfigurierte Limit (Messwerte oder Zeit) einmalig in
        eine Ziel-Samplezahl bezogen auf `sample_rate_hz` um.

        Samples sind die zuverlässigste Bezugsgröße für ein Aufnahme-Limit:
        sie werden vom Hardware-Sample-Clock des DAQ-Moduls getaktet, nicht
        softwareseitig per Wanduhrzeit (`datetime.now()`) - ein Grenzwert
        lässt sich damit unabhängig von GUI-/Thread-Verzögerungen zuverlässig
        auswerten (siehe `is_recording_limit_reached`).
        """
        if self.recording_stop_unit == RecordingStopUnit.SAMPLES:
            return int(round(self.recording_stop_value))
        seconds_per_unit = _RECORDING_STOP_UNIT_TO_SECONDS[self.recording_stop_unit]
        return int(round(self.recording_stop_value * seconds_per_unit * self.sample_rate_hz))

    def is_recording_limit_reached(self, samples_acquired: int) -> bool:
        """Prüft, ob das konfigurierte Aufnahme-Limit erreicht ist.

        Zentrale Stelle für die Grenzwert-Logik (Messwerte vs. Zeiteinheiten,
        siehe `target_recording_stop_samples`), damit `gui/live_view.py` nur
        noch die tatsächlich erfasste Samplezahl liefern muss. Gibt bei
        `recording_unlimited=True` immer False zurück (bisheriges
        Standardverhalten: laufen, bis manuell gestoppt oder die Festplatte
        voll ist).
        """
        if self.recording_unlimited:
            return False
        return samples_acquired >= self.target_recording_stop_samples()

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
            "recording_unlimited": self.recording_unlimited,
            "recording_stop_value": self.recording_stop_value,
            "recording_stop_unit": self.recording_stop_unit.value,
            "trigger": self.trigger.to_dict(),
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
            recording_unlimited=data.get("recording_unlimited", True),
            recording_stop_value=data.get("recording_stop_value", 0.0),
            recording_stop_unit=RecordingStopUnit(
                data.get("recording_stop_unit", RecordingStopUnit.SAMPLES.value)
            ),
            trigger=TriggerConfig.from_dict(data.get("trigger", {})),
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
