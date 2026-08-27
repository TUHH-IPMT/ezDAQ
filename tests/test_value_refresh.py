"""
tests/test_value_refresh.py

Tests for the refresh rate of the numeric readout in the live view
(`Channel.plot_value_refresh_hz`, set in
`gui/live_view.py::ChannelValueSettingsDialog`).

Background: the view ticks at ~66 Hz
(`gui/live_view.py::_UI_UPDATE_INTERVAL_MS`), which the curve needs but
which turns a noisy reading into an unreadable blur of digits. The
readout therefore runs on its own, per-channel rate, and the readings
between two refreshes are AVERAGED - showing every n-th instantaneous
sample would only make the number jump less often, not settle down.

`LiveView._update_value_readouts()` is exercised directly rather than
through a running measurement: it is pure bookkeeping over a block of
scaled samples plus a monotonic clock, and driving it directly is the
only way to assert the timing without a DAQ device.
"""

from __future__ import annotations

import time
import unittest

import numpy as np

from data.models import Channel
from gui.live_view import LiveView


def _view(*channels: Channel) -> LiveView:
    """A LiveView reduced to what `_update_value_readouts` touches.

    `__new__` without `__init__` is the pattern already used for
    `SetupView` in `tests/test_trigger_headless.py` - it also avoids
    needing a QApplication here, since none of these attributes are Qt
    objects.
    """
    view = LiveView.__new__(LiveView)
    view._channels = list(channels)
    view._reset_value_readout_state()
    return view


def _block(*per_channel_values: list[float]) -> np.ndarray:
    return np.array(per_channel_values, dtype=float)


class ValueAveragingTest(unittest.TestCase):
    """What is shown is the mean over the interval, not a sample."""

    def test_mean_over_the_interval_is_returned(self) -> None:
        channel = Channel("cDAQ1Mod1/ai0", "Force", plot_value_refresh_hz=0.1)
        view = _view(channel)

        # First tick is due immediately (see `_reset_value_readout_state`)
        # and consumes these; at 0.1 Hz nothing is due again for 10 s.
        view._update_value_readouts(_block([1.0, 2.0, 3.0]))
        view._update_value_readouts(_block([10.0, 20.0, 30.0]))
        # Force the next refresh to be due without waiting.
        view._value_next_refresh_s[0] = 0.0
        due = view._update_value_readouts(_block([4.0, 6.0]))

        # Everything accumulated since the last refresh: 10+20+30+4+6 = 70
        # over 5 samples.
        self.assertAlmostEqual(due[0], 14.0)

    def test_accumulator_resets_after_each_refresh(self) -> None:
        channel = Channel("cDAQ1Mod1/ai0", "Force", plot_value_refresh_hz=0.1)
        view = _view(channel)

        view._update_value_readouts(_block([100.0, 100.0]))
        view._value_next_refresh_s[0] = 0.0
        due = view._update_value_readouts(_block([5.0, 5.0]))

        self.assertAlmostEqual(due[0], 5.0, msg="old samples leaked into the new mean")

    def test_averaging_calms_a_noisy_signal(self) -> None:
        """The actual point of the feature - a single sample of this
        signal scatters far more than its mean does."""
        channel = Channel("cDAQ1Mod1/ai0", "Force", plot_value_refresh_hz=0.1)
        view = _view(channel)
        rng = np.random.default_rng(0)

        means = []
        for _ in range(20):
            view._value_next_refresh_s[0] = 0.0
            samples = 5.0 + rng.normal(0.0, 1.0, 250)
            means.append(view._update_value_readouts(_block(list(samples)))[0])

        self.assertLess(
            float(np.std(means)),
            0.2,
            "means scatter almost as much as single samples (sigma = 1.0)",
        )

    def test_no_refresh_while_not_due(self) -> None:
        """A channel not due keeps its displayed number - it must not be
        rewritten with an interim value."""
        channel = Channel("cDAQ1Mod1/ai0", "Force", plot_value_refresh_hz=0.5)
        view = _view(channel)

        view._update_value_readouts(_block([1.0]))  # consumes the initial due
        due = view._update_value_readouts(_block([2.0]))

        self.assertNotIn(0, due)

    def test_empty_block_produces_no_value(self) -> None:
        """No data means nothing to average - the readout keeps standing
        rather than showing a mean over zero samples."""
        channel = Channel("cDAQ1Mod1/ai0", "Force", plot_value_refresh_hz=100.0)
        view = _view(channel)

        due = view._update_value_readouts(np.zeros((1, 0)))

        self.assertEqual(due, {})


