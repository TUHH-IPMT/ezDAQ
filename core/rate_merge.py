"""
core/rate_merge.py

Merges raw blocks read each cycle from multiple device groups with
DIFFERENT, intrinsically incompatible sample rates (see
`data/models.py::resolve_rate_groups`) into a single, jointly clocked
block, before it is written to the (single) ring buffer.

The normal case (all devices in ONE group, e.g. two NI9234 or
NI9234+NI9215) doesn't need this file - for that, the previous, direct
read path via `_read_group_block()` remains unchanged (see
`core/acquisition.py`). `RateMerger` is used exclusively when
`resolve_rate_groups()` has returned more than one group (currently:
NI9210 together with at least one other module).

Algorithm (zero-order hold/forward-fill of the slower group(s) onto the
clock grid of the fastest group):
    For the fastest group, exactly `samples_to_read` samples are read
    per cycle as usual. For each slower group, a NEW READ IS NOT
    performed on every cycle (at ~14 S/s vs. ~1651.6 S/s, on average only
    every ~118 fast ticks does a new real sample arrive) - instead, the
    number of new samples of the slower group DUE this cycle is computed
    from the GLOBAL count of fast ticks since measurement start:

        due(t) = floor(t * slow_rate / fast_rate)

    IMPORTANT: "due according to this formula" does NOT mean "actually
    already delivered by the driver" - the formula knows nothing about
    whether the hardware has already finished its conversion for this
    sample (which, at 14 S/s, can take up to ~71ms). A blocking read for
    exactly the due count (an earlier version of this file) could
    therefore block for up to a full conversion period of the slow
    group, while the fast group kept running in parallel - reproduced on
    real hardware: after ~10-20s this leads to an "application is not
    able to keep up with the hardware acquisition" error on the FAST
    task, because its own (limited) driver buffer overflows during the
    block.

    Therefore, per cycle NEVER more is requested from the slow group
    than `BaseDevice.available_samples()` (a non-blocking status query)
    actually reports right now - if a sample that is due according to
    `due()` isn't there yet, the last known value is simply held longer
    instead of waiting; the backlog is tracked via `self._delivered`
    (the number actually delivered per group, NOT the same as `due()`)
    and automatically caught up in a later cycle, as soon as the
    hardware actually delivers the samples. Based on absolute tick
    indices (rather than incremental rounding), this avoids drift over
    the course of a long measurement (a Bresenham-like approach) -
    validated against real hardware over 45s/1850 cycles (difference
    from the theoretical due() value < 1 sample).
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np

from hardware.base_device import BaseDevice


@dataclass
class DeviceGroup:
    """Bundles the devices of ONE `data.models.RateGroup`, after they have
    been created and configured via `core.measurement.create_devices()`.

    With more than one device, they internally share a `NIDAQSharedTask`
    (see `hardware/nidaq_device.py`) - exactly as in today's single-group
    case.
    """

    devices: list[BaseDevice]
    resolved_sample_rate_hz: float

    @property
    def channel_count(self) -> int:
        return sum(len(d.active_channels) for d in self.devices)


def _read_group_block(devices: list[BaseDevice], samples_to_read: int, timeout: float) -> np.ndarray:
    """Reads ONE combined raw data block (all devices of this one group,
    joined along the channel axis) - identical logic to the previous
    `AcquisitionThread._read_blocks_from_devices`, just applied to a
    single group instead of "all devices of the measurement", and
    already returned fully concatenated.
    """
    num_channels = sum(len(d.active_channels) for d in devices)
    if not devices or samples_to_read <= 0:
        return np.empty((num_channels, 0), dtype=np.float64)

    shared_devices = [d for d in devices if getattr(d, "_shared_task", None) is not None]
    if shared_devices:
        shared_block = shared_devices[0].read_shared_block(samples_to_read, timeout=timeout)
        return np.concatenate([d.read_from_shared_block(shared_block) for d in devices], axis=0)

    with ThreadPoolExecutor(max_workers=max(1, len(devices))) as executor:
        futures = [executor.submit(d.read, samples_to_read, timeout=timeout) for d in devices]
        return np.concatenate([f.result() for f in futures], axis=0)


def _group_available_samples(devices: list[BaseDevice]) -> int:
    """Smallest currently available (non-blocking-queried) sample count
    across all devices of ONE group - with a shared task (>1 device in
    the group) all report the same value anyway, `min()` here is purely
    defensive."""
    if not devices:
        return 0
    return min(d.available_samples() for d in devices)


class RateMerger:
    """Merges one fast group with one or more slower groups into a
    jointly clocked block (see module docstring).
    """

    def __init__(self, groups: list[DeviceGroup], read_timeout_seconds: float) -> None:
        if len(groups) < 2:
            raise ValueError("RateMerger wird nur für >= 2 Gruppen benötigt.")
        self._groups = groups
        self._timeout = read_timeout_seconds
        self._fast_index = max(range(len(groups)), key=lambda i: groups[i].resolved_sample_rate_hz)
        # Last known value per slow group (zero-order-hold state),
        # initially 0.0 - until the first real sample, this matches the
        # ring buffer's own zero-initialized state anyway.
        self._last_known: dict[int, np.ndarray] = {
            i: np.zeros((groups[i].channel_count, 1), dtype=np.float64)
            for i in range(len(groups))
            if i != self._fast_index
        }
        # Number of ACTUALLY read (not just due() according to due())
        # samples per slower group since measurement start - can lag
        # behind due() if the driver hasn't delivered a due sample yet
        # (see module docstring); the backlog is automatically caught up
        # in a later cycle.
        self._delivered: dict[int, int] = {
            i: 0 for i in range(len(groups)) if i != self._fast_index
        }
        self._fast_ticks_emitted = 0

    def read_merged_block(self, samples_to_read: int) -> np.ndarray:
        """Reads exactly `samples_to_read` samples relative to the
        FASTEST group and returns a combined
        `(total_channel_count, samples_to_read)` block, with channel
        order matching `self._groups` (group by group, see
        `resolve_rate_groups`).
        """
        fast_group = self._groups[self._fast_index]
        start_idx = self._fast_ticks_emitted
        end_idx = start_idx + samples_to_read

        blocks: list[np.ndarray] = [None] * len(self._groups)  # type: ignore[list-item]
        blocks[self._fast_index] = _read_group_block(fast_group.devices, samples_to_read, self._timeout)

        for i, group in enumerate(self._groups):
            if i == self._fast_index:
                continue
            delivered_before = self._delivered[i]
            due_after = math.floor(end_idx * group.resolved_sample_rate_hz / fast_group.resolved_sample_rate_hz)
            owed = due_after - delivered_before
            # NEVER blockingly request more than the driver actually has
            # RIGHT NOW (see module docstring) - otherwise a single
            # sample that is due according to due(), but not yet
            # finished being converted by the hardware, would block the
            # entire cycle, while the fast group keeps running in
            # parallel and its own (limited) driver buffer overflows.
            num_to_read = min(owed, _group_available_samples(group.devices)) if owed > 0 else 0
            new_block = _read_group_block(group.devices, num_to_read, self._timeout)
            delivered_after = delivered_before + num_to_read
            self._delivered[i] = delivered_after

            extended = np.concatenate([self._last_known[i], new_block], axis=1)
            local_ticks = start_idx + np.arange(1, samples_to_read + 1)
            raw_counts = np.floor(
                local_ticks * group.resolved_sample_rate_hz / fast_group.resolved_sample_rate_hz
            ).astype(np.int64)
            # Clamp to actually delivered samples (0..num_to_read): if a
            # sample is due according to the formula but not yet
            # delivered, the last known value is simply held longer
            # instead of waiting - the resulting backlog sits in
            # `due_after - delivered_after` and is automatically caught
            # up in the next cycle (see above).
            counts = np.clip(raw_counts - delivered_before, 0, num_to_read)
            # counts runs monotonically from 0..num_to_read - fancy
            # indexing on `extended` directly yields the vectorized
            # forward-fill block, no Python loop over samples needed.
            filled = extended[:, counts]
            self._last_known[i] = filled[:, -1:]
            blocks[i] = filled

        self._fast_ticks_emitted = end_idx
        return np.concatenate(blocks, axis=0)
