"""
tests/test_storage_writer.py

Tests for the file layout written by `data/exporter.py::StorageWriter`.

Why this matters: the writer used to append every DAQ block (typically
1000 samples) as its own Parquet row group - thousands of tiny row
groups per measurement, a footer of several MB and slow reads. It now
collects blocks into large row groups. These tests pin down both halves
of that: the row groups are large (and bounded), and the content -
columns, values, time column - is exactly what it was before.

Needs numpy/pandas/pyarrow, but no Qt and no NI driver.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from core.ringbuffer import RingBuffer
from data import exporter
from data.exporter import StorageWriter, parquet_row_group_rows
from data.models import Channel, StorageFormat

_BLOCK = 1000


def _channels() -> list[Channel]:
    return [
        Channel(hardware_channel="cDAQ1Mod1/ai0", display_name="Kraft", scale=2.0, offset=0.5),
        Channel(hardware_channel="cDAQ1Mod1/ai1", display_name="Weg", scale=-1.0, offset=0.0),
        Channel(hardware_channel="cDAQ1Mod1/ai2", display_name="Temperatur", scale=1.0, offset=-3.0),
    ]


def _raw(num_samples: int) -> np.ndarray:
    rng = np.random.default_rng(42)
    return rng.standard_normal((3, num_samples))


def _expected(raw: np.ndarray, sample_rate_hz: float) -> pd.DataFrame:
    """What the file must contain - computed independently of the writer."""
    channels = _channels()
    data = {"time_s": np.arange(raw.shape[1]) / sample_rate_hz}
    for i, channel in enumerate(channels):
        data[channel.display_name] = raw[i] * channel.scale + channel.offset
    return pd.DataFrame(data)


def _record(
    path: Path,
    raw: np.ndarray,
    sample_rate_hz: float,
    storage_format: StorageFormat = StorageFormat.PARQUET,
    block: int = _BLOCK,
    after_first_block=None,
) -> StorageWriter:
    """Runs a StorageWriter like during a real measurement: the DAQ side
    writes one block at a time, and the writer picks up each block on
    its own before the next one arrives."""
    ring_buffer = RingBuffer(num_channels=raw.shape[0], capacity=raw.shape[1] + block)
    writer = StorageWriter(
        ring_buffer,
        _channels(),
        path,
        storage_format,
        sample_rate_hz,
        poll_interval_seconds=0.0005,
    )
    writer.start()
    for start in range(0, raw.shape[1], block):
        chunk = raw[:, start:start + block]
        ring_buffer.write(chunk)
        # Wait until the writer has fully processed this block
        # (`pending_samples` drops to 0 already when it was read, before
        # it was written).
        deadline = time.monotonic() + 5.0
        while writer.total_samples_written < start + chunk.shape[1]:
            if time.monotonic() > deadline:
                raise AssertionError("StorageWriter hat den Block nicht abgeholt.")
            time.sleep(0.0005)
        if after_first_block is not None and start == 0:
            after_first_block(writer)
    writer.stop()
    return writer


class RowGroupSizeTest(unittest.TestCase):
    def test_time_limit_applies_for_slow_measurements(self) -> None:
        # 8 channels at 10 kHz: 64 MB would be ~93 s, 10 s comes first.
        self.assertEqual(parquet_row_group_rows(8, 10_000), 100_000)

    def test_byte_limit_bounds_ram_at_high_rates(self) -> None:
        # 32 channels at 51.2 kHz: 10 s would be 512k rows (~135 MB),
        # the 64 MB limit comes first.
        rows = parquet_row_group_rows(32, 51_200)
        self.assertEqual(rows, exporter.PARQUET_ROW_GROUP_MAX_BYTES // (33 * 8))
        self.assertLessEqual(rows * 33 * 8, exporter.PARQUET_ROW_GROUP_MAX_BYTES)

    def test_at_least_one_row(self) -> None:
        self.assertEqual(parquet_row_group_rows(4, 0.01), 1)


class ParquetLayoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "messung.parquet"

    def test_blocks_are_collected_into_large_row_groups(self) -> None:
        # 500 Hz -> 10 s = 5000 rows per row group; 23 blocks of 1000.
        _record(self.path, _raw(23_000), sample_rate_hz=500.0)

        metadata = pq.ParquetFile(self.path).metadata
        sizes = [metadata.row_group(i).num_rows for i in range(metadata.num_row_groups)]
        self.assertEqual(sizes, [5000, 5000, 5000, 5000, 3000])

    def test_content_is_unchanged(self) -> None:
        # Row groups (333.3 Hz -> 3333 rows) that don't line up with the
        # DAQ blocks (789 samples) - values and time column must still run
        # on seamlessly across every boundary.
        raw = _raw(23_456)
        _record(self.path, raw, sample_rate_hz=333.3, block=789)

        metadata = pq.ParquetFile(self.path).metadata
        self.assertGreater(metadata.num_row_groups, 1)
        pd.testing.assert_frame_equal(
            pd.read_parquet(self.path), _expected(raw, 333.3), check_exact=True
        )

    def test_columns_are_time_plus_one_per_channel(self) -> None:
        _record(self.path, _raw(5_000), sample_rate_hz=1_000.0)

        self.assertEqual(
            pd.read_parquet(self.path).columns.tolist(),
            ["time_s", "Kraft", "Weg", "Temperatur"],
        )
        # Reading single channels still works.
        part = pd.read_parquet(self.path, columns=["time_s", "Weg"])
        self.assertEqual(part.columns.tolist(), ["time_s", "Weg"])

    def test_large_backlog_is_split_into_bounded_row_groups(self) -> None:
        # If the writer falls behind, one read can return far more than a
        # row group - it must still be split, not written as one block.
        raw = _raw(12_345)
        ring_buffer = RingBuffer(num_channels=3, capacity=20_000)
        writer = StorageWriter(ring_buffer, _channels(), self.path, StorageFormat.PARQUET, 100.0)
        ring_buffer.write(raw)
        writer.start()
        writer.stop()

        metadata = pq.ParquetFile(self.path).metadata
        sizes = [metadata.row_group(i).num_rows for i in range(metadata.num_row_groups)]
        self.assertEqual(sizes, [1000] * 12 + [345])
        pd.testing.assert_frame_equal(pd.read_parquet(self.path), _expected(raw, 100.0))

    def test_no_dictionary_encoding(self) -> None:
        _record(self.path, _raw(5_000), sample_rate_hz=1_000.0)

        metadata = pq.ParquetFile(self.path).metadata
        for rg in range(metadata.num_row_groups):
            for col in range(metadata.num_columns):
                encodings = metadata.row_group(rg).column(col).encodings
                self.assertFalse(
                    any("DICTIONARY" in e for e in encodings),
                    f"Spalte {col} in Row Group {rg} nutzt {encodings}",
                )

    def test_file_exists_from_the_first_block(self) -> None:
        seen = {}

        def check(writer: StorageWriter) -> None:
            seen["exists"] = self.path.exists()
            seen["buffered"] = writer.buffered_samples

        _record(self.path, _raw(3_000), sample_rate_hz=1_000.0, after_first_block=check)

        self.assertTrue(seen["exists"])
        self.assertEqual(seen["buffered"], _BLOCK)

    def test_everything_is_written_on_stop(self) -> None:
        raw = _raw(2_500)
        writer = _record(self.path, raw, sample_rate_hz=1_000.0)

        self.assertEqual(writer.total_samples_written, 2_500)
        self.assertEqual(writer.buffered_samples, 0)
        self.assertEqual(len(pd.read_parquet(self.path)), 2_500)


class CsvTest(unittest.TestCase):
    def test_csv_is_still_appended_block_by_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "messung.csv"
            seen = {}

            def check(writer: StorageWriter) -> None:
                seen["rows"] = len(pd.read_csv(path))
                seen["buffered"] = writer.buffered_samples

            raw = _raw(3_000)
            _record(path, raw, 1_000.0, StorageFormat.CSV, after_first_block=check)

            self.assertEqual(seen, {"rows": _BLOCK, "buffered": 0})
            pd.testing.assert_frame_equal(pd.read_csv(path), _expected(raw, 1_000.0))

    def test_csv_header_and_missing_values_like_pandas(self) -> None:
        # The old writer used `DataFrame.to_csv`: header quoted only where
        # needed, NaN as an empty field. Both must stay that way.
        channels = [
            Channel(hardware_channel="a", display_name="Kraft [N]"),
            Channel(hardware_channel="b", display_name="x,y"),
        ]
        raw = np.array([[1.0, np.nan, 3.0], [0.5, 0.25, np.nan]])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "messung.csv"
            ring_buffer = RingBuffer(num_channels=2, capacity=10)
            writer = StorageWriter(ring_buffer, channels, path, StorageFormat.CSV, 2.0)
            ring_buffer.write(raw)
            writer.start()
            writer.stop()

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], 'time_s,Kraft [N],"x,y"')
            self.assertEqual(lines[2], "0.5,,0.25")
            self.assertEqual(lines[3], "1,3,")
            loaded = pd.read_csv(path)
            self.assertEqual(loaded.columns.tolist(), ["time_s", "Kraft [N]", "x,y"])
            np.testing.assert_array_equal(loaded["x,y"].to_numpy(), raw[1])


if __name__ == "__main__":
    unittest.main()
