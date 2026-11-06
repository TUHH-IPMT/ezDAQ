"""
tests/test_rewrite_parquet.py

Tests for `tools/rewrite_parquet.py`, which rewrites measurement files
from older ezDAQ versions (one row group per DAQ block) with large row
groups. The one thing it must never do is change the content.

Needs pandas/pyarrow, but no Qt and no NI driver.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from tools import rewrite_parquet


def _write_old_layout(path: Path, num_rows: int, block: int = 1000) -> pd.DataFrame:
    """Writes a file the way StorageWriter did before: one row group per
    DAQ block, written from pandas (with its schema metadata)."""
    rng = np.random.default_rng(1)
    df = pd.DataFrame({
        "time_s": np.arange(num_rows) / 1000.0,
        "Kraft": rng.standard_normal(num_rows),
        "Weg": rng.standard_normal(num_rows),
    })
    writer = None
    for start in range(0, num_rows, block):
        table = pa.Table.from_pandas(df.iloc[start:start + block], preserve_index=False)
        if writer is None:
            writer = pq.ParquetWriter(str(path), table.schema)
        writer.write_table(table)
    writer.close()
    return df


class RewriteParquetTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_rewrites_into_large_row_groups_with_identical_content(self) -> None:
        path = self.dir / "alt.parquet"
        expected = _write_old_layout(path, 25_500)
        self.assertEqual(pq.ParquetFile(path).metadata.num_row_groups, 26)

        # Small target so the split across several row groups is covered.
        with mock.patch.object(rewrite_parquet, "PARQUET_ROW_GROUP_MAX_BYTES", 10_000 * 3 * 8):
            self.assertEqual(rewrite_parquet.main([str(self.dir)]), 0)

        metadata = pq.ParquetFile(path).metadata
        sizes = [metadata.row_group(i).num_rows for i in range(metadata.num_row_groups)]
        self.assertEqual(sizes, [10_000, 10_000, 5_500])
        pd.testing.assert_frame_equal(pd.read_parquet(path), expected, check_exact=True)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["alt.parquet"])

    def test_keep_backup(self) -> None:
        path = self.dir / "alt.parquet"
        _write_old_layout(path, 5_000)
        original = path.read_bytes()

        rewrite_parquet.main([str(path), "--keep-backup"])

        self.assertEqual((self.dir / "alt.parquet.bak").read_bytes(), original)
        self.assertEqual(pq.ParquetFile(path).metadata.num_row_groups, 1)

    def test_dry_run_changes_nothing(self) -> None:
        path = self.dir / "alt.parquet"
        _write_old_layout(path, 5_000)
        original = path.read_bytes()

        rewrite_parquet.main([str(path), "--dry-run"])

        self.assertEqual(path.read_bytes(), original)

    def test_new_layout_is_left_alone(self) -> None:
        path = self.dir / "neu.parquet"
        pd.DataFrame({"time_s": np.arange(50_000) / 1000.0}).to_parquet(path)
        original = path.read_bytes()

        self.assertFalse(rewrite_parquet.needs_rewrite(path))
        rewrite_parquet.main([str(path)])

        self.assertEqual(path.read_bytes(), original)

    def test_original_kept_if_verification_fails(self) -> None:
        path = self.dir / "alt.parquet"
        _write_old_layout(path, 5_000)
        original = path.read_bytes()

        with mock.patch.object(rewrite_parquet, "_same_content", return_value=False):
            self.assertEqual(rewrite_parquet.main([str(path)]), 1)

        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["alt.parquet"])


if __name__ == "__main__":
    unittest.main()
