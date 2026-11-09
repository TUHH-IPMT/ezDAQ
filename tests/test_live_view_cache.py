"""
tests/test_live_view_cache.py

Tests for `gui/live_view.py::_SweepCache`, the incremental time axis /
min-max envelope / value range of the live view.

Why this matters: the live view used to recompute all three over the
WHOLE sweep on every tick (~66x/s) - at high channel counts and rates
that made it stutter on older PCs. The cache only processes the samples
added since the last tick. It is only allowed to be faster, never
different: every test here compares it bit for bit against the full
recomputation the live view did before (`_downsample_for_display`,
`np.arange`, `np.min`/`np.max`).

Uses the same `LiveView.__new__` pattern as `tests/test_trigger_headless.py`
- no widgets are built.
"""

from __future__ import annotations

import unittest

import numpy as np
from PyQt6.QtWidgets import QApplication

from data.models import Channel, ModuleType
from gui.live_view import LiveView, _downsample_for_display, _MAX_DISPLAY_POINTS_PER_CURVE


def _reference(live_view: LiveView, index: int):
    """What `_on_timer_tick` computed before the cache: full recomputation."""
    position = live_view._channel_buffer_positions[index]
    values = live_view._display_buffer[index, :position]
    rate = live_view._channel_native_rates[index]
    times = live_view._channel_cycle_starts[index] + np.arange(position) / rate
    draw = _downsample_for_display(times, values, live_view._channel_display_capacity(index))
    value_range = (float(np.min(values)), float(np.max(values)))
    return times, draw, value_range


def _live_view(rates: list[float], window_seconds: float) -> LiveView:
    QApplication.instance() or QApplication([])
    live_view = LiveView.__new__(LiveView)
    live_view._channels = [
        Channel(f"cDAQ1Mod1/ai{i}", f"K{i}", module_type=ModuleType.NI9234,
                plot_time_window_seconds=window_seconds)
        for i in range(len(rates))
    ]
    live_view._sample_rate_hz = max(rates)
    live_view._channel_native_rates = dict(enumerate(rates))
    capacity = max(LiveView._capacity_for_rate(r, window_seconds) for r in rates)
    live_view._display_capacity_samples = capacity
    live_view._display_buffer = np.zeros((len(rates), capacity))
    live_view._channel_buffer_positions = {i: 0 for i in range(len(rates))}
    live_view._channel_cycle_starts = {i: 0.0 for i in range(len(rates))}
    live_view._channel_total_ticks_seen = {i: 0 for i in range(len(rates))}
    return live_view


class SweepCacheTest(unittest.TestCase):
    def _run_ticks(self, live_view: LiveView, num_ticks: int, seed: int, nan_every: int = 0) -> int:
        rng = np.random.default_rng(seed)
        num_channels = len(live_view._channels)
        checked = 0
        for tick in range(num_ticks):
            # Irregular block sizes, like real GUI ticks (sometimes a long
            # pause -> a large block, possibly across a sweep boundary).
            size = int(rng.choice([1, 7, 640, 1280, 5000, 23_456]))
            block = rng.standard_normal((num_channels, size)) * (1 + tick % 5)
            if nan_every and tick % nan_every == 0:
                block[:, size // 2] = np.nan
            live_view._write_to_display_buffer(block)

            for index in range(num_channels):
                if live_view._channel_buffer_positions[index] == 0:
                    continue
                cache, values = live_view._sweep_cache(index)
                times, (ref_t, ref_v), ref_range = _reference(live_view, index)

                np.testing.assert_array_equal(cache.times[:values.shape[0]], times)
                draw_t, draw_v = cache.display_data(values)
                np.testing.assert_array_equal(draw_t, ref_t)
                np.testing.assert_array_equal(draw_v, ref_v)
                np.testing.assert_array_equal(np.array(cache.value_range()), np.array(ref_range))
                checked += 1
        return checked

    def test_identical_to_full_recomputation_over_several_sweeps(self) -> None:
        # 51.2 kHz x 1 s window: heavily downsampled (factor 25), several
        # sweep wraparounds within the run.
        live_view = _live_view([51_200.0, 51_200.0], window_seconds=1.0)
        checked = self._run_ticks(live_view, num_ticks=120, seed=1)
        self.assertGreater(checked, 200)
        self.assertGreater(live_view._channel_cycle_starts[0], 2.0)

    def test_nan_propagates_like_before(self) -> None:
        live_view = _live_view([20_000.0], window_seconds=1.0)
        self._run_ticks(live_view, num_ticks=60, seed=2, nan_every=7)

    def test_slow_channel_next_to_fast_one(self) -> None:
        # Own native rate (NI9210-like) -> own capacity, own time axis.
        live_view = _live_view([10_000.0, 14.0], window_seconds=2.0)
        self._run_ticks(live_view, num_ticks=80, seed=3)

    def test_small_window_is_not_downsampled(self) -> None:
        live_view = _live_view([1_000.0], window_seconds=1.0)
        live_view._write_to_display_buffer(np.arange(500, dtype=float).reshape(1, -1))
        cache, values = live_view._sweep_cache(0)
        draw_t, draw_v = cache.display_data(values)
        self.assertEqual(draw_v.shape[0], 500)
        self.assertLessEqual(1_000, 2 * _MAX_DISPLAY_POINTS_PER_CURVE)

    def test_cache_is_rebuilt_when_the_position_moves_back(self) -> None:
        # E.g. a new measurement with the same sweep start (0.0) - the old
        # envelope must not be reused.
        live_view = _live_view([51_200.0], window_seconds=1.0)
        live_view._write_to_display_buffer(np.full((1, 30_000), 5.0))
        live_view._sweep_cache(0)

        live_view._channel_buffer_positions = {0: 0}
        live_view._write_to_display_buffer(np.full((1, 10_000), -1.0))
        cache, values = live_view._sweep_cache(0)

        self.assertEqual(cache.value_range(), (-1.0, -1.0))
        np.testing.assert_array_equal(cache.display_data(values)[1], np.full(cache.display_data(values)[1].shape, -1.0))

    def test_no_data_yet(self) -> None:
        live_view = _live_view([1_000.0], window_seconds=1.0)
        cache, values = live_view._sweep_cache(0)
        self.assertIsNone(cache)
        self.assertEqual(values.size, 0)


if __name__ == "__main__":
    unittest.main()
