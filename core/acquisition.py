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
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import numpy as np

from core.ringbuffer import RingBuffer
from hardware.base_device import AcquisitionError, BaseDevice

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
        devices: list[BaseDevice],
        ring_buffer: RingBuffer,
        samples_per_read: int,
        read_timeout_seconds: float = 5.0,
        on_error: Optional[ErrorCallback] = None,
    ) -> None:
        """Initialisiert den DAQ-Thread.

        Args:
            devices: Liste konfigurierter und bereits gestarteter
                Hardware-Geräte. Die Kanal-Reihenfolge über alle Geräte
                hinweg MUSS der Kanal-Reihenfolge von `ring_buffer`
                entsprechen (Gerät 1 zuerst, dann Gerät 2, ...).
            ring_buffer: Ziel-Ring-Buffer; `ring_buffer.num_channels` muss
                der Summe der aktiven Kanäle aller `devices` entsprechen.
            samples_per_read: Blockgröße pro Lesezyklus und Gerät.
            read_timeout_seconds: Timeout pro `device.read()`-Aufruf.
            on_error: Optionaler Callback, der bei einem Fehler im
                DAQ-Thread aufgerufen wird. WICHTIG: Der Callback läuft
                selbst im DAQ-Thread (siehe `core/controller.py` für die
                daraus resultierenden Konsequenzen bzgl. Deadlock-Vermeidung).

        Raises:
            ValueError: falls die Kanalanzahl der Geräte nicht zur
                Kanalanzahl des Ring Buffers passt.
        """
        total_channels = sum(len(d.active_channels) for d in devices)
        if total_channels != ring_buffer.num_channels:
            raise ValueError(
                f"Summe der aktiven Kanäle aller Geräte ({total_channels}) "
                f"stimmt nicht mit ring_buffer.num_channels "
                f"({ring_buffer.num_channels}) überein."
            )

        self._devices = devices
        self._ring_buffer = ring_buffer
        self._samples_per_read = samples_per_read
        self._read_timeout_seconds = read_timeout_seconds
        self._on_error = on_error

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
        """Haupt-Schleife des DAQ-Threads (läuft im Hintergrund-Thread)."""
        try:
            while not self._stop_event.is_set():
                blocks = self._read_blocks_from_devices()
                combined = np.concatenate(blocks, axis=0)
                self._ring_buffer.write(combined)
                self.total_samples_acquired += combined.shape[1]
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

    def _read_blocks_from_devices(self) -> list[np.ndarray]:
        """Liest von allen Geräten einen gemeinsamen Datenblock ein."""
        if not self._devices:
            return []

        shared_devices = [device for device in self._devices if getattr(device, "_shared_task", None) is not None]
        if shared_devices:
            shared_block = shared_devices[0].read_shared_block(
                self._samples_per_read,
                timeout=self._read_timeout_seconds,
            )
            return [device.read_from_shared_block(shared_block) for device in self._devices]

        with ThreadPoolExecutor(max_workers=max(1, len(self._devices))) as executor:
            futures = [
                executor.submit(
                    device.read,
                    self._samples_per_read,
                    timeout=self._read_timeout_seconds,
                )
                for device in self._devices
            ]
            return [future.result() for future in futures]