class ValueRefreshRateTest(unittest.TestCase):
    def test_channels_refresh_independently(self) -> None:
        """Two channels with different rates must not drag each other."""
        fast = Channel("cDAQ1Mod1/ai0", "Fast", plot_value_refresh_hz=100.0)
        slow = Channel("cDAQ1Mod1/ai1", "Slow", plot_value_refresh_hz=0.1)
        view = _view(fast, slow)

        view._update_value_readouts(_block([1.0], [1.0]))  # initial, both due
        time.sleep(0.02)
        due = view._update_value_readouts(_block([2.0], [2.0]))

        self.assertIn(0, due, "100 Hz channel should be due again after 20 ms")
        self.assertNotIn(1, due, "0.1 Hz channel must not be due after 20 ms")

    def test_rate_is_not_quantized_down_by_the_tick_grid(self) -> None:
        """A refresh can only happen ON a tick, so `now` already carries
        the overshoot past the deadline. Re-basing on it would accumulate
        that overshoot and make a requested rate run measurably slower -
        the deadline is therefore stepped on a fixed grid.
        """
        channel = Channel("cDAQ1Mod1/ai0", "Force", plot_value_refresh_hz=50.0)
        view = _view(channel)

        started = time.monotonic()
        refreshes = 0
        for _ in range(40):
            if 0 in view._update_value_readouts(_block([1.0])):
                refreshes += 1
            time.sleep(0.015)
        elapsed = time.monotonic() - started

        expected = 50.0 * elapsed
        # Generous bound - this asserts the rate is not systematically
        # halved by the grid, not that timing is exact under a test
        # runner.
        self.assertGreater(
            refreshes,
            expected * 0.7,
            f"only {refreshes} refreshes in {elapsed:.2f}s, expected ~{expected:.0f}",
        )

    def test_reset_makes_every_channel_due_immediately(self) -> None:
        """So the first reading of a measurement appears on the next
        tick instead of only after a full interval - at 0.1 Hz that
        would be a ten second wait on a blank display."""
        channel = Channel("cDAQ1Mod1/ai0", "Force", plot_value_refresh_hz=0.1)
        view = _view(channel)

        due = view._update_value_readouts(_block([7.0]))

        self.assertIn(0, due)
        self.assertAlmostEqual(due[0], 7.0)

    def test_long_pause_does_not_cause_a_burst(self) -> None:
        """After the view was stopped the deadline lies far in the past;
        re-basing prevents a refresh on every single tick until caught
        up."""
        channel = Channel("cDAQ1Mod1/ai0", "Force", plot_value_refresh_hz=10.0)
        view = _view(channel)

        view._update_value_readouts(_block([1.0]))
        # Pretend the view stood still for a minute.
        view._value_next_refresh_s[0] = time.monotonic() - 60.0
        view._update_value_readouts(_block([1.0]))
        due = view._update_value_readouts(_block([1.0]))

        self.assertNotIn(0, due, "deadline was not re-based - refreshes burst")


class ValueRefreshPersistenceTest(unittest.TestCase):
    def test_default_is_30_hz(self) -> None:
        self.assertEqual(Channel("cDAQ1Mod1/ai0", "Force").plot_value_refresh_hz, 30.0)

    def test_survives_dict_round_trip(self) -> None:
        channel = Channel("cDAQ1Mod1/ai0", "Force", plot_value_refresh_hz=2.5)

        self.assertEqual(Channel.from_dict(channel.to_dict()).plot_value_refresh_hz, 2.5)

    def test_configuration_without_the_key_gets_the_default(self) -> None:
        data = Channel("cDAQ1Mod1/ai0", "Force").to_dict()
        del data["plot_value_refresh_hz"]

        self.assertEqual(Channel.from_dict(data).plot_value_refresh_hz, 30.0)

    def test_nonsensical_stored_rate_is_clamped(self) -> None:
        """0 Hz would be a division by zero in `_update_value_readouts`."""
        data = Channel("cDAQ1Mod1/ai0", "Force").to_dict()
        data["plot_value_refresh_hz"] = 0.0

        self.assertGreaterEqual(Channel.from_dict(data).plot_value_refresh_hz, 0.1)


