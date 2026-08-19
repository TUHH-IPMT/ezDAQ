"""
tests/test_rate_groups.py

Pure Python/dataclass tests for the grid rate logic in
`data/models.py` - specifically the generalization of
`resolve_rate_groups()` to multiple grid-constrained module types
present at the same time (NI9234 + NI9235). Deliberately does NOT
need a `QApplication`/Qt, since `resolve_rate_groups()`/`Channel`
are pure Python logic with no GUI dependency.
"""

from __future__ import annotations

import unittest

from data.models import (
    Channel,
    ModuleType,
    NI9234_BASE_SAMPLE_RATE_HZ,
    NI9235_BASE_SAMPLE_RATE_HZ,
    SignalType,
    resolve_rate_groups,
)


def _ni9234_channel(name: str = "Vib") -> Channel:
    return Channel(f"cDAQ1Mod1/ai0", name, module_type=ModuleType.NI9234, signal_type=SignalType.VOLTAGE)


def _ni9235_channel(name: str = "Strain") -> Channel:
    return Channel(
        "cDAQ1Mod2/ai0",
        name,
        module_type=ModuleType.NI9235,
        signal_type=SignalType.STRAIN,
        strain_gage_factor=2.0,
    )


def _ni9215_channel(name: str = "Free") -> Channel:
    return Channel("cDAQ1Mod3/ai0", name, module_type=ModuleType.NI9215, signal_type=SignalType.VOLTAGE)


class Ni9234RegressionTests(unittest.TestCase):
    """Behavior for a standalone NI9234 must NOT change due to the
    generalization to a shared grid-rate concept - including the
    boundary values n=1 (maximum rate) and n=31 (minimum rate)."""

    def test_valid_rate_at_n_equals_1_resolves_unchanged(self) -> None:
        groups = resolve_rate_groups([_ni9234_channel()], NI9234_BASE_SAMPLE_RATE_HZ)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].resolved_sample_rate_hz, NI9234_BASE_SAMPLE_RATE_HZ)

    def test_valid_rate_at_n_equals_31_resolves_unchanged(self) -> None:
        rate = NI9234_BASE_SAMPLE_RATE_HZ / 31
        groups = resolve_rate_groups([_ni9234_channel()], rate)
        self.assertEqual(len(groups), 1)
        self.assertAlmostEqual(groups[0].resolved_sample_rate_hz, rate)

    def test_invalid_rate_raises_with_ni9234_specific_message(self) -> None:
        with self.assertRaisesRegex(ValueError, "51200 Hz / n"):
            resolve_rate_groups([_ni9234_channel()], 1000.0)


class Ni9235GridTests(unittest.TestCase):
    """Grid math for the NI9235 (fs = 50000 Hz / n, n = 5..63)."""

    def test_max_rate_at_n_equals_5(self) -> None:
        groups = resolve_rate_groups([_ni9235_channel()], NI9235_BASE_SAMPLE_RATE_HZ / 5)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].resolved_sample_rate_hz, 10_000.0)

    def test_min_rate_at_n_equals_63(self) -> None:
        rate = NI9235_BASE_SAMPLE_RATE_HZ / 63
        groups = resolve_rate_groups([_ni9235_channel()], rate)
        self.assertEqual(len(groups), 1)
        self.assertAlmostEqual(groups[0].resolved_sample_rate_hz, rate, places=2)

    def test_invalid_intermediate_rate_raises_with_ni9235_specific_message(self) -> None:
        # 9000 Hz lies between the valid values for n=5 (10000) and
        # n=6 (8333.3) - not a valid NI9235 grid value.
        with self.assertRaisesRegex(ValueError, "50000 Hz / n"):
            resolve_rate_groups([_ni9235_channel()], 9000.0)


