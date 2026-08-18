"""
core/acquisition.py

DAQ-Thread: liest zyklisch von allen konfigurierten Hardware-Geräten und
schreibt die kombinierten Rohdaten in den Ring Buffer.

Architektur (siehe Vorgabe):

    DAQ Thread -> Ring Buffer -> Live View
                              -> Storage Writer

Design-Entscheidung:
    Pro Messung gibt es GENAU EINEN `AcquisitionThread`, der in jedem
    Zyklus einen gemeinsamen Datenblock von allen konfigurierten Geräten
    erfasst und die resultierenden Teilblöcke entlang der Kanalachse zu
    EINEM kombinierten Block zusammenfügt, bevor dieser einmalig in den
    (einzigen, gemeinsamen) Ring Buffer geschrieben wird. Das vermeidet
    mehrere gleichzeitige Schreiber in denselben Ring Buffer und hält die
    Kanal-Reihenfolge eindeutig (Reihenfolge der Geräte entspricht der
    Kanal-Reihenfolge im Ring Buffer, siehe `core/measurement.py::create_devices`).

    Bei mehreren NI-Geräten wird dabei automatisch ein gemeinsamer
    nidaqmx-Task verwendet, damit die Samples aus derselben Abtastung
    stammen. Die Acquisitionsschleife nutzt diesen gemeinsamen Task
    automatisch, ohne dass der Nutzer eine zusätzliche Konfiguration
    vornehmen muss.

    Mehr als eine Geräte-Gruppe (siehe `core/rate_merge.py::DeviceGroup`)
    ist nur bei einem intrinsischen Ratenkonflikt möglich (aktuell:
    NI9210 zusammen mit einem anderen Modul, siehe
    `data/models.py::resolve_rate_groups`) - in diesem Fall führt
    `RateMerger` die Gruppen per Zero-Order-Hold zu einem gemeinsam
    getakteten Block zusammen, BEVOR dieser in den Ring Buffer
    geschrieben wird. Der Ein-Gruppen-Fall (Regelfall) bleibt davon
    unberührt und läuft weiterhin über den direkten Lesepfad.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

import numpy as np

from core.rate_merge import DeviceGroup, RateMerger, _read_group_block
from core.ringbuffer import RingBuffer
from hardware.base_device import AcquisitionError

logger = logging.getLogger(__name__)

ErrorCallback = Callable[[Exception], None]


class AcquisitionThread:
    """Führt die zyklische Datenerfassung in einem eigenen Thread aus.

    Liest in jedem Zyklus von allen übergebenen Geräten und schreibt die
    kombinierten Rohdaten (UNSKALIERT) in den Ring Buffer. Die
    physikalische Skalierung (`core.measurement.apply_scaling`) erfolgt
    bewusst NICHT hier, sondern erst beim Konsum der Daten (Storage
    Writer, Live View), damit der DAQ-Thread so schnell wie möglich bleibt.
    """

    def __init__(
        self,
        device_groups: list[DeviceGroup],
        ring_buffer: RingBuffer,
        samples_per_read: int,
        read_timeout_seconds: float = 5.0,
        on_error: Optional[ErrorCallback] = None,
        target_samples: Optional[int] = None,
    ) -> None:
        """Initialisiert den DAQ-Thread.

        Args:
            device_groups: Ratenkompatible Geräte-Gruppen (siehe
                `data/models.py::resolve_rate_groups` +
                `core/rate_merge.py::DeviceGroup`), bereits konfiguriert
                und gestartet. Die Kanal-Reihenfolge über alle Gruppen
                und Geräte hinweg (Gruppe 1 zuerst, darin Gerät 1 zuerst,
                ...) MUSS der Kanal-Reihenfolge von `ring_buffer`
                entsprechen. Regelfall ist genau EINE Gruppe; mehr als
                eine Gruppe bedeutet einen echten Ratenkonflikt (siehe
                `core/rate_merge.py::RateMerger`).
            ring_buffer: Ziel-Ring-Buffer; `ring_buffer.num_channels` muss
                der Summe der aktiven Kanäle aller Geräte entsprechen.
            samples_per_read: Blockgröße pro Lesezyklus, bezogen auf die
                SCHNELLSTE Gruppe.
            read_timeout_seconds: Timeout pro `device.read()`-Aufruf.
            on_error: Optionaler Callback, der bei einem Fehler im
                DAQ-Thread aufgerufen wird. WICHTIG: Der Callback läuft
                selbst im DAQ-Thread (siehe `core/controller.py` für die
                daraus resultierenden Konsequenzen bzgl. Deadlock-Vermeidung).
            target_samples: Optionale Ziel-Samplezahl pro Kanal (siehe
                `data/models.py::MeasurementConfig.target_recording_stop_samples`).
                Wird NIE überschritten: der letzte Block wird exakt auf die
                verbleibende Anzahl gekappt, danach beendet sich der Thread
                SOFORT selbst - ohne auf `stop()`/das externe Stop-Signal zu
                warten (siehe `_run`). `None` = unbegrenzt (läuft, bis
                `stop()` aufgerufen wird - bisheriges Standardverhalten).

        Raises:
            ValueError: falls die Kanalanzahl der Geräte nicht zur
                Kanalanzahl des Ring Buffers passt.
        """
        self._devices = [d for group in device_groups for d in group.devices]
        total_channels = sum(len(d.active_channels) for d in self._devices)
        if total_channels != ring_buffer.num_channels:
            raise ValueError(
                f"Summe der aktiven Kanäle aller Geräte ({total_channels}) "
                f"stimmt nicht mit ring_buffer.num_channels "
                f"({ring_buffer.num_channels}) überein."
            )

        self._device_groups = device_groups
        self._ring_buffer = ring_buffer
        self._samples_per_read = samples_per_read
        self._read_timeout_seconds = read_timeout_seconds
        self._target_samples = target_samples
        self._on_error = on_error
        self._merger = RateMerger(device_groups, read_timeout_seconds) if len(device_groups) > 1 else None

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_error: Optional[Exception] = None
        self.total_samples_acquired = 0

    @property
    def is_running(self) -> bool:
        """True, solange der DAQ-Thread aktiv läuft."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def last_error(self) -> Optional[Exception]:
        """Der zuletzt aufgetretene Fehler, falls der Thread wegen eines
        Fehlers beendet wurde (sonst None)."""
        return self._last_error

    def start(self) -> None:
        """Startet den DAQ-Thread.

        Erwartet, dass alle übergebenen Geräte bereits konfiguriert UND
        gestartet sind (siehe `core/controller.py::start_measurement`).

        Raises:
            RuntimeError: falls der Thread bereits läuft.
        """
        if self.is_running:
            raise RuntimeError("AcquisitionThread läuft bereits.")
        self._stop_event.clear()
        self._last_error = None
        self.total_samples_acquired = 0
        self._thread = threading.Thread(
            target=self._run, name="DAQAcquisitionThread", daemon=True
        )
        self._thread.start()
        logger.info("DAQ-Thread gestartet (%d Geräte)", len(self._devices))

    def stop(self, timeout: float = 5.0) -> None:
        """Signalisiert dem DAQ-Thread zu stoppen und wartet auf dessen Ende.

        Idempotent: kann auch aufgerufen werden, wenn der Thread bereits
        (z. B. wegen eines Fehlers) beendet ist.
        """
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning(
                    "DAQ-Thread reagiert nicht innerhalb von %.1fs auf das Stop-Signal.",
                    timeout,
                )
        logger.info(
            "DAQ-Thread gestoppt, insgesamt %d Samples pro Kanal erfasst.",
            self.total_samples_acquired,
        )

    def _run(self) -> None:
        """Haupt-Schleife des DAQ-Threads (läuft im Hintergrund-Thread).

        Mit `target_samples` gesetzt: fordert für den letzten Block gezielt
        nur noch die verbleibende Samplezahl an (statt eines vollen Blocks)
        und bricht die Schleife SOFORT ab, sobald das Ziel erreicht ist -
        kein Warten auf `_stop_event`. Das garantiert exakt `target_samples`
        im Ring Buffer (weder mehr noch weniger) UND macht den Stopp bei
        einem konfigurierten Limit praktisch verzögerungsfrei, da der Thread
        beim naechsten externen `stop()`-Aufruf (siehe `gui/live_view.py::
        _on_timer_tick`) in aller Regel schon beendet ist.
        """
        try:
            while not self._stop_event.is_set():
                samples_to_read = self._samples_per_read
                if self._target_samples is not None:
                    remaining = self._target_samples - self.total_samples_acquired
                    if remaining <= 0:
                        break
                    samples_to_read = min(samples_to_read, remaining)

                combined = self._read_combined_block(samples_to_read)
                if self._target_samples is not None:
                    # Sicherheitsnetz falls ein Geraet trotz angeforderter
                    # `samples_to_read` mehr liefert - garantiert exakt
                    # `target_samples`, unabhaengig vom Treiberverhalten.
                    remaining = self._target_samples - self.total_samples_acquired
                    combined = combined[:, :remaining]
                self._ring_buffer.write(combined)
                self.total_samples_acquired += combined.shape[1]

                if self._target_samples is not None and self.total_samples_acquired >= self._target_samples:
                    break
        except AcquisitionError as exc:
            logger.error("Fehler im DAQ-Thread: %s", exc)
            self._last_error = exc
            if self._on_error is not None:
                self._on_error(exc)
        except Exception as exc:  # unerwarteter Fehler wird bewusst nicht verschluckt
            logger.exception("Unerwarteter Fehler im DAQ-Thread")
            self._last_error = exc
            if self._on_error is not None:
                self._on_error(exc)

    def _read_combined_block(self, samples_to_read: int) -> np.ndarray:
        """Liest den (ggf. verschmolzenen) Rohdaten-Block für diesen Zyklus.

        Der Ein-Gruppen-Fall (Regelfall) verwendet exakt denselben
        Lesepfad wie bisher (`_read_group_block`), unverändert. Erst bei
        > 1 Gruppe (aktuell nur NI9210 kombiniert mit einem anderen
        Modul) kommt `RateMerger` zum Einsatz (siehe
        `core/rate_merge.py`).
        """
        if self._merger is not None:
            return self._merger.read_merged_block(samples_to_read)
        devices = self._device_groups[0].devices if self._device_groups else []
        return _read_group_block(devices, samples_to_read, self._read_timeout_seconds)
