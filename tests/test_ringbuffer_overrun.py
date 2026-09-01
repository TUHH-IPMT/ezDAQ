"""
tests/test_ringbuffer_overrun.py

Tests for the overrun accounting in `core/ringbuffer.py`.

Why this matters: an overrun repairs itself - the reader is pulled
forward to the oldest still-valid position and carries on. The data
loss is therefore invisible afterwards, and doubly so in a stored
measurement: `data/exporter.py` derives the time column from the number
of samples it actually wrote, so a gap closes up seamlessly instead of
showing as a gap. `lost_samples()` is the only trace that remains.

Needs numpy (the buffer is a NumPy array), but no Qt and no NI driver.
"""

from __future__ import annotations

import unittest

import numpy as np

from core.ringbuffer import RingBuffer


def _block(num_channels: int, num_samples: int, value: float) -> np.ndarray:
    return np.full((num_channels, num_samples), value, dtype=np.float64)


class LostSamplesTest(unittest.TestCase):
    def test_new_reader_starts_without_losses(self) -> None:
        buffer = RingBuffer(num_channels=2, capacity=100)

        reader = buffer.register_reader()

        self.assertEqual(buffer.lost_samples(reader), 0)

    def test_reader_that_keeps_up_loses_nothing(self) -> None:
        buffer = RingBuffer(num_channels=2, capacity=100)
        reader = buffer.register_reader()

        for _ in range(10):
            buffer.write(_block(2, 50, 1.0))
            buffer.read_new(reader)

        self.assertEqual(buffer.lost_samples(reader), 0)

    def test_overrun_is_counted(self) -> None:
        buffer = RingBuffer(num_channels=2, capacity=100)
        reader = buffer.register_reader()

        # 250 written, 100 fit in the buffer - the reader never read, so
        # everything beyond the last 100 samples is gone.
        for _ in range(5):
            buffer.write(_block(2, 50, 1.0))

        self.assertEqual(buffer.lost_samples(reader), 150)

    def test_losses_accumulate_over_several_overruns(self) -> None:
        buffer = RingBuffer(num_channels=2, capacity=100)
        reader = buffer.register_reader()

        for _ in range(3):
            buffer.write(_block(2, 50, 1.0))
        erste = buffer.lost_samples(reader)
        for _ in range(3):
            buffer.write(_block(2, 50, 1.0))

        self.assertEqual(erste, 50)
        self.assertEqual(buffer.lost_samples(reader), 200)

    def test_a_slow_reader_does_not_affect_a_fast_one(self) -> None:
        # The central promise of the buffer: the live view may skip
        # samples without the storage writer losing any.
        buffer = RingBuffer(num_channels=2, capacity=100)
        schnell = buffer.register_reader()
        langsam = buffer.register_reader()

        for _ in range(6):
            buffer.write(_block(2, 50, 1.0))
            buffer.read_new(schnell)

        self.assertEqual(buffer.lost_samples(schnell), 0)
        self.assertGreater(buffer.lost_samples(langsam), 0)

    def test_read_after_overrun_returns_only_what_is_left(self) -> None:
        buffer = RingBuffer(num_channels=2, capacity=100)
        reader = buffer.register_reader()

        for _ in range(5):
            buffer.write(_block(2, 50, 1.0))
        data = buffer.read_new(reader)

        self.assertEqual(data.shape[1], 100)
        self.assertEqual(buffer.lost_samples(reader), 150)

    def test_reset_clears_the_counter(self) -> None:
        buffer = RingBuffer(num_channels=2, capacity=100)
        reader = buffer.register_reader()
        for _ in range(5):
            buffer.write(_block(2, 50, 1.0))

        buffer.reset()

        self.assertEqual(buffer.lost_samples(reader), 0)

    def test_unknown_reader_raises(self) -> None:
        buffer = RingBuffer(num_channels=2, capacity=100)
        reader = buffer.register_reader()
        buffer.unregister_reader(reader)

        with self.assertRaises(KeyError):
            buffer.lost_samples(reader)


if __name__ == "__main__":
    unittest.main()
