"""
tools/rewrite_parquet.py

Rewrites existing ezDAQ measurement files (.parquet) with large row
groups and without dictionary encoding - the layout `StorageWriter`
(see `data/exporter.py`) writes now.

Older versions wrote every DAQ block (typically 1000 samples) as its own
row group: thousands of tiny row groups per measurement, a large footer
and slow reads. The content - columns, values, time column - is not
changed by rewriting; the script checks this column by column before it
replaces a file.

Usage (from the ezDAQ folder):

    .venv\\Scripts\\python tools\\rewrite_parquet.py <file or folder> [...]

    --dry-run       only show which files would be rewritten
    --keep-backup   keep the original as "<name>.parquet.bak"

Folders are searched recursively for *.parquet. Files that already have
large row groups are skipped.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.exporter import PARQUET_ROW_GROUP_MAX_BYTES, _PARQUET_WRITER_OPTIONS  # noqa: E402

# Below this average row-group size a file counts as "old layout".
_MIN_AVERAGE_ROWS = 10_000


def _target_rows(schema: pa.Schema) -> int:
    return max(1, PARQUET_ROW_GROUP_MAX_BYTES // (len(schema) * 8))


def needs_rewrite(path: Path) -> bool:
    metadata = pq.read_metadata(path)
    if metadata.num_row_groups <= 1:
        return False
    return metadata.num_rows / metadata.num_row_groups < _MIN_AVERAGE_ROWS


def _same_content(a: Path, b: Path) -> bool:
    """Compares two Parquet files column by column (only one column of
    each file in memory at a time)."""
    schema_a, schema_b = pq.read_schema(a), pq.read_schema(b)
    if not schema_a.equals(schema_b):
        return False
    if pq.read_metadata(a).num_rows != pq.read_metadata(b).num_rows:
        return False
    for name in schema_a.names:
        col_a = pq.read_table(a, columns=[name]).column(0)
        col_b = pq.read_table(b, columns=[name]).column(0)
        if not col_a.equals(col_b):
            return False
    return True


def rewrite(path: Path, keep_backup: bool = False) -> None:
    """Rewrites `path` with large row groups; replaces the file only
    after the content has been verified."""
    tmp = path.with_name(path.name + ".tmp")

    try:
        # Both files must be closed before `os.replace` (Windows locks open files).
        with pq.ParquetFile(path) as source, pq.ParquetWriter(
            str(tmp), source.schema_arrow, **_PARQUET_WRITER_OPTIONS
        ) as writer:
            schema = source.schema_arrow
            target_rows = _target_rows(schema)
            pending: list[pa.RecordBatch] = []
            pending_rows = 0
            for batch in source.iter_batches(batch_size=65_536):
                pending.append(batch)
                pending_rows += batch.num_rows
                while pending_rows >= target_rows:
                    table = pa.Table.from_batches(pending, schema=schema)
                    writer.write_table(table.slice(0, target_rows), row_group_size=target_rows)
                    rest = table.slice(target_rows)
                    pending = rest.to_batches()
                    pending_rows = rest.num_rows
            if pending_rows:
                writer.write_table(
                    pa.Table.from_batches(pending, schema=schema), row_group_size=pending_rows
                )

        if not _same_content(path, tmp):
            raise RuntimeError("Inhalt nach dem Umschreiben nicht identisch - Original bleibt unverändert.")

        if keep_backup:
            os.replace(path, path.with_name(path.name + ".bak"))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _collect(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(p.rglob("*.parquet")))
        elif p.suffix.lower() == ".parquet":
            files.append(p)
        else:
            print(f"Übersprungen (keine .parquet-Datei/kein Ordner): {p}")
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Schreibt ezDAQ-Parquet-Dateien mit großen Row Groups neu "
                    "(schneller lesbar, Inhalt bleibt identisch)."
    )
    parser.add_argument("paths", nargs="+", type=Path, help="Dateien oder Ordner")
    parser.add_argument("--dry-run", action="store_true", help="nur anzeigen, nichts ändern")
    parser.add_argument("--keep-backup", action="store_true",
                        help="Original als <name>.parquet.bak behalten")
    args = parser.parse_args(argv)

    errors = 0
    for path in _collect(args.paths):
        try:
            metadata = pq.read_metadata(path)
            info = f"{metadata.num_row_groups} Row Groups, {metadata.num_rows:,} Zeilen"
            if not needs_rewrite(path):
                print(f"OK, bereits große Row Groups ({info}): {path}")
                continue
            if args.dry_run:
                print(f"Würde umschreiben ({info}): {path}")
                continue
            size_before = path.stat().st_size
            rewrite(path, keep_backup=args.keep_backup)
            new = pq.read_metadata(path)
            print(
                f"Umgeschrieben: {path}\n"
                f"    {metadata.num_row_groups} -> {new.num_row_groups} Row Groups, "
                f"{size_before / 1e6:.1f} -> {path.stat().st_size / 1e6:.1f} MB"
            )
        except Exception as exc:
            errors += 1
            print(f"FEHLER bei {path}: {exc}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
