"""
gui/serial_trigger.py

Hintergrund-Lauscher für den seriellen (USB-)Mess-Trigger.

Anders als `gui/workers.py::BackgroundWorker` (bewusst als kurzlebiger
Einzelauftrag dokumentiert) ist dieser `QThread` LANGE laufend: er wartet
u. U. minutenlang auf ein bestimmtes Signal vom konfigurierten COM-Port,
bevor er entweder auslöst oder vom Nutzer abgebrochen wird (siehe
`gui/main_window.py::_on_start_measurement`/`_on_stop_measurement`).

Lebt bewusst unter `gui/` statt unter `hardware/`: `hardware/nidaq_device.py`
dokumentiert sich explizit als "EINZIGE Stelle der Anwendung, die nidaqmx
direkt importiert" - der serielle Trigger hat mit NI-DAQmx nichts zu tun,
und anders als dort gibt es hier keinen bestehenden Layering-Präzedenzfall,
der GUI-seitige Qt-Threads verbieten würde.
"""

from __future__ import annotations

import logging
import threading
import time

import serial
from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger(__name__)


class SerialTriggerListener(QThread):
    """Wartet auf einem seriellen Port auf ein exaktes Byte-/Text-Signal.

    Liest fortlaufend vom konfigurierten COM-Port und vergleicht die
    zuletzt empfangenen Bytes (gleitendes Fenster, siehe `run()`) gegen
    `expected_message` - erst ein exakter Treffer löst aus (kein beliebiges
    Byte). Das Fenster funktioniert auch, wenn die Nachricht über mehrere
    `read()`-Aufrufe verteilt eintrifft.

    Der Aufrufer MUSS eine Referenz halten, bis der Thread beendet ist
    (siehe `BackgroundWorker`-Doku) - `gui/main_window.py` hält sie in
    `self._serial_listener`.
    """

    message_matched = pyqtSignal()
    connection_failed = pyqtSignal(str)
    data_received = pyqtSignal(bytes)

    def __init__(
        self,
        port: str,
        baud_rate: int,
        expected_message: bytes,
        poll_interval_seconds: float = 0.05,
        read_timeout_seconds: float = 0.1,
    ) -> None:
        super().__init__()
        self._port = port
        self._baud_rate = baud_rate
        self._expected_message = expected_message
        self._poll_interval_seconds = poll_interval_seconds
        self._read_timeout_seconds = read_timeout_seconds
        self._stop_event = threading.Event()

    def stop(self) -> None:
        """Signalisiert dem Thread zu stoppen und wartet auf dessen Ende.

        Idempotent: kann auch aufgerufen werden, wenn der Thread bereits
        (z. B. nach einem Treffer oder Verbindungsfehler) beendet ist.
        """
        self._stop_event.set()
        self.wait(2000)

    def run(self) -> None:
        buffer = bytearray()
        try:
            with serial.Serial(self._port, self._baud_rate, timeout=self._read_timeout_seconds) as ser:
                while not self._stop_event.is_set():
                    chunk = ser.read(max(1, ser.in_waiting or 1))
                    if not chunk:
                        time.sleep(self._poll_interval_seconds)
                        continue
                    self.data_received.emit(bytes(chunk))
                    buffer.extend(chunk)
                    # Gleitendes Fenster: nur die zuletzt empfangenen
                    # len(expected_message) Bytes behalten - erlaubt
                    # beliebige Bytes davor/dazwischen sowie eine über
                    # mehrere Reads verteilt eintreffende Nachricht.
                    if len(buffer) > len(self._expected_message):
                        del buffer[: len(buffer) - len(self._expected_message)]
                    if self._expected_message and bytes(buffer) == self._expected_message:
                        logger.info("Serieller Trigger ausgelöst (Port %s)", self._port)
                        self.message_matched.emit()
                        return
        except serial.SerialException as exc:
            logger.error("Serieller Trigger: Verbindung zu %s fehlgeschlagen: %s", self._port, exc)
            self.connection_failed.emit(str(exc))
