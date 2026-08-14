"""
core/ringbuffer.py

Thread-sicherer Ring Buffer für Mehrkanal-Messdaten.

Architektur-Kontext (siehe Vorgabe):

    DAQ Thread -> Ring Buffer -> Live View
                              -> Storage Writer

Der Ring Buffer entkoppelt den DAQ-Erfassungs-Thread von den beiden
Konsumenten (Live View, Storage Writer), die typischerweise mit
unterschiedlicher Geschwindigkeit lesen:

    * Der Storage Writer muss JEDEN Sample verlustfrei bekommen.
    * Die Live View darf und soll Samples überspringen dürfen (z. B. wenn
      die GUI kurz nicht rendert), ohne den Storage Writer zu beeinflussen.

Deshalb verwendet dieser Ring Buffer KEINE einzelne globale Leseposition,
sondern unabhängige "Reader" mit jeweils eigenem Lesezeiger
(`register_reader` / `read_new`). Ein langsamer Reader kann dadurch nie
einen anderen Reader blockieren oder dessen Daten "wegkonsumieren".

Performance-Design für Abtastraten bis 100 kHz:
    * Der Speicher wird einmalig als NumPy-Array vorallokiert
      (`np.zeros((num_channels, capacity))`) - keine Allokationen pro Sample.
    * Schreiben/Lesen erfolgt blockweise (NumPy-Slicing), nicht sample-weise.
    * Der Lock wird nur für die (billige) Zeiger-Buchhaltung gehalten, das
      eigentliche Kopieren der Daten passiert per NumPy-Vektorisierung
      innerhalb des Locks, bleibt aber durch die Blockverarbeitung kurz.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class RingBuffer:
    """Thread-sicherer, blockweiser Ring Buffer für Mehrkanal-Messdaten.

    Ein einzelner `RingBuffer` hält Daten für eine feste Anzahl Kanäle mit
    fester Kapazität (Samples pro Kanal). Der DAQ-Thread schreibt Blöcke
    via `write()`. Beliebig viele unabhängige Konsumenten können sich via
    `register_reader()` registrieren und ihre neuen Daten via `read_new()`
    abholen, ohne sich gegenseitig zu beeinflussen.

    Thread-Sicherheit:
        Alle öffentlichen Methoden sind thread-sicher (intern durch einen
        `threading.Lock` geschützt) und können gefahrlos aus verschiedenen
        Threads aufgerufen werden (z. B. DAQ-Thread schreibt, GUI-Thread
        und Storage-Thread lesen gleichzeitig).
    """

    def __init__(self, num_channels: int, capacity: int) -> None:
        """Initialisiert den Ring Buffer.

        Args:
            num_channels: Anzahl der Kanäle (Zeilen des internen Arrays).
            capacity: Kapazität in Samples pro Kanal.

        Raises:
            ValueError: falls `num_channels` oder `capacity` nicht positiv sind.
        """
        if num_channels <= 0:
            raise ValueError("num_channels muss positiv sein")
        if capacity <= 0:
            raise ValueError("capacity muss positiv sein")

        self._num_channels = num_channels
        self._capacity = capacity
        self._buffer = np.zeros((num_channels, capacity), dtype=np.float64)

        self._lock = threading.Lock()
        self._write_pos = 0
        self._total_written = 0

        self._readers: dict[int, int] = {}
        self._next_reader_id = 0

    @property
    def num_channels(self) -> int:
        """Anzahl der Kanäle des Buffers."""
        return self._num_channels

    @property
    def capacity(self) -> int:
        """Kapazität des Buffers in Samples pro Kanal."""
        return self._capacity

    # ------------------------------------------------------------------ #
    # Reader-Verwaltung
    # ------------------------------------------------------------------ #

    def register_reader(self, back_samples: int = 0) -> int:
        """Registriert einen neuen, unabhängigen Konsumenten (Reader).

        Der neue Reader beginnt standardmäßig an der aktuellen
        Schreibposition, sieht also nur Daten, die NACH der Registrierung
        geschrieben werden.

        Args:
            back_samples: Optional - lässt den Reader stattdessen
                `back_samples` Samples VOR der aktuellen Schreibposition
                beginnen (rückwirkendes Lesen, z. B. für einen
                Vorlauf-/Pre-Trigger-Puffer beim Schwellwert-Trigger, siehe
                `gui/main_window.py::_on_trigger_fired`). Wird auf die
                älteste noch im Puffer vorhandene Position geclamped -
                sowohl nach unten (nicht vor Sample 0), als auch nach oben
                (nicht weiter zurück, als der Puffer noch hält, sonst
                bereits überschriebene Daten). `0` (Standard) entspricht
                exakt dem bisherigen Verhalten.

        Returns:
            Eine Reader-ID, die bei allen weiteren Aufrufen
            (`read_new`, `available_samples`, `unregister_reader`) verwendet wird.
        """
        with self._lock:
            reader_id = self._next_reader_id
            self._next_reader_id += 1
            oldest_valid = max(0, self._total_written - self._capacity)
            start_count = max(oldest_valid, self._total_written - max(0, back_samples))
            self._readers[reader_id] = start_count
            logger.debug(
                "Reader %d registriert (start_count=%d, back_samples=%d)",
                reader_id,
                start_count,
                back_samples,
            )
            return reader_id

    def unregister_reader(self, reader_id: int) -> None:
        """Entfernt einen Reader (z. B. wenn die Live View geschlossen wird)."""
        with self._lock:
            self._readers.pop(reader_id, None)
            logger.debug("Reader %d entfernt", reader_id)

    # ------------------------------------------------------------------ #
    # Schreiben
    # ------------------------------------------------------------------ #

    def write(self, data: np.ndarray) -> None:
        """Schreibt einen neuen Datenblock in den Ring Buffer.

        Args:
            data: Array der Form (num_channels, num_new_samples).

        Raises:
            ValueError: falls die Kanalanzahl nicht passt oder der Block
                größer als die Gesamtkapazität ist (Konfigurationsfehler,
                z. B. `samples_per_read > ring_buffer_size`).
        """
        if data.ndim != 2 or data.shape[0] != self._num_channels:
            raise ValueError(
                f"Erwartete Form (num_channels={self._num_channels}, N), "
                f"erhalten {data.shape}"
            )

        n = data.shape[1]
        if n == 0:
            return
        if n > self._capacity:
            raise ValueError(
                f"Block (N={n}) ist größer als die Ring-Buffer-Kapazität "
                f"({self._capacity}). ring_buffer_size in der Konfiguration erhöhen."
            )

        with self._lock:
            start = self._write_pos
            end = start + n

            if end <= self._capacity:
                self._buffer[:, start:end] = data
            else:
                first_part = self._capacity - start
                self._buffer[:, start:] = data[:, :first_part]
                self._buffer[:, : n - first_part] = data[:, first_part:]

            self._write_pos = (start + n) % self._capacity
            self._total_written += n

            self._check_overruns_locked()

    def _check_overruns_locked(self) -> None:
        """Erkennt und behandelt Reader, die zu weit hinterherhinken.

        Muss innerhalb von `self._lock` aufgerufen werden. Ein Reader, der
        mehr als `capacity` Samples im Rückstand ist, hat garantiert
        Daten verloren (sie wurden bereits überschrieben). Der Reader wird
        in diesem Fall auf die älteste noch gültige Position vorgezogen und
        der Datenverlust wird geloggt.
        """
        for reader_id, read_count in list(self._readers.items()):
            lag = self._total_written - read_count
            if lag > self._capacity:
                lost = lag - self._capacity
                logger.warning(
                    "Ring Buffer Overrun fuer Reader %d: %d Samples wurden "
                    "ueberschrieben, bevor sie gelesen wurden (Kapazitaet %d). "
                    "Reader wird auf die aelteste verfuegbare Position vorgezogen.",
                    reader_id,
                    lost,
                    self._capacity,
                )
                self._readers[reader_id] = self._total_written - self._capacity

    # ------------------------------------------------------------------ #
    # Lesen
    # ------------------------------------------------------------------ #

    def read_new(self, reader_id: int, max_samples: Optional[int] = None) -> np.ndarray:
        """Liest alle seit dem letzten Aufruf neu geschriebenen Samples.

        Nicht-blockierend: Ist nichts Neues vorhanden, wird ein leeres
        Array zurückgegeben (Aufrufer entscheidet selbst über Polling-Intervall).

        Args:
            reader_id: ID aus `register_reader`.
            max_samples: Optionale Obergrenze für die Anzahl zurückgegebener
                Samples pro Aufruf. Nützlich für die Live View, damit ein
                GUI-Update nach einer längeren Pause nicht schlagartig
                riesige Datenmengen zeichnen muss. Übersprungene, ältere
                Samples gelten dabei als regulär gelesen (kein Overrun-Log).

        Returns:
            Array der Form (num_channels, num_available_samples).
            `num_available_samples` kann 0 sein.

        Raises:
            KeyError: falls `reader_id` nicht (mehr) registriert ist.
        """
        with self._lock:
            if reader_id not in self._readers:
                raise KeyError(f"Unbekannte reader_id: {reader_id}")

            read_count = self._readers[reader_id]
            available = self._total_written - read_count

            if available <= 0:
                return np.empty((self._num_channels, 0), dtype=np.float64)

            if max_samples is not None and available > max_samples:
                skipped = available - max_samples
                read_count += skipped
                available = max_samples

            start = read_count % self._capacity
            end = start + available

            if end <= self._capacity:
                out = self._buffer[:, start:end].copy()
            else:
                first_part = self._capacity - start
                out = np.empty((self._num_channels, available), dtype=np.float64)
                out[:, :first_part] = self._buffer[:, start:]
                out[:, first_part:] = self._buffer[:, : available - first_part]

            self._readers[reader_id] = read_count + available
            return out

    def available_samples(self, reader_id: int) -> int:
        """Anzahl der aktuell ungelesenen Samples für einen Reader.

        Raises:
            KeyError: falls `reader_id` nicht (mehr) registriert ist.
        """
        with self._lock:
            if reader_id not in self._readers:
                raise KeyError(f"Unbekannte reader_id: {reader_id}")
            return max(0, self._total_written - self._readers[reader_id])

    def reset(self) -> None:
        """Setzt den Buffer und alle Lesezeiger zurück (z. B. bei Messstart)."""
        with self._lock:
            self._buffer.fill(0.0)
            self._write_pos = 0
            self._total_written = 0
            for reader_id in self._readers:
                self._readers[reader_id] = 0
            logger.debug("Ring Buffer zurueckgesetzt")
