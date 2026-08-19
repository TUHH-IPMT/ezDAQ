"""
gui/serial_trigger.py

Background listener for the serial (USB) measurement trigger.

Unlike `gui/workers.py::BackgroundWorker` (deliberately documented as a
short-lived, one-off job), this `QThread` runs for a LONG time: it may
wait for minutes for a specific signal from the configured COM port
before either triggering or being canceled by the user (see
`gui/main_window.py::_on_start_measurement`/`_on_stop_measurement`).

Deliberately lives under `gui/` rather than `hardware/`:
`hardware/nidaq_device.py` explicitly documents itself as the "ONLY
place in the application that imports nidaqmx directly" - the serial
trigger has nothing to do with NI-DAQmx, and unlike there, no existing
layering precedent here would forbid GUI-side Qt threads.
"""

from __future__ import annotations

import logging
import threading
import time

import serial
from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger(__name__)


class SerialTriggerListener(QThread):
    """Waits on a serial port for an exact byte/text signal.

    Continuously reads from the configured COM port and compares the
    most recently received bytes (sliding window, see `run()`) against
    `expected_message` - only an exact match triggers (not just any
    byte). The window also works when the message arrives split across
    multiple `read()` calls.

    IMPORTANT: The comparison only happens once a `read()` returns NO
    more new bytes (a brief pause in transmission) - not on every single
    incoming byte. Without this pause, a LONGER, actually-sent message
    would trigger prematurely as soon as its prefix happens to match
    `expected_message` (e.g. `expected_message = b"TRIGGE"` would
    otherwise already trigger as soon as the first 6 of the 7 bytes of
    "TRIGGER" have arrived - before the "R" even arrives).

    This transmission pause IS the dominant source of latency until
    triggering - `read_timeout_seconds` is used directly as the timeout
    of `serial.Serial()`, i.e. `run()` waits exactly that long for
    further bytes after the last one received, before the comparison
    (and possibly the trigger) happens. The default values are chosen
    deliberately tight (well below the byte transmission time of even a
    slow baud rate), so that triggering stays as close as possible to
    the actual physical signal without jeopardizing the prefix detection
    described above.

    The caller MUST hold a reference until the thread has finished (see
    `BackgroundWorker` docs) - `gui/main_window.py` holds it in
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
        poll_interval_seconds: float = 0.01,
        read_timeout_seconds: float = 0.02,
    ) -> None:
        super().__init__()
        self._port = port
        self._baud_rate = baud_rate
        self._expected_message = expected_message
        self._poll_interval_seconds = poll_interval_seconds
        self._read_timeout_seconds = read_timeout_seconds
        self._stop_event = threading.Event()

    def stop(self) -> None:
        """Signals the thread to stop and waits for it to finish.

        Idempotent: can also be called if the thread has already
        finished (e.g. after a match or a connection error).
        """
        self._stop_event.set()
        self.wait(2000)

    def run(self) -> None:
        buffer = bytearray()
        try:
            with serial.Serial(self._port, self._baud_rate, timeout=self._read_timeout_seconds) as ser:
                while not self._stop_event.is_set():
                    chunk = ser.read(max(1, ser.in_waiting or 1))
                    if chunk:
                        self.data_received.emit(bytes(chunk))
                        buffer.extend(chunk)
                        # Sliding window: keep only the most recently
                        # received len(expected_message) bytes - allows
                        # arbitrary bytes before/between as well as a
                        # message arriving split across multiple reads.
                        if len(buffer) > len(self._expected_message):
                            del buffer[: len(buffer) - len(self._expected_message)]
                        # Do NOT compare here: further bytes of the same
                        # message could still be on their way (see class
                        # docstring) - only after a transmission pause,
                        # below.
                        continue

                    if self._expected_message and buffer and bytes(buffer) == self._expected_message:
                        logger.info("Serieller Trigger ausgelöst (Port %s)", self._port)
                        self.message_matched.emit()
                        return

                    time.sleep(self._poll_interval_seconds)
        except serial.SerialException as exc:
            logger.error("Serieller Trigger: Verbindung zu %s fehlgeschlagen: %s", self._port, exc)
            self.connection_failed.emit(str(exc))
