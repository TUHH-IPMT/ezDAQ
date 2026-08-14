"""
data/exporter.py

Storage Writer: liest kontinuierlich und verlustfrei aus dem Ring Buffer
und schreibt Messdaten WÄHREND der Messung blockweise auf die
Festplatte (Parquet oder CSV).

Architektur (siehe Vorgabe):

    DAQ Thread -> Ring Buffer -> Storage Writer

Der Storage Writer registriert sich als eigener, unabhängiger
Ring-Buffer-Reader (siehe `core/ringbuffer.py::RingBuffer`) und liest
OHNE `max_samples`-Begrenzung, damit er garantiert keine Samples verliert
- unabhängig davon, wie schnell oder langsam die Live View parallel liest.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from core.measurement import apply_scaling
from core.ringbuffer import RingBuffer
from data.models import Channel, StorageFormat

logger = logging.getLogger(__name__)


def _sanitize_column_name(name: str) -> str:
    """Stellt sicher, dass ein Kanal-Anzeigename als Spaltenname nutzbar ist.

    Ändert nicht den `display_name` des Kanals selbst - nur den in der
    Datei verwendeten Spaltennamen (z. B. falls der Anzeigename leer wäre).
    """
    stripped = name.strip()
    return stripped if stripped else "kanal"


def _build_dataframe(
    scaled_block: np.ndarray,
    channels: list[Channel],
    start_sample_index: int,
    sample_rate_hz: float,
) -> pd.DataFrame:
    """Wandelt einen bereits physikalisch skalierten Rohdaten-Block in
    einen DataFrame mit berechneter Zeitspalte ("time_s") um."""
    n = scaled_block.shape[1]
    time_s = (start_sample_index + np.arange(n)) / sample_rate_hz
    data: dict[str, np.ndarray] = {"time_s": time_s}
    for i, channel in enumerate(channels):
        data[_sanitize_column_name(channel.display_name)] = scaled_block[i]
    return pd.DataFrame(data)


class StorageWriter:
    """Schreibt Messdaten während der Messung kontinuierlich auf die Festplatte.

    Liest über einen eigenen Ring-Buffer-Reader und schreibt neue Blöcke
    jeweils sofort in die Zieldatei:
        * Parquet: inkrementell über `pyarrow.parquet.ParquetWriter`
          (ein Writer bleibt über die gesamte Messung geöffnet, jeder
          Block wird als weitere Row-Group angehängt).
        * CSV: inkrementelles Anhängen (`DataFrame.to_csv(mode="a")`).

    Dadurch bleiben Messdaten auch bei einem Absturz während einer langen
    Messung bis zum zuletzt geschriebenen Block erhalten (siehe
    Projektvorgabe "Daten während der Messung speichern").
    """

    def __init__(
        self,
        ring_buffer: RingBuffer,
        channels: list[Channel],
        output_path: Path,
        storage_format: StorageFormat,
        sample_rate_hz: float,
        poll_interval_seconds: float = 0.05,
        reader_back_samples: int = 0,
    ) -> None:
        """Initialisiert den Storage Writer.

        Args:
            ring_buffer: Ring Buffer der laufenden Messung.
            channels: Aktive Kanäle in der Reihenfolge der Ring-Buffer-Zeilen
                (muss der Reihenfolge entsprechen, mit der der DAQ-Thread
                schreibt, siehe `core/measurement.py::create_devices`).
            output_path: Zieldatei. Die Endung wird NICHT automatisch
                angepasst - siehe `data/metadata.py::MeasurementProject.measurement_data_path`.
            storage_format: `StorageFormat.PARQUET` oder `StorageFormat.CSV`.
            sample_rate_hz: Abtastrate, für die berechnete Zeitspalte.
            poll_interval_seconds: Wartezeit zwischen Lesevorgängen, wenn
                aktuell keine neuen Daten im Ring Buffer vorliegen.
            reader_back_samples: Optionaler Vorlauf in Samples - lässt den
                internen Ring-Buffer-Reader rückwirkend beginnen (siehe
                `core/ringbuffer.py::RingBuffer.register_reader`), damit
                bereits vor dem Erzeugen dieses StorageWriters gepufferte
                Daten mitgeschrieben werden (Pre-Trigger-Aufzeichnung beim
                Schwellwert-Trigger, siehe
                `gui/main_window.py::_on_trigger_fired`). `0` (Standard)
                entspricht dem bisherigen Verhalten (Start "jetzt").
        """
        self._ring_buffer = ring_buffer
        self._channels = channels
        self._output_path = output_path
        self._storage_format = storage_format
        self._sample_rate_hz = sample_rate_hz
        self._poll_interval_seconds = poll_interval_seconds

        self._reader_id = ring_buffer.register_reader(back_samples=reader_back_samples)
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_error: Optional[Exception] = None

        self._total_samples_written = 0
        self._parquet_writer: Optional[pq.ParquetWriter] = None
        self._csv_header_written = False

    @property
    def is_running(self) -> bool:
        """True, während der Storage-Writer-Thread aktiv läuft."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def last_error(self) -> Optional[Exception]:
        """Der zuletzt aufgetretene Fehler, falls der Thread deswegen beendet wurde."""
        return self._last_error

    @property
    def total_samples_written(self) -> int:
        """Anzahl bisher geschriebener Samples pro Kanal."""
        return self._total_samples_written

    @property
    def output_path(self) -> Path:
        """Zieldatei, in die geschrieben wird."""
        return self._output_path

    @property
    def pending_samples(self) -> int:
        """Anzahl der vom DAQ-Thread bereits geschriebenen, vom StorageWriter
        aber noch nicht auf die Festplatte übertragenen Samples.

        Nähert sich dieser Wert der Ring-Buffer-Kapazität an (z. B. weil die
        Festplatte nicht mitkommt oder voll ist), droht ein Overrun -
        unwiederbringlicher Datenverlust, siehe
        `core/ringbuffer.py::RingBuffer._check_overruns_locked`.
        """
        return self._ring_buffer.available_samples(self._reader_id)

    def start(self) -> None:
        """Startet den Storage-Writer-Thread.

        Raises:
            RuntimeError: falls der Thread bereits läuft.
        """
        if self.is_running:
            raise RuntimeError("StorageWriter läuft bereits.")
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="StorageWriterThread", daemon=True
        )
        self._thread.start()
        logger.info("StorageWriter gestartet: %s", self._output_path)

    def stop(self, timeout: float = 10.0) -> None:
        """Stoppt den Storage-Writer-Thread und schließt die Zieldatei sauber.

        Wartet, bis der Thread alle bereits gelesenen, aber noch nicht
        geschriebenen Daten verarbeitet hat, bevor die Datei geschlossen wird.
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning(
                    "StorageWriter reagiert nicht innerhalb von %.1fs auf das Stop-Signal.",
                    timeout,
                )
        self._close_writer()
        self._ring_buffer.unregister_reader(self._reader_id)
        logger.info(
            "StorageWriter gestoppt, %d Samples in %s gespeichert.",
            self._total_samples_written,
            self._output_path,
        )

    def _run(self) -> None:
        try:
            while True:
                data = self._ring_buffer.read_new(self._reader_id)
                if data.shape[1] == 0:
                    if self._stop_event.is_set():
                        break
                    time.sleep(self._poll_interval_seconds)
                    continue
                self._write_block(data)
                self._total_samples_written += data.shape[1]
        except Exception as exc:
            logger.exception("Fehler im StorageWriter, Messdaten bis zum letzten "
                              "erfolgreich geschriebenen Block bleiben erhalten.")
            self._last_error = exc

    def _write_block(self, raw_block: np.ndarray) -> None:
        """Skaliert und schreibt einen einzelnen Rohdaten-Block."""
        scaled = apply_scaling(raw_block, self._channels)
        df = _build_dataframe(
            scaled, self._channels, self._total_samples_written, self._sample_rate_hz
        )

        if self._storage_format == StorageFormat.PARQUET:
            table = pa.Table.from_pandas(df, preserve_index=False)
            if self._parquet_writer is None:
                self._parquet_writer = pq.ParquetWriter(str(self._output_path), table.schema)
            self._parquet_writer.write_table(table)
        else:
            df.to_csv(
                self._output_path,
                mode="a",
                header=not self._csv_header_written,
                index=False,
            )
            self._csv_header_written = True

    def _close_writer(self) -> None:
        """Schließt einen ggf. offenen Parquet-Writer (idempotent)."""
        if self._parquet_writer is not None:
            try:
                self._parquet_writer.close()
            except Exception:
                logger.exception("Fehler beim Schließen des Parquet-Writers")
            self._parquet_writer = None
