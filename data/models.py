"""
data/models.py

Central data models of the application.

This module contains pure data structures (no hardware or GUI logic).
All other layers (hardware, core, gui, analysis) use these models as a
shared "language" to describe channels, devices, and measurements.

Design decision:
    The models are deliberately implemented as `dataclasses` with type
    hints. That keeps them lightweight, JSON-serializable (see
    data/metadata.py), and easy to extend without GUI or hardware code
    having to know about them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class ModuleType(str, Enum):
    """Supported NI cDAQ module types.

    Implemented as a string enum so the value can be stored directly and
    readably in JSON configurations and metadata.
    """

    NI9215 = "NI9215"
    NI9234 = "NI9234"
    NI9210 = "NI9210"
    NI9213 = "NI9213"
    NI9235 = "NI9235"


NI9210_FIXED_SAMPLE_RATE_HZ = 14.0

# Unlike the SAR ADC of the NI9215, the NI9234 has a delta-sigma ADC:
# the sample rate is not freely selectable, only as an integer divider
# of the internal master timebase (13.1072 MHz, already divided by 256
# internally): fs = 51,200 Hz / n, n integer 1..31 (51,200 S/s down to
# approx. 1,651.6 S/s). Source: NI 9234 Operating Instructions and
# Specifications, section "Understanding NI 9234 Data Rates". The
# NI-DAQmx driver does accept other values too (and rounds internally to
# the nearest valid rate), but without validating this up front, the app
# would keep computing with the unrounded rate that isn't actually being
# measured (metadata, time axis, FFT in the analysis view).
NI9234_BASE_SAMPLE_RATE_HZ = 51_200.0
NI9234_MIN_RATE_DIVISOR = 1
NI9234_MAX_RATE_DIVISOR = 31

# The NI9235 (120 Ω quarter-bridge strain gauge module) also has a
# delta-sigma ADC with the same grid pattern, but a DIFFERENT master
# timebase (12.8 MHz instead of 13.1072 MHz): fs = (12.8 MHz / 256) / n =
# 50,000 Hz / n. On the internal timebase, n is limited to 5..63
# (10,000 S/s down to approx. 793.65 S/s) - unlike the NI9234, NOT
# starting at n=1, since the hardware no longer provides a valid rate
# above 10 kS/s. Source: NI-9235 Specifications (ni.com, 2022-07-11),
# section "Data Rates" / "Data rate range (fs) using internal master
# timebase".
NI9235_BASE_SAMPLE_RATE_HZ = 50_000.0
NI9235_MIN_RATE_DIVISOR = 5
NI9235_MAX_RATE_DIVISOR = 63


@dataclass(frozen=True)
class GridRateSpec:
    """Describes the sample-rate grid of a delta-sigma module (fs =
    `base_hz` / n, n integer `min_divisor`..`max_divisor`).

    Lets multiple modules with a structurally identical but numerically
    different grid (currently NI9234 and NI9235) share the same grid
    validation/rounding logic, see `_GRID_SAMPLE_RATE_SPEC_BY_MODULE`.
    """

    module_label: str
    base_hz: float
    min_divisor: int
    max_divisor: int


# Modules with a delta-sigma grid (fs = base_hz / n) that CANNOT be
# freely set to an arbitrary target rate, only snapped. A future
# additional grid module only needs an entry here - see
# `resolve_rate_groups()`, which generically iterates over all existing
# entries (no longer NI9234-specific).
_GRID_SAMPLE_RATE_SPEC_BY_MODULE: dict[ModuleType, GridRateSpec] = {
    ModuleType.NI9234: GridRateSpec("NI9234", NI9234_BASE_SAMPLE_RATE_HZ, NI9234_MIN_RATE_DIVISOR, NI9234_MAX_RATE_DIVISOR),
    ModuleType.NI9235: GridRateSpec("NI9235", NI9235_BASE_SAMPLE_RATE_HZ, NI9235_MIN_RATE_DIVISOR, NI9235_MAX_RATE_DIVISOR),
}


def _grid_valid_sample_rates(spec: GridRateSpec) -> list[float]:
    """All valid sample rates of a grid module (sorted descending)."""
    return [spec.base_hz / n for n in range(spec.min_divisor, spec.max_divisor + 1)]


def _nearest_grid_sample_rate(sample_rate_hz: float, spec: GridRateSpec) -> float:
    """The valid grid rate of `spec` closest to `sample_rate_hz`.

    Intended exclusively for the grid check in `_is_valid_grid_sample_rate`
    (symmetric tolerance around a valid value). The suggestion in error
    messages deliberately does NOT use this function but
    `_next_grid_sample_rate_at_or_above` instead - see there.
    """
    return min(_grid_valid_sample_rates(spec), key=lambda rate: abs(rate - sample_rate_hz))


def _next_grid_sample_rate_at_or_above(sample_rate_hz: float, spec: GridRateSpec) -> float:
    """The smallest valid grid rate of `spec` that does not fall below
    `sample_rate_hz` - i.e. round up instead of to the nearest value.

    Rationale: a sample rate that's too high only costs disk space, while
    one that's too low irrecoverably loses signal content (the module's
    anti-aliasing filter tracks the rate, so a grid choice that's too low
    cuts off real frequency content). For vibration/strain measurements,
    bandwidth is usually the actual requirement - hence rounding up when
    in doubt.

    Used ONLY as a suggestion in the error message, NOT applied
    automatically: the user has to enter the valid rate themselves, so
    the app never silently measures something other than what was
    configured (exactly the DIAdem/NI MAX pitfall).

    If the request exceeds the highest supported rate, that rate is
    returned - nothing goes beyond that on the hardware side.
    """
    candidates = [rate for rate in _grid_valid_sample_rates(spec) if rate >= sample_rate_hz]
    return min(candidates) if candidates else spec.base_hz / spec.min_divisor


def _is_valid_grid_sample_rate(sample_rate_hz: float, spec: GridRateSpec, tolerance_hz: float = 0.05) -> bool:
    """Checks `sample_rate_hz` against the grid of `spec` (fs = base_hz / n).

    `tolerance_hz` covers rounding from the GUI input (spinbox with one
    decimal place, see `gui/setup_view.py::_sample_rate_spin`) - many
    valid rates (e.g. 51200/3 = 17066.666...) can't be represented
    exactly with one decimal place anyway.
    """
    return abs(sample_rate_hz - _nearest_grid_sample_rate(sample_rate_hz, spec)) <= tolerance_hz


def grid_valid_sample_rates(module_type: ModuleType) -> list[float]:
    """Public, module-type-generic version of `_grid_valid_sample_rates`."""
    return _grid_valid_sample_rates(_GRID_SAMPLE_RATE_SPEC_BY_MODULE[module_type])


def nearest_grid_sample_rate(module_type: ModuleType, sample_rate_hz: float) -> float:
    """Public, module-type-generic version of `_nearest_grid_sample_rate`."""
    return _nearest_grid_sample_rate(sample_rate_hz, _GRID_SAMPLE_RATE_SPEC_BY_MODULE[module_type])


def next_grid_sample_rate_at_or_above(module_type: ModuleType, sample_rate_hz: float) -> float:
    """Public, module-type-generic version of `_next_grid_sample_rate_at_or_above`."""
    return _next_grid_sample_rate_at_or_above(sample_rate_hz, _GRID_SAMPLE_RATE_SPEC_BY_MODULE[module_type])


def is_valid_grid_sample_rate(module_type: ModuleType, sample_rate_hz: float, tolerance_hz: float = 0.05) -> bool:
    """Public, module-type-generic version of `_is_valid_grid_sample_rate`."""
    return _is_valid_grid_sample_rate(sample_rate_hz, _GRID_SAMPLE_RATE_SPEC_BY_MODULE[module_type], tolerance_hz)


def ni9234_valid_sample_rates() -> list[float]:
    """All 31 valid sample rates of the NI9234 (sorted descending).

    Thin wrapper around the generic grid logic (see
    `_GRID_SAMPLE_RATE_SPEC_BY_MODULE`) - behavior unchanged from when
    this function was implemented NI9234-specifically.
    """
    return grid_valid_sample_rates(ModuleType.NI9234)


def nearest_ni9234_sample_rate(sample_rate_hz: float) -> float:
    """The valid NI9234 sample rate closest to `sample_rate_hz`.

    Intended exclusively for the grid check in `is_valid_ni9234_sample_rate`
    (symmetric tolerance around a valid value). The suggestion in error
    messages deliberately does NOT use this function but
    `next_ni9234_sample_rate_at_or_above` instead - see there.
    """
    return nearest_grid_sample_rate(ModuleType.NI9234, sample_rate_hz)


def next_ni9234_sample_rate_at_or_above(sample_rate_hz: float) -> float:
    """The smallest valid NI9234 sample rate that does not fall below
    `sample_rate_hz` - i.e. round up instead of to the nearest value.

    Used ONLY as a suggestion in the error message, NOT applied
    automatically: the user has to enter the valid rate themselves, so
    the app never silently measures something other than what was
    configured (exactly the DIAdem/NI MAX pitfall).
    """
    return next_grid_sample_rate_at_or_above(ModuleType.NI9234, sample_rate_hz)


def is_valid_ni9234_sample_rate(sample_rate_hz: float, tolerance_hz: float = 0.05) -> bool:
    """Checks `sample_rate_hz` against the NI9234 grid (fs = 51200 Hz / n)."""
    return is_valid_grid_sample_rate(ModuleType.NI9234, sample_rate_hz, tolerance_hz)


class SignalType(str, Enum):
    """Physical signal type of a channel.

    Used, among other things, by the hardware layer to decide which
    nidaqmx channel function (e.g. `ai_voltage_chan` vs. `ai_accel_chan`)
    needs to be called for a channel.
    """

    VOLTAGE = "voltage"
    IEPE_ACCELERATION = "iepe_acceleration"
    THERMOCOUPLE = "thermocouple"
    STRAIN = "strain"


# Thermocouple types offered by the application (NI9210/NI9213, see
# `hardware/ni9210.py`). Values correspond directly to the member names
# of `nidaqmx.constants.ThermocoupleType` (e.g. `ThermocoupleType["K"]`),
# so no additional translation table needs to be maintained here. The
# rarer types A/C (tungsten-rhenium) are deliberately not included.
THERMOCOUPLE_TYPES = ["K", "J", "T", "E", "N", "R", "S", "B"]

# Practical measurement ranges per thermocouple type in °C (rough
# reference values per IEC 60584 for the standard measuring range), used
# as min_val/max_val for `add_ai_thrmcpl_chan` (see `hardware/ni9210.py`).
# Not an exact calibration-lab datasheet - sufficient for the supported
# use case (temperature monitoring, not metrological precision
# measurement).
THERMOCOUPLE_TEMPERATURE_RANGES_C: dict[str, tuple[float, float]] = {
    "K": (-200.0, 1372.0),
    "J": (-210.0, 1200.0),
    "T": (-200.0, 400.0),
    "E": (-200.0, 1000.0),
    "N": (-200.0, 1300.0),
    "R": (-50.0, 1768.0),
    "S": (-50.0, 1768.0),
    "B": (250.0, 1820.0),
}

# ADC timing modes that control the trade-off between speed and
# effective resolution - available in hardware ONLY on the NI9213, NOT
# on the NI9210 (which has a fixed sample rate of 14 S/s with no
# configurable timing mode). Values correspond directly to the member
# names of `nidaqmx.constants.ADCTimingMode`, see `hardware/ni9213.py`.
# The full DAQmx driver additionally knows "AUTOMATIC",
# "BEST_50_HZ_REJECTION", "BEST_60_HZ_REJECTION", and "CUSTOM" -
# deliberately not offered here, since neither NI-MAX nor DIAdem present
# them for selection in their UI (only HIGH_RESOLUTION/HIGH_SPEED), and
# no verified conversion times could be found for the first three
# either (see git history/doc/offene_punkte.md).
ADC_TIMING_MODES = [
    "HIGH_RESOLUTION",
    "HIGH_SPEED",
]

# Bridge variants supported by the NI9235 in hardware - EXCLUSIVELY
# quarter-bridge (see hardware/ni9235.py), half-/full-bridge are not
# physically wired on this module. Values correspond directly to the
# member names of `nidaqmx.constants.StrainGageBridgeType`.
#   QUARTER_BRIDGE_I:  one active strain gage element (default case).
#   QUARTER_BRIDGE_II: one active strain gage element + one dummy element.
NI9235_BRIDGE_TYPES = ["QUARTER_BRIDGE_I", "QUARTER_BRIDGE_II"]

# Conversion time per channel per ADC timing mode (seconds) - the
# NI9213 ADC is multiplexed across the channels of ONE physical module;
# per the NI 9213 datasheet, the maximum achievable sample rate is given
# by fs_max = min(1 / (conversion time * number of active channels),
# 100 S/s) - "if you are using fewer than all channels, the sample rate
# might be faster". Both values are confirmed via several independent
# NI community citations from the datasheet.
NI9213_CONVERSION_TIME_S: dict[str, float] = {
    "HIGH_RESOLUTION": 0.055,
    "HIGH_SPEED": 0.00074,
}
NI9213_MAX_SAMPLE_RATE_HZ = 100.0


def ni9213_device_groups(channels: list["Channel"]) -> dict[str, list["Channel"]]:
    """Groups the active NI9213 channels by physical device (e.g.
    "cDAQ1Mod3"), since the ADC is multiplexed per module (not per
    measurement configuration) - two separate NI9213 modules don't
    share a common converter bandwidth.

    A self-contained, deliberately simple string grouping instead of an
    import from `core/measurement.py::group_channels_by_device` -
    `data/models.py` deliberately does not depend on `core/` (see module
    docstring above).
    """
    groups: dict[str, list[Channel]] = {}
    for channel in channels:
        if not (channel.enabled and channel.module_type == ModuleType.NI9213):
            continue
        device_name = channel.hardware_channel.split("/", 1)[0]
        groups.setdefault(device_name, []).append(channel)
    return groups


def max_ni9213_sample_rate_hz(channels_on_device: list["Channel"]) -> float:
    """Maximum achievable sample rate for ONE physical NI9213 module.

    `channels_on_device`: only the active channels of this one device
    (see `ni9213_device_groups`). If `adc_timing_mode` is inconsistent
    within the group (shouldn't happen via the GUI, see
    `hardware/ni9213.py`, but possible e.g. with a manually edited
    configuration file), defensively assumes the slowest mode involved.
    """
    if not channels_on_device:
        return NI9213_MAX_SAMPLE_RATE_HZ
    conversion_time_s = max(
        NI9213_CONVERSION_TIME_S.get(ch.adc_timing_mode, NI9213_CONVERSION_TIME_S["HIGH_RESOLUTION"])
        for ch in channels_on_device
    )
    return min(NI9213_MAX_SAMPLE_RATE_HZ, 1.0 / (conversion_time_s * len(channels_on_device)))


# Module types with a hardware-side FIXED sample rate that cannot be
# adapted to a shared target rate (currently only the NI9210 at 14 S/s).
# NI9234/NI9235 (grid modules, see `_GRID_SAMPLE_RATE_SPEC_BY_MODULE`
# above) and NI9213 (max. achievable rate depends on channel count/timing
# mode) are deliberately excluded here: all three can satisfy ONE shared
# target rate as long as it lies on their respective grid, or below their
# maximum - they therefore fundamentally stay in the shared "target rate"
# group (exception: two grid modules present AT THE SAME TIME with rates
# that snap differently for the target rate, see `resolve_rate_groups()`).
# A future module with a similarly rigid SINGLE rate only needs to be
# added here - `resolve_rate_groups()` automatically forms its own group
# from it, without the grouping logic itself needing to be adjusted.
_FIXED_SAMPLE_RATE_HZ_BY_MODULE: dict[ModuleType, float] = {
    ModuleType.NI9210: NI9210_FIXED_SAMPLE_RATE_HZ,
}

# Tolerance for comparing "fixed module rate == target rate" - covers
# rounding of the GUI input (see `is_valid_ni9234_sample_rate` for the
# same tolerance used elsewhere).
_FIXED_RATE_TOLERANCE_HZ = 0.05


@dataclass
class RateGroup:
    """A set of active channels that can share the same sample rate in
    hardware, and therefore (the preferred case) run in a single nidaqmx
    task with true sample-clock synchronicity.

    Multiple `RateGroup`s in one measurement arise ONLY when (a) a
    module has a hardware-fixed rate that's incompatible with the other
    channels (see `_FIXED_SAMPLE_RATE_HZ_BY_MODULE`, currently: NI9210,
    fixed 14 S/s), or (b) two grid modules present at the same time (see
    `_GRID_SAMPLE_RATE_SPEC_BY_MODULE`, currently NI9234/NI9235) snap to
    different rates for the target rate - this is the exception, not the
    default case. See `resolve_rate_groups`.
    """

    channels: list["Channel"]
    resolved_sample_rate_hz: float
    reason: str


def resolve_rate_groups(
    channels: list["Channel"], target_sample_rate_hz: float
) -> list[RateGroup]:
    """Splits active channels into groups with a jointly usable sample rate.

    A channel from a module in `_FIXED_SAMPLE_RATE_HZ_BY_MODULE` (a fixed
    rate that is NOT adaptable to a target rate) only gets its own group
    if its fixed rate differs from `target_sample_rate_hz` - if the
    target rate happens to exactly match the fixed rate (e.g. an NI9210
    at a target rate of exactly 14 S/s), there's no conflict and the
    channel stays in the shared "target rate" group. Multiple actually
    differing fixed rates are grouped BY THEIR RESPECTIVE RATE (not by
    module type) - two different modules that happen to share the same
    fixed rate end up in the same group this way. All other modules
    (adaptable to a target rate) ALWAYS stay in the shared "target rate"
    group (the preferred, truly synchronized case) - this group is never
    split for convenience.

    Args:
        channels: Active channels (e.g. `MeasurementConfig.active_channels()`).
        target_sample_rate_hz: Target rate set by the user.

    Returns:
        One group per rate that actually occurs (target-rate group
        first, if present, then the fixed-rate groups in the order of
        their first occurrence in `channels`) - this order later
        determines the channel order in the ring buffer (see
        `core/controller.py::start_measurement`).

    Raises:
        ValueError: if the target rate is intrinsically unreachable for
            a module WITHOUT a fixed rate (NI9234 grid, NI9213 maximum
            rate) - this is independent of which other modules are in
            the measurement, so it's NOT a "sharing" problem but a
            genuine misconfiguration.
    """
    fixed_channels: list[Channel] = []
    adaptive_channels: list[Channel] = []
    for ch in channels:
        fixed_rate = _FIXED_SAMPLE_RATE_HZ_BY_MODULE.get(ch.module_type)
        if fixed_rate is not None and abs(target_sample_rate_hz - fixed_rate) > _FIXED_RATE_TOLERANCE_HZ:
            fixed_channels.append(ch)
        else:
            # No conflict: either no module with a fixed rate, or the
            # fixed rate already matches the target rate - the channel
            # can stay in the shared task (see docstring above).
            adaptive_channels.append(ch)

    groups: list[RateGroup] = []

    if adaptive_channels:
        # All grid module types (see `_GRID_SAMPLE_RATE_SPEC_BY_MODULE`)
        # that actually occur among the adaptive channels - sorted so
        # the group order stays deterministic for the same input. Each
        # is checked/snapped against ITS OWN grid, no longer just the
        # NI9234 (see docstring above, case (b)).
        grid_module_types = sorted(
            (
                {ch.module_type for ch in adaptive_channels}
                & _GRID_SAMPLE_RATE_SPEC_BY_MODULE.keys()
            ),
            key=lambda m: m.value,
        )
        resolved_by_module: dict[ModuleType, float] = {}
        if len(grid_module_types) == 1:
            # Default case: EXACTLY ONE grid module type present. Behavior
            # unchanged from before this generalization: a target rate
            # that doesn't lie on THIS grid (within tolerance) is a
            # genuine configuration error - the user should deliberately
            # enter a valid value instead of the app silently falling
            # back to a completely different one.
            module_type = grid_module_types[0]
            spec = _GRID_SAMPLE_RATE_SPEC_BY_MODULE[module_type]
            if not _is_valid_grid_sample_rate(target_sample_rate_hz, spec):
                suggestion = _next_grid_sample_rate_at_or_above(target_sample_rate_hz, spec)
                raise ValueError(
                    f"Das {spec.module_label} unterstützt nur Abtastraten nach der Formel "
                    f"{spec.base_hz:.0f} Hz / n (n = {spec.min_divisor}..{spec.max_divisor}); "
                    f"nächster gültiger Wert nach oben: {suggestion:.1f} S/s."
                )
            # Snap to the EXACT valid grid rate, do NOT use the raw
            # target rate (e.g. rounded to one decimal place): DAQmx
            # rounds a value that lies even minimally ABOVE a valid rate
            # up to the NEXT-HIGHER valid rate (not to the nearest one)
            # - e.g. 17066.7 Hz (0.03 Hz above the exactly valid
            # 17066.67 Hz) would internally jump up to 25600 Hz, without
            # the app/metadata/live view noticing. Snapping to the exact
            # value here, right at the source, ensures that the rate
            # shown/saved everywhere is actually the one DAQmx really
            # configures - verified on real hardware
            # (`task.timing.samp_clk_rate`).
            resolved_by_module[module_type] = _nearest_grid_sample_rate(target_sample_rate_hz, spec)
        else:
            # >=2 grid module types present AT THE SAME TIME (e.g.
            # NI9234 + NI9235): the two grids mathematically never
            # overlap (see the module comment at
            # `NI9235_BASE_SAMPLE_RATE_HZ`) - so the raw target rate can
            # generally only be "valid" for AT MOST one of the two
            # grids. Unlike the single-module case, this is NOT a
            # configuration error but the default case here (exactly
            # like the NI9210 fixed-rate case: each module simply gets
            # the rate nearest to it on its own grid, without an error
            # message) - hence NO `_is_valid_grid_sample_rate` gate here,
            # just unconditional snapping.
            for module_type in grid_module_types:
                spec = _GRID_SAMPLE_RATE_SPEC_BY_MODULE[module_type]
                resolved_by_module[module_type] = _nearest_grid_sample_rate(target_sample_rate_hz, spec)

        for device_name, group_channels in ni9213_device_groups(adaptive_channels).items():
            max_rate = max_ni9213_sample_rate_hz(group_channels)
            if target_sample_rate_hz > max_rate + 0.05:
                raise ValueError(
                    f"Das NI9213 ({device_name}, {len(group_channels)} aktive(r) Kanal/"
                    f"Kanäle, Timing-Modus '{group_channels[0].adc_timing_mode}') "
                    f"unterstützt bei dieser Kanalzahl maximal {max_rate:.1f} S/s."
                )

        distinct_rates = set(resolved_by_module.values())
        if len(distinct_rates) <= 1:
            # Regelfall: 0 oder 1 Raster-Modultyp vorhanden -> EXAKT eine
            # gemeinsame "Zielrate"-Gruppe, wie schon immer.
            resolved_rate = next(iter(distinct_rates), target_sample_rate_hz)
            groups.append(
                RateGroup(
                    channels=adaptive_channels,
                    resolved_sample_rate_hz=resolved_rate,
                    reason="Zielrate",
                )
            )
        else:
            # >=2 grid module types (e.g. NI9234 + NI9235) snap to different
            # rates for THIS target rate - a shared task would silently
            # clock one of the two modules wrong. Split into separate
            # groups, analogous to the existing fixed-rate split below.
            # Channels without their own grid limit (e.g. NI9215) join the
            # group whose snapped rate is closest to the raw target rate -
            # a deliberate but not hardware-enforced choice (the module
            # tolerates any clock rate).
            per_module: dict[ModuleType, list[Channel]] = {}
            for ch in adaptive_channels:
                key = (
                    ch.module_type
                    if ch.module_type in resolved_by_module
                    else min(
                        grid_module_types,
                        key=lambda m: abs(resolved_by_module[m] - target_sample_rate_hz),
                    )
                )
                per_module.setdefault(key, []).append(ch)
            for module_type in grid_module_types:
                rate = resolved_by_module[module_type]
                group_channels = per_module.get(module_type, [])
                module_names = sorted({ch.module_type.value for ch in group_channels})
                groups.append(
                    RateGroup(
                        channels=group_channels,
                        resolved_sample_rate_hz=rate,
                        reason=f"{'/'.join(module_names)} (Raster {rate:.1f} S/s)",
                    )
                )

    fixed_groups: dict[float, list[Channel]] = {}
    for ch in fixed_channels:
        fixed_groups.setdefault(_FIXED_SAMPLE_RATE_HZ_BY_MODULE[ch.module_type], []).append(ch)

    for rate, group_channels in fixed_groups.items():
        module_names = sorted({ch.module_type.value for ch in group_channels})
        groups.append(
            RateGroup(
                channels=group_channels,
                resolved_sample_rate_hz=rate,
                reason=f"{'/'.join(module_names)} (feste {rate:.1f} S/s)",
            )
        )

    return groups


class StorageFormat(str, Enum):
    """Storage formats supported by the application for measurement data."""

    PARQUET = "parquet"
    CSV = "csv"


class RecordingStopUnit(str, Enum):
    """Unit for the configured recording limit (see
    `MeasurementConfig.recording_stop_value`/`recording_unlimited`)."""

    SAMPLES = "samples"
    SECONDS = "seconds"
    MINUTES = "minutes"
    HOURS = "hours"


# Conversion factor to seconds per time unit - SAMPLES deliberately not
# included, since that compares sample counts rather than seconds (see
# `MeasurementConfig.is_recording_limit_reached`).
_RECORDING_STOP_UNIT_TO_SECONDS: dict[RecordingStopUnit, float] = {
    RecordingStopUnit.SECONDS: 1.0,
    RecordingStopUnit.MINUTES: 60.0,
    RecordingStopUnit.HOURS: 3600.0,
}


class TriggerKind(str, Enum):
    """Kind of a single trigger condition (see `TriggerCondition`).

    NONE (default) = no automatic condition (manual behavior).
    THRESHOLD/SERIAL fire automatically as soon as the respective
    configured condition occurs - the "armed" state (hardware already
    running, waiting for the start condition) lives in
    `gui/live_view.py::LiveView.enter_armed_state`.
    """

    NONE = "none"
    THRESHOLD = "threshold"
    SERIAL = "serial"


class TriggerDirection(str, Enum):
    """Comparison direction of the threshold trigger (see
    `TriggerCondition.threshold_direction`)."""

    RISES_ABOVE = "rises_above"
    FALLS_BELOW = "falls_below"
    ABS_EXCEEDS = "abs_exceeds"


@dataclass
class TriggerCondition:
    """A single trigger condition - used for both the start and the stop
    of a measurement (see `TriggerConfig.start`/`TriggerConfig.stop`),
    each independently configurable.

    Attributes:
        kind: Kind of condition.
        threshold_channel_hardware_id: Hardware channel
            (`Channel.hardware_channel`) of the channel to monitor - only
            relevant for `kind=THRESHOLD`.
        threshold_value: Threshold value in the channel's physical unit.
        threshold_direction: Comparison direction (see `TriggerDirection`).
        serial_port: Serial interface (e.g. "COM3") - only relevant for
            `kind=SERIAL`.
        serial_baud_rate: Baud rate of the serial connection.
        serial_expected_message: Exact byte/text signal whose receipt
            fires the condition (not just any byte) - see
            `gui/serial_trigger.py::SerialTriggerListener`.
    """

    kind: TriggerKind = TriggerKind.NONE
    threshold_channel_hardware_id: str = ""
    threshold_value: float = 0.0
    threshold_direction: TriggerDirection = TriggerDirection.RISES_ABOVE
    serial_port: str = ""
    serial_baud_rate: int = 9600
    serial_expected_message: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "threshold_channel_hardware_id": self.threshold_channel_hardware_id,
            "threshold_value": self.threshold_value,
            "threshold_direction": self.threshold_direction.value,
            "serial_port": self.serial_port,
            "serial_baud_rate": self.serial_baud_rate,
            "serial_expected_message": self.serial_expected_message,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TriggerCondition":
        return cls(
            kind=TriggerKind(data.get("kind", TriggerKind.NONE.value)),
            threshold_channel_hardware_id=data.get("threshold_channel_hardware_id", ""),
            threshold_value=data.get("threshold_value", 0.0),
            threshold_direction=TriggerDirection(
                data.get("threshold_direction", TriggerDirection.RISES_ABOVE.value)
            ),
            serial_port=data.get("serial_port", ""),
            serial_baud_rate=data.get("serial_baud_rate", 9600),
            serial_expected_message=data.get("serial_expected_message", ""),
        )


@dataclass
class TriggerConfig:
    """Configuration for automatic measurement start AND/OR stop.

    Deliberately a separate, nested dataclass rather than flat fields on
    `MeasurementConfig`.

    `start.kind == NONE` = manual start (clicking "Start Measurement",
    the previous default behavior). `stop.kind == NONE` = no trigger
    stop - the existing, separate recording limit
    (`MeasurementConfig.recording_unlimited`/`recording_stop_value`/
    `recording_stop_unit`) and the manual stop button keep working
    INDEPENDENTLY of that (whichever fires first stops the measurement -
    the same "or" relationship that already existed between the manual
    stop and the recording limit).

    Attributes:
        start: Condition for the automatic start.
        stop: Condition for the automatic stop.
        pretrigger_seconds: How many seconds BEFORE the start-trigger
            instant should additionally be recorded retroactively (like
            an oscilloscope trigger) - only relevant for
            `start.kind=THRESHOLD`, see
            `core/ringbuffer.py::RingBuffer.register_reader`. The stop
            deliberately has NO pre-roll - a stop trigger simply ends the
            recording at the instant it fires.
        auto_rearm: Whether, after EVERY stop (manual, via trigger, or
            recording limit), a new measurement with the same
            configuration is started automatically instead of waiting
            for another manual click on "Start Measurement" - this is
            what turns `start`/`stop` into a true, unattended, repeating
            trigger cycle (see
            `gui/main_window.py::_on_stop_measurement`). Only relevant
            when at least `start.kind` or `stop.kind` != NONE.
    """

    start: TriggerCondition = field(default_factory=TriggerCondition)
    stop: TriggerCondition = field(default_factory=TriggerCondition)
    pretrigger_seconds: float = 5.0
    auto_rearm: bool = False

    def to_dict(self) -> dict:
        return {
            "start": self.start.to_dict(),
            "stop": self.stop.to_dict(),
            "pretrigger_seconds": self.pretrigger_seconds,
            "auto_rearm": self.auto_rearm,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TriggerConfig":
        return cls(
            start=TriggerCondition.from_dict(data.get("start", {}) or {}),
            stop=TriggerCondition.from_dict(data.get("stop", {}) or {}),
            pretrigger_seconds=data.get("pretrigger_seconds", 5.0),
            auto_rearm=data.get("auto_rearm", False),
        )


@dataclass
class Channel:
    """Represents a single measurement channel.

    Attributes:
        hardware_channel: Physical hardware channel, e.g. "cDAQ1Mod1/ai0".
        display_name: Freely choosable display name for GUI and analysis,
            e.g. "Force Cylinder 1".
        unit: Physical unit of the scaled value, e.g. "N", "m/s^2".
        scale: Scaling factor of the linear transformation.
        offset: Offset of the linear transformation.
        signal_type: Physical signal type (voltage, IEPE acceleration, ...).
        module_type: Module the channel is on (NI9215, NI9234, ...).
        enabled: Whether the channel is active for the next measurement.
        min_range: Optional lower measurement range (e.g. -10.0 V for NI9215).
        max_range: Optional upper measurement range (e.g. +10.0 V for NI9215).
        sensitivity_mv_per_unit: Sensor sensitivity in mV/unit, relevant
            for IEPE acceleration sensors (NI9234).
        thermocouple_type: Thermocouple type (e.g. "K", "J", "T", ...),
            relevant for thermocouple channels (NI9210/NI9213), see
            `THERMOCOUPLE_TYPES`.
        strain_gage_factor: k-factor of the connected strain gauge
            (typically ~2.0), relevant for NI9235 channels. `None` = not
            set (analogous to `sensitivity_mv_per_unit`).
        strain_bridge_type: Quarter-bridge variant (see
            `NI9235_BRIDGE_TYPES`) - depends on the physical wiring
            (with/without dummy gage), relevant for NI9235 channels.
        lead_wire_resistance_ohm: Lead wire resistance in Ω to compensate
            for cable length (see NI9235 "Lead Wire Desensitization"),
            relevant for NI9235 channels. 0.0 = no compensation (default).
        cal_point1_measured / cal_point1_reference: First reference point
            of an optional 2-point calibration (measured raw value vs.
            known reference value, e.g. ice point 0 °C for a
            thermocouple) - `None` as long as uncalibrated. Stored
            together with `cal_point2_*` only for traceability;
            `scale`/`offset` remain the values actually applied (see
            `gui/widgets/channel_table.py::TwoPointCalibrationDialog`).
        cal_point2_measured / cal_point2_reference: Second reference
            point of the 2-point calibration, e.g. boiling point 100 °C.
        adc_timing_mode: ADC timing mode (see `ADC_TIMING_MODES`), ONLY
            available in hardware on the NI9213 (NI9210 has a fixed
            sample rate). Per nidaqmx, must be identical for all channels
            of the same physical module - the channel table therefore
            automatically propagates a change to all channels of the same
            module, see `gui/widgets/channel_table.py`.
        plot_color: Individual curve color in the live view (e.g.
            "#64b5f6"), `None` = theme default color (see
            `gui/live_view.py::ChannelDisplayDialog`).
        plot_background: Individual plot background color, `None` =
            theme default background.
        plot_grid_color: Individual gridline color, `None` = theme
            default (foreground color, see
            `gui/live_view.py::_channel_grid_color`).
        plot_y_min: Lower Y-axis display range of the live view. Unlike
            `min_range`/`max_range` (hardware measurement range), a
            purely display-related setting - `None` falls back to
            `min_range` or -10.0.
        plot_y_max: Upper Y-axis display range of the live view, `None`
            falls back to `max_range` or 10.0.
        plot_autoscale: Whether the Y axis automatically switches to the
            actual value range when `plot_y_min`/`plot_y_max` is
            exceeded/undershot - if `False`, the fixed range always
            stays active.
        plot_show_x_label: Whether the X axis TITLE ("Time [s]") is
            drawn on the plot. Only the title text - tick marks, tick
            numbers and the grid stay untouched. Off gives the space
            back to the plot area, which is what makes it worth having
            with many subplots in one grid.
        plot_show_y_label: Same for the Y axis title (channel name plus
            unit, see `gui/live_view.py::_channel_axis_label`).
        plot_visible: Whether the channel is shown as its own subplot in
            the live view. Affects ONLY the display, not
            acquisition/storage - a channel with `plot_visible=False` is
            still recorded normally, it just doesn't appear in the live
            view grid (see `gui/live_view.py::_rebuild_plots`).

    The physical conversion is computed as:
        physikalischer_wert = rohwert * scale + offset
    """

    hardware_channel: str
    display_name: str
    unit: str = ""
    scale: float = 1.0
    offset: float = 0.0
    signal_type: SignalType = SignalType.VOLTAGE
    module_type: ModuleType = ModuleType.NI9215
    enabled: bool = True
    min_range: Optional[float] = -10.0
    max_range: Optional[float] = 10.0
    sensitivity_mv_per_unit: Optional[float] = None
    thermocouple_type: str = "K"
    strain_gage_factor: Optional[float] = None
    strain_bridge_type: str = "QUARTER_BRIDGE_I"
    lead_wire_resistance_ohm: float = 0.0
    cal_point1_measured: Optional[float] = None
    cal_point1_reference: Optional[float] = None
    cal_point2_measured: Optional[float] = None
    cal_point2_reference: Optional[float] = None
    adc_timing_mode: str = "HIGH_RESOLUTION"
    plot_color: Optional[str] = None
    plot_background: Optional[str] = None
    plot_grid_color: Optional[str] = None
    plot_y_min: Optional[float] = None
    plot_y_max: Optional[float] = None
    plot_autoscale: bool = True
    plot_time_window_seconds: float = 5.0
    # Axis TITLES ("Time [s]" / channel name + unit), switchable
    # separately per axis - ticks and grid are unaffected. Default on,
    # i.e. the previous appearance (see
    # `gui/live_view.py::ChannelPlotSettingsDialog`).
    plot_show_x_label: bool = True
    plot_show_y_label: bool = True
    # Whether the actual curve trace is shown (main grid AND own window) -
    # independent of `plot_show_value` below: both switched off together
    # shows nothing at all (see `plot_visible` for that). Default is ONLY
    # the chart (see `plot_show_value`), turning ONLY this field off shows
    # exclusively the numeric value without a chart (see
    # `gui/live_view.py::ChannelDisplayDialog`/`_rebuild_plots`).
    plot_show_graph: bool = True
    # Large, current value readout next to the subplot in the main grid
    # (see `gui/live_view.py::ChannelDisplayDialog`/`_rebuild_plots`) -
    # OFF by default: deliberately opt-in per channel instead of costing
    # unnecessary space up front with many channels.
    plot_show_value: bool = False
    # Number of integer digits for `plot_show_value` - if a reading
    # doesn't fit, a placeholder of hash marks is shown (like DIAdem/
    # LabVIEW digital displays) instead of a misleadingly truncated
    # number, rather than continuously readjusting the display width. In
    # the dialog, editable together with `plot_value_decimal_digits` as
    # ONE format pattern (e.g. "000.0000"), see
    # `gui/live_view.py::ChannelDisplayDialog`.
    plot_value_integer_digits: int = 3
    # Number of decimal digits - see `plot_value_integer_digits`.
    plot_value_decimal_digits: int = 3
    plot_visible: bool = True
    # Shows the channel in its own window (instead of the live view's
    # main grid) (see `gui/live_view.py::ChannelPopoutWindow`) - not
    # mutually exclusive with `plot_visible`: a channel is visible either
    # nowhere (plot_visible=False), in the main grid (plot_popout=False),
    # or in its own window (plot_popout=True), never in two places at once.
    plot_popout: bool = False
    # Last known position/size of the own window (see
    # `gui/live_view.py::ChannelPopoutWindow`) - continuously updated
    # while the window is open, and reused on the next app start/
    # measurement start so the arrangement is preserved. `None` (all
    # four) as long as the window has never been moved/resized - the
    # default position/size from `ChannelPopoutWindow.__init__` then
    # applies. Checked against the CURRENTLY connected screens when
    # restoring (see `gui/theme.py::is_position_on_screen`), so a window
    # that was last on a second monitor that has since been removed
    # doesn't become unreachable.
    plot_popout_x: Optional[int] = None
    plot_popout_y: Optional[int] = None
    plot_popout_width: Optional[int] = None
    plot_popout_height: Optional[int] = None

    def to_physical(self, raw_value: float) -> float:
        """Wandelt einen Rohwert in den skalierten physikalischen Wert um."""
        return raw_value * self.scale + self.offset

    def to_dict(self) -> dict:
        """Serialisiert den Kanal in ein JSON-kompatibles Dictionary."""
        return {
            "hardware_channel": self.hardware_channel,
            "display_name": self.display_name,
            "unit": self.unit,
            "scale": self.scale,
            "offset": self.offset,
            "signal_type": self.signal_type.value,
            "module_type": self.module_type.value,
            "enabled": self.enabled,
            "min_range": self.min_range,
            "max_range": self.max_range,
            "sensitivity_mv_per_unit": self.sensitivity_mv_per_unit,
            "thermocouple_type": self.thermocouple_type,
            "strain_gage_factor": self.strain_gage_factor,
            "strain_bridge_type": self.strain_bridge_type,
            "lead_wire_resistance_ohm": self.lead_wire_resistance_ohm,
            "cal_point1_measured": self.cal_point1_measured,
            "cal_point1_reference": self.cal_point1_reference,
            "cal_point2_measured": self.cal_point2_measured,
            "cal_point2_reference": self.cal_point2_reference,
            "adc_timing_mode": self.adc_timing_mode,
            "plot_color": self.plot_color,
            "plot_background": self.plot_background,
            "plot_grid_color": self.plot_grid_color,
            "plot_y_min": self.plot_y_min,
            "plot_y_max": self.plot_y_max,
            "plot_autoscale": self.plot_autoscale,
            "plot_time_window_seconds": self.plot_time_window_seconds,
            "plot_show_x_label": self.plot_show_x_label,
            "plot_show_y_label": self.plot_show_y_label,
            "plot_show_graph": self.plot_show_graph,
            "plot_show_value": self.plot_show_value,
            "plot_value_integer_digits": self.plot_value_integer_digits,
            "plot_value_decimal_digits": self.plot_value_decimal_digits,
            "plot_visible": self.plot_visible,
            "plot_popout": self.plot_popout,
            "plot_popout_x": self.plot_popout_x,
            "plot_popout_y": self.plot_popout_y,
            "plot_popout_width": self.plot_popout_width,
            "plot_popout_height": self.plot_popout_height,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Channel":
        """Creates a Channel from a dictionary (e.g. from JSON)."""
        return cls(
            hardware_channel=data["hardware_channel"],
            display_name=data.get("display_name", data["hardware_channel"]),
            unit=data.get("unit", ""),
            scale=data.get("scale", 1.0),
            offset=data.get("offset", 0.0),
            signal_type=SignalType(data.get("signal_type", SignalType.VOLTAGE.value)),
            module_type=ModuleType(data.get("module_type", ModuleType.NI9215.value)),
            enabled=data.get("enabled", True),
            min_range=data.get("min_range", -10.0),
            max_range=data.get("max_range", 10.0),
            sensitivity_mv_per_unit=data.get("sensitivity_mv_per_unit"),
            thermocouple_type=data.get("thermocouple_type", "K"),
            strain_gage_factor=data.get("strain_gage_factor"),
            strain_bridge_type=data.get("strain_bridge_type", "QUARTER_BRIDGE_I"),
            lead_wire_resistance_ohm=data.get("lead_wire_resistance_ohm", 0.0),
            cal_point1_measured=data.get("cal_point1_measured"),
            cal_point1_reference=data.get("cal_point1_reference"),
            cal_point2_measured=data.get("cal_point2_measured"),
            cal_point2_reference=data.get("cal_point2_reference"),
            adc_timing_mode=data.get("adc_timing_mode", "HIGH_RESOLUTION"),
            plot_color=data.get("plot_color"),
            plot_background=data.get("plot_background"),
            plot_grid_color=data.get("plot_grid_color"),
            plot_y_min=data.get("plot_y_min"),
            plot_y_max=data.get("plot_y_max"),
            plot_autoscale=data.get("plot_autoscale", True),
            plot_time_window_seconds=max(
                0.1, float(data.get("plot_time_window_seconds", 5.0))
            ),
            plot_show_x_label=data.get("plot_show_x_label", True),
            plot_show_y_label=data.get("plot_show_y_label", True),
            plot_show_graph=data.get("plot_show_graph", True),
            plot_show_value=data.get("plot_show_value", False),
            plot_value_integer_digits=max(
                1, int(data.get("plot_value_integer_digits", 3))
            ),
            plot_value_decimal_digits=max(
                0, int(data.get("plot_value_decimal_digits", 3))
            ),
            plot_visible=data.get("plot_visible", True),
            plot_popout=data.get("plot_popout", False),
            plot_popout_x=cls._optional_int(data.get("plot_popout_x")),
            plot_popout_y=cls._optional_int(data.get("plot_popout_y")),
            plot_popout_width=cls._optional_int(data.get("plot_popout_width")),
            plot_popout_height=cls._optional_int(data.get("plot_popout_height")),
        )

    @staticmethod
    def _optional_int(value) -> Optional[int]:
        """Robustly converts a value loaded from JSON (can be `None`,
        `int`, or `float`) to `Optional[int]` - see
        `plot_popout_x`/`plot_popout_y`/`plot_popout_width`/
        `plot_popout_height`."""
        return None if value is None else int(value)


@dataclass
class DeviceInfo:
    """Describes a detected physical NI cDAQ module/device.

    Attributes:
        device_name: Device name assigned by nidaqmx, e.g. "cDAQ1Mod1".
        product_type: Product designation, e.g. "NI 9215".
        module_type: Mapped ModuleType, if supported by the system.
        num_channels: Number of physically available ANALOG INPUT
            channels on the module (this app currently supports analog
            input only) - 0 even for a physically present module that
            only has other channel types (e.g. a pure analog-output
            module like the NI9263). See `has_any_channels` to
            distinguish SUCH modules from an empty chassis entry with no
            channels at all.
        has_any_channels: Whether the device has ANY channel at all -
            analog in/out, digital in/out, or counter - regardless of
            whether this app supports that particular channel type. A
            pure chassis controller entry (e.g. "cDAQ1", with no channels
            of its own) has `False`; an inserted module - even one this
            app doesn't (yet) support, like a pure AO module - has
            `True`. Used to report such modules as "detected but not
            supported" despite `num_channels == 0`, instead of silently
            hiding them like an empty chassis entry (see
            `gui/setup_view.py::set_discovered_devices`).
        is_connected: Whether the device actually responded to a
            hardware probe during discovery - as opposed to merely being
            present in the NI-DAQmx configuration database. Everything
            else in this dataclass comes from that database, which keeps
            a once-configured device (in particular a RESERVED network
            cDAQ chassis) listed with its full channel tree even after
            its cable has been pulled. Without this flag such a device
            would keep showing up as selectable although no measurement
            can be started with it (see
            `hardware/nidaq_device.py::_is_device_connected`).
    """

    device_name: str
    product_type: str
    module_type: Optional[ModuleType] = None
    num_channels: int = 0
    has_any_channels: bool = False
    # List of physical channel names, e.g. ["cDAQ1Mod1/ai0", ...]
    physical_channels: list[str] = field(default_factory=list)
    # Defaults to True so that callers constructing DeviceInfo without a
    # hardware probe (tests, metadata) keep the previous behavior.
    is_connected: bool = True


@dataclass
class MeasurementConfig:
    """Configuration for a single measurement/recording.

    Attributes:
        name: Identifier of the measurement, e.g. "measurement_001".
        sample_rate_hz: Target rate in Hz. Applies directly to all
            channels except the NI9210 (fixed 14 S/s, see
            `resolve_rate_groups`).
        channels: List of active channels for this measurement.
        storage_format: Chosen storage format (Parquet/CSV).
        samples_per_read: Block size per read from the DAQ device.
        ring_buffer_size: Capacity of the ring buffer in samples per channel.
        recording_unlimited: True (default/previous behavior) = the
            measurement runs until the user manually stops it or storage
            space runs out. False = the measurement stops automatically
            once `recording_stop_value`/`recording_stop_unit` is reached
            (see `is_recording_limit_reached`).
        recording_stop_value: Limit in the unit `recording_stop_unit` -
            only relevant if `recording_unlimited` is False.
        recording_stop_unit: Unit of the limit (samples or time).
        trigger: Configuration for automatic measurement start AND/OR
            stop (see `TriggerConfig`) - the recording limit above
            continues to apply independently and in addition (whichever
            fires first stops it).
    """

    name: str
    sample_rate_hz: float
    channels: list[Channel] = field(default_factory=list)
    storage_format: StorageFormat = StorageFormat.PARQUET
    samples_per_read: int = 1000
    ring_buffer_size: int = 100_000
    save_to_disk: bool = True
    recording_unlimited: bool = True
    recording_stop_value: float = 0.0
    recording_stop_unit: RecordingStopUnit = RecordingStopUnit.SAMPLES
    trigger: TriggerConfig = field(default_factory=TriggerConfig)

    def __post_init__(self) -> None:
        if not self.recording_unlimited and self.recording_stop_value <= 0:
            raise ValueError(
                "recording_stop_value muss bei begrenzten Messungen größer als 0 sein."
            )
        # Only raises ValueError for intrinsically unreachable rates
        # (NI9234 grid, NI9213 maximum rate) - an NI9210 combined with
        # faster modules is NO LONGER an error, it results in two
        # separate sampling groups (see `resolve_rate_groups` and
        # `core/controller.py::start_measurement`).
        resolve_rate_groups(self.active_channels(), self.sample_rate_hz)

    def active_channels(self) -> list[Channel]:
        """Returns only the enabled channels."""
        return [ch for ch in self.channels if ch.enabled]

    def target_recording_stop_samples(self) -> int:
        """Converts the configured limit (samples or time) once into a
        target sample count relative to `sample_rate_hz`.

        Samples are the most reliable basis for a recording limit: they
        are clocked by the DAQ module's hardware sample clock, not by
        software wall-clock time (`datetime.now()`) - a limit can
        therefore be evaluated reliably regardless of GUI/thread delays
        (see `is_recording_limit_reached`).
        """
        if self.recording_stop_unit == RecordingStopUnit.SAMPLES:
            return int(round(self.recording_stop_value))
        seconds_per_unit = _RECORDING_STOP_UNIT_TO_SECONDS[self.recording_stop_unit]
        return int(round(self.recording_stop_value * seconds_per_unit * self.sample_rate_hz))

    def is_recording_limit_reached(self, samples_acquired: int) -> bool:
        """Checks whether the configured recording limit has been reached.

        Central place for the limit logic (samples vs. time units, see
        `target_recording_stop_samples`), so `gui/live_view.py` only has
        to supply the actually acquired sample count. Always returns
        False when `recording_unlimited=True` (previous default
        behavior: run until manually stopped or the disk is full).
        """
        if self.recording_unlimited:
            return False
        return samples_acquired >= self.target_recording_stop_samples()

    def to_dict(self) -> dict:
        """Serialisiert die Konfiguration in ein JSON-kompatibles Dictionary."""
        return {
            "name": self.name,
            "sample_rate_hz": self.sample_rate_hz,
            "channels": [ch.to_dict() for ch in self.channels],
            "storage_format": self.storage_format.value,
            "samples_per_read": self.samples_per_read,
            "ring_buffer_size": self.ring_buffer_size,
            "save_to_disk": self.save_to_disk,
            "recording_unlimited": self.recording_unlimited,
            "recording_stop_value": self.recording_stop_value,
            "recording_stop_unit": self.recording_stop_unit.value,
            "trigger": self.trigger.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MeasurementConfig":
        """Creates a MeasurementConfig from a dictionary (e.g. from JSON)."""
        return cls(
            name=data["name"],
            sample_rate_hz=data.get("sample_rate_hz", 1000.0),
            channels=[Channel.from_dict(ch) for ch in data.get("channels", [])],
            storage_format=StorageFormat(
                data.get("storage_format", StorageFormat.PARQUET.value)
            ),
            samples_per_read=data.get("samples_per_read", 1000),
            ring_buffer_size=data.get("ring_buffer_size", 100_000),
            save_to_disk=data.get("save_to_disk", True),
            recording_unlimited=data.get("recording_unlimited", True),
            recording_stop_value=data.get("recording_stop_value", 0.0),
            recording_stop_unit=RecordingStopUnit(
                data.get("recording_stop_unit", RecordingStopUnit.SAMPLES.value)
            ),
            trigger=TriggerConfig.from_dict(data.get("trigger", {})),
        )


@dataclass
class MeasurementSession:
    """Represents a concrete, running or completed measurement.

    Deliberately separates the static configuration (`MeasurementConfig`)
    from the runtime/result information of a recording (start/end time,
    path).

    Attributes:
        config: The measurement configuration used.
        start_time: Instant the measurement started.
        end_time: Instant the measurement ended (None while it's running).
        file_path: Path to the saved measurement file, once available.
    """

    config: MeasurementConfig
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    file_path: Optional[str] = None

    @property
    def is_running(self) -> bool:
        """True as long as the measurement has started but not ended."""
        return self.start_time is not None and self.end_time is None

    @property
    def duration_seconds(self) -> Optional[float]:
        """Duration of the measurement in seconds, if start and end times are available."""
        if self.start_time is None:
            return None
        end = self.end_time or datetime.now()
        return (end - self.start_time).total_seconds()
