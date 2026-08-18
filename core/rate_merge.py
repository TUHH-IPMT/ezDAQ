"""
core/rate_merge.py

Fasst pro Zyklus gelesene Rohblöcke mehrerer Geräte-Gruppen mit
UNTERSCHIEDLICHEN, intrinsisch unvereinbaren Abtastraten (siehe
`data/models.py::resolve_rate_groups`) zu einem einzigen, gemeinsam
getakteten Block zusammen, bevor dieser in den (einzigen) Ring Buffer
geschrieben wird.

Der Regelfall (alle Geräte in EINER Gruppe, z. B. zwei NI9234 oder
NI9234+NI9215) braucht diese Datei nicht - dafür bleibt der bisherige,
direkte Lesepfad über `_read_group_block()` unverändert bestehen (siehe
`core/acquisition.py`). `RateMerger` kommt ausschließlich zum Einsatz,
wenn `resolve_rate_groups()` mehr als eine Gruppe zurückgegeben hat
(aktuell: NI9210 zusammen mit mindestens einem anderen Modul).

Algorithmus (Zero-Order-Hold/Forward-Fill der langsameren Gruppe(n) auf
das Taktraster der schnellsten Gruppe):
    Für die schnellste Gruppe wird pro Zyklus wie gewohnt genau
    `samples_to_read` Samples gelesen. Für jede langsamere Gruppe wird
    NICHT bei jedem Zyklus neu gelesen (bei ~14 S/s vs. ~1651,6 S/s
    kommt im Schnitt nur alle ~118 schnellen Takte ein neues echtes
    Sample hinzu) - stattdessen wird anhand der GLOBALEN, seit Messstart
    gezählten Anzahl schneller Takte berechnet, wie viele neue Samples
    der langsamen Gruppe in diesem Zyklus fällig sind:

        due(t) = floor(t * langsame_rate / schnelle_rate)

    Die Differenz `due(ende) - due(anfang)` ist die in diesem Zyklus zu
    lesende Anzahl neuer Samples der langsamen Gruppe (kann 0 sein).
    Diese auf absoluten Takt-Indizes basierende Berechnung (statt
    inkrementeller Rundung) vermeidet Drift über eine lange Messung
    hinweg (Bresenham-artiges Verfahren) - an echter Hardware über 45s/
    1850 Zyklen validiert (Differenz zum theoretischen due()-Wert < 1
    Sample).
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np

from hardware.base_device import BaseDevice


@dataclass
class DeviceGroup:
    """Bündelt die Geräte EINER `data.models.RateGroup`, nachdem sie via
    `core.measurement.create_devices()` erzeugt und konfiguriert wurden.

    Bei mehr als einem Gerät teilen sie sich intern einen
    `NIDAQSharedTask` (siehe `hardware/nidaq_device.py`) - genau wie
    heute im Ein-Gruppen-Fall.
    """

    devices: list[BaseDevice]
    resolved_sample_rate_hz: float

    @property
    def channel_count(self) -> int:
        return sum(len(d.active_channels) for d in self.devices)


def _read_group_block(devices: list[BaseDevice], samples_to_read: int, timeout: float) -> np.ndarray:
    """Liest EINEN kombinierten Rohdaten-Block (alle Geräte dieser einen
    Gruppe, entlang der Kanalachse zusammengefügt) - identische Logik zum
    bisherigen `AcquisitionThread._read_blocks_from_devices`, nur auf
    eine einzelne Gruppe statt "alle Geräte der Messung" bezogen, und
    bereits fertig konkateniert zurückgegeben.
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


class RateMerger:
    """Verschmilzt eine schnelle Gruppe mit einer oder mehreren
    langsameren Gruppen zu einem gemeinsam getakteten Block (siehe
    Moduldocstring).
    """

    def __init__(self, groups: list[DeviceGroup], read_timeout_seconds: float) -> None:
        if len(groups) < 2:
            raise ValueError("RateMerger wird nur für >= 2 Gruppen benötigt.")
        self._groups = groups
        self._timeout = read_timeout_seconds
        self._fast_index = max(range(len(groups)), key=lambda i: groups[i].resolved_sample_rate_hz)
        # Letzter bekannter Wert je langsamer Gruppe (Zero-Order-Hold-
        # Zustand), initial 0.0 - bis zum ersten echten Sample entspricht
        # das dem ohnehin nullinitialisierten Ring-Buffer-Zustand.
        self._last_known: dict[int, np.ndarray] = {
            i: np.zeros((groups[i].channel_count, 1), dtype=np.float64)
            for i in range(len(groups))
            if i != self._fast_index
        }
        self._fast_ticks_emitted = 0

    def read_merged_block(self, samples_to_read: int) -> np.ndarray:
        """Liest genau `samples_to_read` Samples bezogen auf die
        SCHNELLSTE Gruppe und liefert einen kombinierten
        `(gesamt_kanalzahl, samples_to_read)`-Block, Kanalreihenfolge wie
        `self._groups` (Gruppe für Gruppe, siehe `resolve_rate_groups`).
        """
        fast_group = self._groups[self._fast_index]
        start_idx = self._fast_ticks_emitted
        end_idx = start_idx + samples_to_read

        blocks: list[np.ndarray] = [None] * len(self._groups)  # type: ignore[list-item]
        blocks[self._fast_index] = _read_group_block(fast_group.devices, samples_to_read, self._timeout)

        for i, group in enumerate(self._groups):
            if i == self._fast_index:
                continue
            due_before = math.floor(start_idx * group.resolved_sample_rate_hz / fast_group.resolved_sample_rate_hz)
            due_after = math.floor(end_idx * group.resolved_sample_rate_hz / fast_group.resolved_sample_rate_hz)
            num_due = due_after - due_before
            new_block = _read_group_block(group.devices, num_due, self._timeout)

            extended = np.concatenate([self._last_known[i], new_block], axis=1)
            local_ticks = start_idx + np.arange(1, samples_to_read + 1)
            counts = (
                np.floor(local_ticks * group.resolved_sample_rate_hz / fast_group.resolved_sample_rate_hz).astype(
                    np.int64
                )
                - due_before
            )
            # counts läuft monoton von 0..num_due - Fancy-Indexing auf
            # `extended` liefert direkt den vektorisierten
            # Forward-Fill-Block, ein Python-Loop über Samples entfällt.
            filled = extended[:, counts]
            self._last_known[i] = filled[:, -1:]
            blocks[i] = filled

        self._fast_ticks_emitted = end_idx
        return np.concatenate(blocks, axis=0)
