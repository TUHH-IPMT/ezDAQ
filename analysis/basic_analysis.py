"""
analysis/basic_analysis.py

Analysis functions for the analysis view (see `gui/analysis_view.py`).

All functions receive a `pandas.DataFrame` with a "time_s" column and
one column per channel (see `data/loader.py::LoadedMeasurement`) and
return raw data (numpy arrays) - wrapping the result as a new result
channel (including `Channel` metadata, x-axis, etc.) is handled by
`gui/analysis_view.py`, so this module stays independent of the GUI
and individually testable.

Currently implemented:
    * compute_fft(...): amplitude spectrum of a channel (numpy.fft.rfft).
    * apply_filter(...): Butterworth low-/high-pass (scipy.signal).
    * apply_smoothing(...): moving average.
    * native_samples(...): de-duplicates a forward-filled (zero-order
      hold) channel down to its actual new samples - see
      `gui/analysis_view.py::_prepare_channel_for_rate_aware_analysis`.

Not yet implemented (future version):
    * compute_rms(...), compute_statistics(...), generate_report(...).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def compute_fft(
    data: pd.DataFrame, channel_name: str, sample_rate_hz: float
) -> tuple[np.ndarray, np.ndarray]:
    """Computes the (one-sided) amplitude spectrum of a channel.

    Returns:
        Tuple `(freq_hz, amplitude)` of equal length.
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
        amplitude[0] /= 2.0  # DC component is not doubled by the *2 normalization

    return freq_hz, amplitude


def apply_filter(
    data: pd.DataFrame,
    channel_name: str,
    sample_rate_hz: float,
    cutoff_hz: float,
    kind: str = "lowpass",
) -> np.ndarray:
    """Applies a Butterworth low-/high-pass filter to a channel.

    Zero-phase filtering via `scipy.signal.filtfilt`, so the result is
    not shifted in time relative to the original signal.
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


def native_samples(data: pd.DataFrame, channel_name: str) -> pd.DataFrame:
    """Reduces a zero-order-held (forward-filled) channel down to only
    the rows where the value actually changes (= a genuine new sample).

    A channel merged by `core/rate_merge.py::RateMerger` into a faster
    rate group (e.g. an NI9210 alongside a faster module, see
    `data/models.py::resolve_rate_groups`) repeats its last real
    measurement value until a new sample is due - FFT/filters must not
    treat these repetitions as genuine new samples at the file tick
    rate (otherwise the zero-order-hold staircase would fake a false,
    sinc-shaped spectrum artifact).

    Detects repetitions purely from consecutive identical values -
    robust against the non-integer tick ratio produced by `RateMerger`
    (e.g. ~118 fast ticks per genuine 14 S/s sample), without needing
    to know the exact ratio.

    ONLY call this for channels whose native rate (see
    `data/metadata.py::build_measurement_metadata`, key
    `native_sample_rate_hz`) is below the file tick rate - otherwise
    legitimate repeated values in genuine, non-forward-filled signals
    would be incorrectly removed.
    """
    values = data[channel_name].to_numpy()
    keep = np.ones(len(values), dtype=bool)
    keep[1:] = values[1:] != values[:-1]
    return data.loc[keep]


def apply_smoothing(data: pd.DataFrame, channel_name: str, window_size: int) -> np.ndarray:
    """Smooths a channel using a moving average (centered window)."""
    if window_size < 2:
        raise ValueError("Die Fenstergröße muss mindestens 2 betragen.")

    series = data[channel_name]
    return series.rolling(window=window_size, center=True, min_periods=1).mean().to_numpy()


def compute_rms(data: pd.DataFrame, channel_name: str) -> float:
    """Computes the RMS value of a channel (NOT IMPLEMENTED)."""
    raise NotImplementedError(
        "RMS-Berechnung ist für eine spätere Version vorgesehen, aber noch nicht implementiert."
    )


def compute_statistics(data: pd.DataFrame, channel_name: str) -> dict:
    """Computes basic statistics (min/max/mean/std) of a channel
    (NOT IMPLEMENTED)."""
    raise NotImplementedError(
        "Statistik-Berechnung ist für eine spätere Version vorgesehen, aber noch nicht implementiert."
    )


def generate_report(measurement_path: str, output_path: str) -> None:
    """Generates an automated report for a measurement (NOT IMPLEMENTED)."""
    raise NotImplementedError(
        "Automatische Reports sind für eine spätere Version vorgesehen, aber noch nicht implementiert."
    )
