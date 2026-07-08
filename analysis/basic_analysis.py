"""
analysis/basic_analysis.py

Vorbereitete Architektur für zukünftige Analysefunktionen.

In Version 1 der Analyse-Ansicht (siehe `gui/analysis_view.py`) werden
gemäß Vorgabe AUSDRÜCKLICH NOCH KEINE Analysefunktionen implementiert
(kein FFT, keine Filter, kein RMS, keine Statistik, keine automatischen
Reports). Dieses Modul definiert lediglich die Funktionssignaturen und
die einheitliche Datenübergabe (`pandas.DataFrame` mit Spalte "time_s"
und einer Spalte je Kanal, siehe `data/loader.py::LoadedMeasurement`),
damit spätere Implementierungen sich nahtlos einfügen, ohne dass
`gui/analysis_view.py` strukturell angepasst werden muss.

Geplante Erweiterungen (nicht implementiert):
    * compute_fft(...): Frequenzspektrum eines Kanals.
    * apply_filter(...): Tief-/Hoch-/Bandpassfilter auf einen Kanal.
    * compute_rms(...): Effektivwert über ein Zeitfenster.
    * compute_statistics(...): Min/Max/Mittelwert/Standardabweichung.
    * generate_report(...): Automatisierter PDF-/HTML-Report je Messung.
"""

from __future__ import annotations

import pandas as pd


def compute_fft(data: pd.DataFrame, channel_name: str, sample_rate_hz: float):
    """Berechnet das Frequenzspektrum eines Kanals (NICHT IMPLEMENTIERT).

    Vorgesehene Signatur für eine spätere Implementierung, z. B. via
    `numpy.fft.rfft`. Absichtlich noch nicht umgesetzt (siehe Vorgabe,
    Analyse-Ansicht Version 1).
    """
    raise NotImplementedError(
        "FFT-Analyse ist für eine spätere Version vorgesehen, aber noch nicht implementiert."
    )


def apply_filter(
    data: pd.DataFrame, channel_name: str, sample_rate_hz: float, cutoff_hz: float, kind: str = "lowpass"
):
    """Wendet einen Filter auf einen Kanal an (NICHT IMPLEMENTIERT).

    Vorgesehen für z. B. `scipy.signal`-basierte Tief-/Hoch-/
    Bandpassfilter. Absichtlich noch nicht umgesetzt.
    """
    raise NotImplementedError(
        "Filterfunktionen sind für eine spätere Version vorgesehen, aber noch nicht implementiert."
    )


def compute_rms(data: pd.DataFrame, channel_name: str) -> float:
    """Berechnet den Effektivwert (RMS) eines Kanals (NICHT IMPLEMENTIERT)."""
    raise NotImplementedError(
        "RMS-Berechnung ist für eine spätere Version vorgesehen, aber noch nicht implementiert."
    )


def compute_statistics(data: pd.DataFrame, channel_name: str) -> dict:
    """Berechnet Basisstatistiken (Min/Max/Mittelwert/Std) eines Kanals
    (NICHT IMPLEMENTIERT)."""
    raise NotImplementedError(
        "Statistik-Berechnung ist für eine spätere Version vorgesehen, aber noch nicht implementiert."
    )


def generate_report(measurement_path: str, output_path: str) -> None:
    """Erzeugt einen automatisierten Report für eine Messung (NICHT IMPLEMENTIERT)."""
    raise NotImplementedError(
        "Automatische Reports sind für eine spätere Version vorgesehen, aber noch nicht implementiert."
    )