class MixedGridModuleTests(unittest.TestCase):
    """NI9234 and NI9235 SIMULTANEOUSLY in one measurement - the two grids
    never overlap mathematically (128*n2 = 125*n1, coprime, smallest
    solution n1=128 lies outside 1..31), so they must ALWAYS be split into
    separate rate groups, WITHOUT an error."""

    def test_mixed_modules_split_into_two_groups_without_error(self) -> None:
        # Target is an EXACTLY valid NI9234 rate (n=10) - for the NI9235
        # grid, the same target rate is nowhere near a valid value
        # (closest: n=10 -> 5000.0 Hz).
        target_rate = NI9234_BASE_SAMPLE_RATE_HZ / 10  # 5120.0 Hz
        ni9234_ch = _ni9234_channel()
        ni9235_ch = _ni9235_channel()

        groups = resolve_rate_groups([ni9234_ch, ni9235_ch], target_rate)

        self.assertEqual(len(groups), 2)
        rates = {g.resolved_sample_rate_hz for g in groups}
        self.assertEqual(rates, {5120.0, 5000.0})

        by_rate = {g.resolved_sample_rate_hz: g.channels for g in groups}
        self.assertEqual(by_rate[5120.0], [ni9234_ch])
        self.assertEqual(by_rate[5000.0], [ni9235_ch])

    def test_free_module_joins_group_closest_to_raw_target(self) -> None:
        # NI9215 has no grid limit of its own - given a conflict between
        # NI9234 (-> 5120.0, distance 0.0) and NI9235 (-> 5000.0, distance
        # 120.0) relative to the raw target rate of 5120.0 Hz, the free
        # channel ends up in the CLOSER group (NI9234, exact match).
        target_rate = NI9234_BASE_SAMPLE_RATE_HZ / 10  # 5120.0 Hz
        ni9234_ch = _ni9234_channel()
        ni9235_ch = _ni9235_channel()
        free_ch = _ni9215_channel()

        groups = resolve_rate_groups([ni9234_ch, ni9235_ch, free_ch], target_rate)

        self.assertEqual(len(groups), 2)
        by_rate = {g.resolved_sample_rate_hz: g.channels for g in groups}
        self.assertIn(free_ch, by_rate[5120.0])
        self.assertNotIn(free_ch, by_rate[5000.0])

    def test_mixed_modules_at_a_rate_valid_for_neither_grid_does_not_raise(self) -> None:
        # Most important regression protection for the generalization:
        # unlike the single-module case, a target rate that is not exactly
        # valid for EITHER grid must NOT raise an error when >=2 grid-
        # constrained modules are present at the same time - each module
        # simply gets its own closest achievable rate (exactly like the
        # existing NI9210 fixed-rate case).
        groups = resolve_rate_groups([_ni9234_channel(), _ni9235_channel()], 1000.0)
        self.assertEqual(len(groups), 2)


class ChannelStrainFieldsSerializationTests(unittest.TestCase):
    """`Channel.to_dict()`/`from_dict()` round trip for the three new
    NI9235 fields."""

    def test_round_trip_preserves_strain_fields(self) -> None:
        channel = Channel(
            "cDAQ1Mod2/ai0",
            "Strain1",
            module_type=ModuleType.NI9235,
            signal_type=SignalType.STRAIN,
            strain_gage_factor=2.13,
            strain_bridge_type="QUARTER_BRIDGE_II",
            lead_wire_resistance_ohm=0.35,
        )

        restored = Channel.from_dict(channel.to_dict())

        self.assertEqual(restored.strain_gage_factor, 2.13)
        self.assertEqual(restored.strain_bridge_type, "QUARTER_BRIDGE_II")
        self.assertEqual(restored.lead_wire_resistance_ohm, 0.35)

    def test_round_trip_defaults_when_strain_gage_factor_unset(self) -> None:
        channel = Channel("cDAQ1Mod2/ai0", "Voltage", module_type=ModuleType.NI9215)

        restored = Channel.from_dict(channel.to_dict())

        self.assertIsNone(restored.strain_gage_factor)
        self.assertEqual(restored.strain_bridge_type, "QUARTER_BRIDGE_I")
        self.assertEqual(restored.lead_wire_resistance_ohm, 0.0)


if __name__ == "__main__":
    unittest.main()
