"""
analysis/basic_analysis.py

Analysefunktionen für die Analyse-Ansicht (siehe `gui/analysis_view.py`).

Alle Funktionen erhalten ein `pandas.DataFrame` mit Spalte "time_s" und
einer Spalte je Kanal (siehe `data/loader.py::LoadedMeasurement`) und
geben Rohdaten (numpy-Arrays) zurück - das Verpacken als neuer
Ergebniskanal (inkl. `Channel`-Metadaten, x-Achse etc.) übernimmt
`gui/analysis_view.py`, damit dieses Modul unabhängig von der GUI bleibt
und einzeln testbar ist.

Aktuell implementiert:
    * compute_fft(...): Amplitudenspektrum eines Kanals (numpy.fft.rfft).
    * apply_filter(...): Butterworth-Tief-/Hochpass (scipy.signal).
    * apply_smoothing(...): Gleitender Mittelwert.

Noch nicht implementiert (spätere Version):
    * compute_rms(...), compute_statistics(...), generate_report(...).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def compute_fft(
    data: pd.DataFrame, channel_name: str, sample_rate_hz: float
) -> tuple[np.ndarray, np.ndarray]:
    """Berechnet das (einseitige) Amplitudenspektrum eines Kanals.

    Returns:
        Tupel `(frequenz_hz, amplitude)` gleicher Länge.
    """
    values = data[channel_name].to_numpy(dtype=float)
    n = len(values)
    if n < 2:
        raise ValueError("Für eine FFT werden mindestens 2 Messwerte benötigt.")

    values = values - np.mean(values)
    spectrum = np.fft.rfft(values)
    freq_hz = np.fft.rfftfreq(n, d=1.0 / sample_rate_hz)

    amplitude = np.abs(spectrum) / n * 2.0
    if len(amplitude) > 0:
        amplitude[0] /= 2.0  # DC-Anteil wird durch die *2-Normierung nicht verdoppelt

    return freq_hz, amplitude


def apply_filter(
    data: pd.DataFrame,
    channel_name: str,
    sample_rate_hz: float,
    cutoff_hz: float,
    kind: str = "lowpass",
) -> np.ndarray:
    """Wendet einen Butterworth-Tief-/Hochpassfilter auf einen Kanal an.

    Nullphasige Filterung via `scipy.signal.filtfilt`, damit das Ergebnis
    zeitlich nicht gegenüber dem Originalsignal verschoben ist.
    """
    from scipy.signal import butter, filtfilt

    if kind not in ("lowpass", "highpass"):
        raise ValueError(f"Unbekannte Filterart: '{kind}' (erwartet 'lowpass' oder 'highpass').")

    values = data[channel_name].to_numpy(dtype=float)
    nyquist_hz = sample_rate_hz / 2.0
    normalized_cutoff = min(max(cutoff_hz / nyquist_hz, 1e-6), 0.999)

    b, a = butter(4, normalized_cutoff, btype=kind)
    padlen = 3 * max(len(a), len(b))
    if len(values) <= padlen:
        raise ValueError("Zu wenige Messwerte für die gewählte Filterordnung.")

    return filtfilt(b, a, values)


def apply_smoothing(data: pd.DataFrame, channel_name: str, window_size: int) -> np.ndarray:
    """Glättet einen Kanal mittels gleitendem Mittelwert (zentriertes Fenster)."""
    if window_size < 2:
        raise ValueError("Die Fenstergröße muss mindestens 2 betragen.")

    series = data[channel_name]
    return series.rolling(window=window_size, center=True, min_periods=1).mean().to_numpy()


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