class ValueSettingsDialogTest(unittest.TestCase):
    """The spin box in `ChannelValueSettingsDialog` reads from and writes
    back to the settings dict `ChannelDisplayDialog` carries."""

    def _settings(self, refresh_hz: float | None = 30.0) -> dict:
        settings = {
            "plot_value_integer_digits": 3,
            "plot_value_decimal_digits": 3,
        }
        if refresh_hz is not None:
            settings["plot_value_refresh_hz"] = refresh_hz
        return settings

    def setUp(self) -> None:
        from PyQt6.QtWidgets import QApplication

        # Module-level reference, see tests/test_axis_labels.py for why.
        global _APP
        try:
            _APP
        except NameError:
            _APP = QApplication.instance() or QApplication([])
        self.app = _APP

    def test_spin_box_reflects_the_incoming_setting(self) -> None:
        from gui.live_view import ChannelValueSettingsDialog

        dialog = ChannelValueSettingsDialog("Force", self._settings(4.0))
        self.assertAlmostEqual(dialog._refresh_hz_spin.value(), 4.0)
        dialog.deleteLater()
        self.app.processEvents()

    def test_results_carry_the_rate_back(self) -> None:
        from gui.live_view import ChannelValueSettingsDialog

        dialog = ChannelValueSettingsDialog("Force", self._settings(30.0))
        dialog._refresh_hz_spin.setValue(2.0)

        self.assertAlmostEqual(dialog.results()["plot_value_refresh_hz"], 2.0)
        dialog.deleteLater()
        self.app.processEvents()

    def test_missing_key_defaults_to_30_hz(self) -> None:
        """A settings dict from a configuration stored before this
        feature existed."""
        from gui.live_view import ChannelValueSettingsDialog

        dialog = ChannelValueSettingsDialog("Force", self._settings(None))
        self.assertAlmostEqual(dialog._refresh_hz_spin.value(), 30.0)
        dialog.deleteLater()
        self.app.processEvents()

    def test_rate_is_capped_at_the_tick_rate(self) -> None:
        """Refreshing more often than the view ticks is impossible, so
        the spin box stops there rather than offering a rate that would
        silently cap."""
        from gui.live_view import ChannelValueSettingsDialog, _MAX_VALUE_REFRESH_HZ

        dialog = ChannelValueSettingsDialog("Force", self._settings(9999.0))

        self.assertLessEqual(dialog._refresh_hz_spin.value(), _MAX_VALUE_REFRESH_HZ)
        dialog.deleteLater()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()


class ValueRefreshBoundsTest(unittest.TestCase):
    """Guards against out-of-range rates - the value also arrives from a
    stored configuration, not only from the (range-limited) spin box."""

    def test_absurdly_high_stored_rate_degrades_to_every_tick(self) -> None:
        """It must not divide by a tiny interval and burst, nor crash -
        the fastest meaningful rate is one refresh per view tick."""
        from gui.live_view import _MAX_VALUE_REFRESH_HZ

        channel = Channel("cDAQ1Mod1/ai0", "Force")
        # What a hand-edited configuration file could contain.
        channel.plot_value_refresh_hz = 100_000.0
        view = _view(channel)

        started = time.monotonic()
        refreshes = 0
        for _ in range(20):
            if 0 in view._update_value_readouts(_block([1.0])):
                refreshes += 1
            time.sleep(0.005)
        elapsed = time.monotonic() - started

        self.assertLessEqual(refreshes, 20, "more refreshes than ticks")
        self.assertLessEqual(
            refreshes,
            _MAX_VALUE_REFRESH_HZ * elapsed + 2,
            "rate was not capped at the view's tick rate",
        )

    def test_zero_and_negative_rates_do_not_divide_by_zero(self) -> None:
        channel = Channel("cDAQ1Mod1/ai0", "Force")
        view = _view(channel)

        for bad_rate in (0.0, -1.0, -1e9):
            with self.subTest(rate=bad_rate):
                channel.plot_value_refresh_hz = bad_rate
                view._update_value_readouts(_block([1.0]))
                self.assertGreater(view._value_next_refresh_s[0], 0.0)

    def test_dialog_clamps_a_stored_rate_above_the_tick_rate(self) -> None:
        from PyQt6.QtWidgets import QApplication
        from gui.live_view import ChannelValueSettingsDialog, _MAX_VALUE_REFRESH_HZ

        app = QApplication.instance() or QApplication([])
        dialog = ChannelValueSettingsDialog(
            "Force",
            {
                "plot_value_integer_digits": 3,
                "plot_value_decimal_digits": 3,
                "plot_value_refresh_hz": 100_000.0,
            },
        )

        # The spin box's own maximum, not `_MAX_VALUE_REFRESH_HZ` itself:
        # it is rounded DOWN to one decimal so the widget can never yield
        # a rate above the tick rate (see `ChannelValueSettingsDialog`).
        self.assertEqual(
            dialog._refresh_hz_spin.value(), dialog._refresh_hz_spin.maximum()
        )
        self.assertLessEqual(dialog._refresh_hz_spin.value(), _MAX_VALUE_REFRESH_HZ)
        dialog.deleteLater()
        app.processEvents()
