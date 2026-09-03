"""
tests/test_naming.py

Tests for the measurement-name resolution in `data/naming.py` - the
rules that used to sit as a private method inside
`gui/main_window.py` and are now shared by the GUI and headless
scripts alike.

Deliberately does NOT need a `QApplication`/Qt: the module is pure
Python plus the filesystem, which is exactly why it could be lifted
out of the GUI in the first place.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from data.models import Channel, MeasurementConfig, ModuleType, NamingScheme, SignalType, StorageFormat
from data.naming import (
    MeasurementNameConflict,
    measurement_data_path,
    measurement_metadata_path,
    resolve_measurement_name,
)

_MOMENT = datetime(2026, 9, 1, 14, 5, 9)


class ResolveMeasurementNameTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _resolve(self, base: str, naming: NamingScheme, fmt=StorageFormat.PARQUET) -> str:
        return resolve_measurement_name(
            base_name=base,
            storage_dir=self.dir,
            storage_format=fmt,
            naming=naming,
            now=_MOMENT,
        )

    def test_number_suffix_starts_at_one(self) -> None:
        name = self._resolve("antasten", NamingScheme(True, 3, False, False))

        self.assertEqual(name, "antasten_001")

    def test_number_suffix_skips_taken_numbers(self) -> None:
        naming = NamingScheme(True, 3, False, False)
        measurement_data_path(self.dir, "antasten_001", StorageFormat.PARQUET).touch()
        measurement_data_path(self.dir, "antasten_002", StorageFormat.PARQUET).touch()

        self.assertEqual(self._resolve("antasten", naming), "antasten_003")

    def test_lone_metadata_file_also_blocks_a_number(self) -> None:
        # An aborted measurement can leave the metadata file behind
        # without any data file - reusing that name would produce a pair
        # of files that do not belong together.
        naming = NamingScheme(True, 3, False, False)
        measurement_metadata_path(self.dir, "antasten_001").touch()

        self.assertEqual(self._resolve("antasten", naming), "antasten_002")

    def test_component_order_is_name_date_time_number(self) -> None:
        naming = NamingScheme(True, 3, True, True)

        self.assertEqual(
            self._resolve("antasten", naming), "antasten_20260901_140509_001"
        )

    def test_date_and_time_are_optional(self) -> None:
        self.assertEqual(
            self._resolve("antasten", NamingScheme(False, 3, True, False)),
            "antasten_20260901",
        )
        self.assertEqual(
            self._resolve("antasten", NamingScheme(False, 3, False, True)),
            "antasten_140509",
        )

    def test_digit_count_is_honored(self) -> None:
        self.assertEqual(
            self._resolve("antasten", NamingScheme(True, 5, False, False)),
            "antasten_00001",
        )

    def test_without_suffix_a_free_name_passes_through(self) -> None:
        self.assertEqual(
            self._resolve("antasten", NamingScheme(False, 3, False, False)),
            "antasten",
        )

    def test_without_suffix_a_taken_name_raises_instead_of_overwriting(self) -> None:
        naming = NamingScheme(False, 3, False, False)
        measurement_data_path(self.dir, "antasten", StorageFormat.PARQUET).touch()

        with self.assertRaises(MeasurementNameConflict) as caught:
            self._resolve("antasten", naming)

        self.assertEqual(caught.exception.name, "antasten")

    def test_conflict_check_follows_the_storage_format(self) -> None:
        # A CSV of that name must not block the Parquet name, and the
        # other way round - the check has to look at the file the
        # StorageWriter will actually write.
        naming = NamingScheme(False, 3, False, False)
        measurement_data_path(self.dir, "antasten", StorageFormat.CSV).touch()

        self.assertEqual(self._resolve("antasten", naming), "antasten")
        with self.assertRaises(MeasurementNameConflict):
            self._resolve("antasten", naming, fmt=StorageFormat.CSV)

    def test_exhausted_digit_range_raises(self) -> None:
        naming = NamingScheme(True, 1, False, False)
        for index in range(1, 10):
            measurement_data_path(
                self.dir, f"antasten_{index}", StorageFormat.PARQUET
            ).touch()

        with self.assertRaises(RuntimeError):
            self._resolve("antasten", naming)


class NamingSchemeInConfigTest(unittest.TestCase):
    """The scheme is part of the configuration, so it has to survive a
    save/load round trip - otherwise a configuration handed to a script
    or a colleague would silently name its files differently."""

    def _config(self, **kw) -> MeasurementConfig:
        kanal = Channel(
            "cDAQ1Mod1/ai0", "Kraft",
            module_type=ModuleType.NI9215, signal_type=SignalType.VOLTAGE,
        )
        return MeasurementConfig(
            name="antasten", sample_rate_hz=1000.0, channels=[kanal], **kw
        )

    def test_default_keeps_the_overwrite_protection(self) -> None:
        naming = self._config().naming

        self.assertTrue(naming.use_number_suffix)
        self.assertEqual(naming.number_suffix_digits, 3)

    def test_scheme_survives_the_round_trip(self) -> None:
        config = self._config(
            naming=NamingScheme(
                use_number_suffix=True, number_suffix_digits=4,
                include_date=True, include_time=False,
            )
        )

        restored = MeasurementConfig.from_dict(config.to_dict())

        self.assertEqual(restored.naming, config.naming)

    def test_switched_off_suffix_survives_too(self) -> None:
        config = self._config(naming=NamingScheme(use_number_suffix=False))

        restored = MeasurementConfig.from_dict(config.to_dict())

        self.assertFalse(restored.naming.use_number_suffix)

    def test_config_written_before_naming_existed_gets_the_defaults(self) -> None:
        # Old messkonfig.json files have no "naming" section at all.
        data = self._config().to_dict()
        del data["naming"]

        restored = MeasurementConfig.from_dict(data)

        self.assertEqual(restored.naming, NamingScheme())

    def test_zero_digits_are_clamped(self) -> None:
        # 0 digits would make the suffix vanish and every measurement
        # collide with the previous one.
        data = self._config().to_dict()
        data["naming"]["number_suffix_digits"] = 0

        self.assertEqual(
            MeasurementConfig.from_dict(data).naming.number_suffix_digits, 1
        )


class PathHelperTest(unittest.TestCase):
    def test_extension_follows_the_storage_format(self) -> None:
        base = Path("C:/Messungen")

        self.assertEqual(
            measurement_data_path(base, "m", StorageFormat.PARQUET).name, "m.parquet"
        )
        self.assertEqual(
            measurement_data_path(base, "m", StorageFormat.CSV).name, "m.csv"
        )
        self.assertEqual(measurement_metadata_path(base, "m").name, "m_info.json")


if __name__ == "__main__":
    unittest.main()
