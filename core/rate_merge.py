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
    der langsamen Gruppe in diesem Zyklus FÄLLIG sind:

        due(t) = floor(t * langsame_rate / schnelle_rate)

    WICHTIG: "fällig laut dieser Formel" heißt NICHT "vom Treiber schon
    tatsächlich geliefert" - die Formel weiß nichts davon, ob die
    Hardware ihre (bei 14 S/s bis zu ~71ms lange) Wandlung für dieses
    Sample bereits abgeschlossen hat. Ein blockierender Read über genau
    die fällige Anzahl (frühere Version dieser Datei) konnte deshalb bis
    zu einer vollen Wandlungsperiode der langsamen Gruppe blockieren,
    während die schnelle Gruppe parallel weiterlief - an echter Hardware
    reproduziert: das führt nach ~10-20s zu einem "application is not
    able to keep up with the hardware acquisition"-Fehler auf dem
    SCHNELLEN Task, weil dessen eigener (begrenzter) Treiberpuffer
    während der Blockade überläuft.

    Deshalb wird pro Zyklus NIEMALS mehr von der langsamen Gruppe
    angefordert, als `BaseDevice.available_samples()` (nicht-blockierende
    Statusabfrage) gerade wirklich meldet - ist ein laut `due()`
    fälliges Sample noch nicht da, wird der zuletzt bekannte Wert
    einfach länger gehalten statt zu warten; der Rückstand wird über
    `self._delivered` (tatsächlich zugestellte Anzahl je Gruppe, NICHT
    dasselbe wie `due()`) verfolgt und in einem späteren Zyklus
    automatisch nachgeholt, sobald die Hardware die Samples wirklich
    liefert. Auf absoluten Takt-Indizes basierend (statt inkrementeller
    Rundung), vermeidet Drift über eine lange Messung hinweg
    (Bresenham-artiges Verfahren) - an echter Hardware über 45s/1850
    Zyklen validiert (Differenz zum theoretischen due()-Wert < 1 Sample).
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


def _group_available_samples(devices: list[BaseDevice]) -> int:
    """Kleinste über alle Geräte EINER Gruppe aktuell verfügbare
    (nicht-blockierend abgefragte) Samplezahl - bei einem gemeinsamen
    Task (>1 Gerät in der Gruppe) melden ohnehin alle denselben Wert,
    `min()` ist hier nur defensiv."""
    if not devices:
        return 0
    return min(d.available_samples() for d in devices)


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
        # Anzahl TATSÄCHLICH gelesener (nicht nur laut due() fälliger)
        # Samples je langsamerer Gruppe seit Messstart - kann hinter
        # due() zurückbleiben, wenn der Treiber ein fälliges Sample noch
        # nicht geliefert hat (siehe Moduldocstring); der Rückstand wird
        # in einem späteren Zyklus automatisch nachgeholt.
        self._delivered: dict[int, int] = {
            i: 0 for i in range(len(groups)) if i != self._fast_index
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
            delivered_before = self._delivered[i]
            due_after = math.floor(end_idx * group.resolved_sample_rate_hz / fast_group.resolved_sample_rate_hz)
            owed = due_after - delivered_before
            # NIE blockierend mehr anfordern, als der Treiber JETZT schon
            # wirklich hat (siehe Moduldocstring) - sonst blockiert ein
            # einzelnes, laut due() zwar fälliges, aber von der Hardware
            # noch nicht fertig gewandeltes Sample den gesamten Zyklus,
            # während die schnelle Gruppe parallel weiterläuft und ihr
            # eigener (begrenzter) Treiberpuffer überläuft.
            num_to_read = min(owed, _group_available_samples(group.devices)) if owed > 0 else 0
            new_block = _read_group_block(group.devices, num_to_read, self._timeout)
            delivered_after = delivered_before + num_to_read
            self._delivered[i] = delivered_after

            extended = np.concatenate([self._last_known[i], new_block], axis=1)
            local_ticks = start_idx + np.arange(1, samples_to_read + 1)
            raw_counts = np.floor(
                local_ticks * group.resolved_sample_rate_hz / fast_group.resolved_sample_rate_hz
            ).astype(np.int64)
            # Auf tatsächlich zugestellte Samples begrenzen (0..num_to_read):
            # ist ein Sample laut Formel zwar schon fällig, aber noch
            # nicht geliefert, wird der zuletzt bekannte Wert einfach
            # länger gehalten statt zu warten - der dadurch entstehende
            # Rückstand steckt in `due_after - delivered_after` und wird
            # automatisch im nächsten Zyklus nachgeholt (siehe oben).
            counts = np.clip(raw_counts - delivered_before, 0, num_to_read)
            # counts läuft monoton von 0..num_to_read - Fancy-Indexing auf
            # `extended` liefert direkt den vektorisierten
            # Forward-Fill-Block, ein Python-Loop über Samples entfällt.
            filled = extended[:, counts]
            self._last_known[i] = filled[:, -1:]
            blocks[i] = filled

        self._fast_ticks_emitted = end_idx
        return np.concatenate(blocks, axis=0)
