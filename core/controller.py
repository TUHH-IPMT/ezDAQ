"""
core/controller.py

Messcontroller: zentrale Schnittstelle zwischen GUI und den darunter
liegenden Schichten (Hardware, Ring Buffer, DAQ-Thread).

Architektur (siehe Vorgabe):

    GUI -> Measurement Controller -> Hardware Interface -> nidaqmx -> NI cDAQ

Die GUI ruft ausschließlich Methoden dieses Controllers auf - sie kennt
weder `RingBuffer` noch `BaseDevice` noch `nidaqmx` direkt.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Callable, Optional

import numpy as np

from config.configuration_manager import ConfigurationManager
from core.acquisition import AcquisitionThread
from core.measurement import MeasurementConfigError, create_devices
from core.ringbuffer import RingBuffer
from data.models import Channel, DeviceInfo, MeasurementConfig, MeasurementSession
from hardware.base_device import AcquisitionError, BaseDevice
from hardware.nidaq_device import NIDAQSharedTask, discover_devices

logger = logging.getLogger(__name__)

ErrorCallback = Callable[[Exception], None]


class MeasurementController:
    """Orchestriert eine komplette Messung.

    Verantwortlichkeiten:
        * Hardware anhand einer `MeasurementConfig` konfigurieren und starten.
        * Den DAQ-Thread betreiben.
        * Live-Daten für Konsumenten (Live View, Storage Writer) über
          unabhängige Ring-Buffer-Reader bereitstellen.
        * Eine Messung sauber stoppen und Hardware-Ressourcen freigeben.

    Eine Instanz verwaltet höchstens eine laufende Messung gleichzeitig
    (siehe Projektvorgabe: "Es soll zunächst nur ein Projekt gleichzeitig
    geöffnet werden können").
    """

    def __init__(self, configuration_manager: ConfigurationManager) -> None:
        self._configuration_manager = configuration_manager
        self._lock = threading.RLock()

        self._devices: list[BaseDevice] = []
        self._ring_buffer: Optional[RingBuffer] = None
        self._acquisition_thread: Optional[AcquisitionThread] = None
        self._session: Optional[MeasurementSession] = None

        self._error_listeners: list[ErrorCallback] = []

    # ------------------------------------------------------------------ #
    # Geräteerkennung
    # ------------------------------------------------------------------ #

    def discover_hardware(self) -> list[DeviceInfo]:
        """Erkennt angeschlossene NI-cDAQ-Module (für die Setup-Ansicht)."""
        return discover_devices()

    # ------------------------------------------------------------------ #
    # Fehlerbenachrichtigung
    # ------------------------------------------------------------------ #

    def add_error_listener(self, callback: ErrorCallback) -> None:
        """Registriert einen Callback für DAQ-Thread-Fehler.

        Wird z. B. von `gui/main_window.py` genutzt, um bei einem
        Hardwarefehler während der Messung eine Fehlermeldung anzuzeigen.
        Der Callback wird IM DAQ-Thread aufgerufen (siehe
        `_handle_acquisition_error`) - GUI-Callbacks müssen daher
        thread-sicher mit der Qt-Event-Loop kommunizieren (z. B. über
        Qt-Signale, nicht durch direkte Widget-Manipulation).
        """
        self._error_listeners.append(callback)

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #

    @property
    def is_running(self) -> bool:
        """True, während eine Messung aktiv läuft."""
        with self._lock:
            return self._acquisition_thread is not None and self._acquisition_thread.is_running

    @property
    def current_session(self) -> Optional[MeasurementSession]:
        """Die aktuell laufende (oder zuletzt gestartete) `MeasurementSession`."""
        with self._lock:
            return self._session

    @property
    def active_channels(self) -> list[Channel]:
        """Aktive Kanäle in der gleichen Reihenfolge wie der Ring Buffer sie schreibt."""
        with self._lock:
            if self._session is None:
                return []
            return self.acquisition_channels

    @property
    def acquisition_channels(self) -> list[Channel]:
        """Gibt die aktiven Kanäle in der Reihenfolge der DAQ-Acquisition zurück."""
        with self._lock:
            if self._session is None:
                return []
            if not self._devices:
                return self._session.config.active_channels()

            channels: list[Channel] = []
            for device in self._devices:
                channels.extend(device.active_channels)
            return channels

    @property
    def active_device_infos(self) -> list[DeviceInfo]:
        """Geräteinformationen der aktuell verwendeten Hardware (für Metadaten)."""
        with self._lock:
            return [d.device_info for d in self._devices]

    # ------------------------------------------------------------------ #
    # Messung starten/stoppen
    # ------------------------------------------------------------------ #

    def start_measurement(
        self,
        config: MeasurementConfig,
        discovered_devices: Optional[list[DeviceInfo]] = None,
    ) -> MeasurementSession:
        """Startet eine neue Messung gemäß `config`.

        Args:
            config: Vollständige Messkonfiguration (Kanäle, Abtastrate, ...).
            discovered_devices: Optionales Ergebnis von
                `discover_hardware()`, um wiederholte Geräteerkennung zu
                vermeiden. Wird sonst automatisch aufgerufen.

        Returns:
            Die neu gestartete `MeasurementSession`.

        Raises:
            RuntimeError: falls bereits eine Messung läuft.
            MeasurementConfigError: bei ungültiger Kanalkonfiguration.
            AcquisitionError: falls Hardware-Konfiguration/-Start fehlschlägt.
        """
        with self._lock:
            if self._acquisition_thread is not None and self._acquisition_thread.is_running:
                raise RuntimeError(
                    "Es läuft bereits eine Messung. Bitte zuerst stop_measurement() aufrufen."
                )

            active_channels = config.active_channels()
            if not active_channels:
                raise MeasurementConfigError(
                    "Die Messkonfiguration enthält keine aktiven Kanäle."
                )

            if discovered_devices is None:
                discovered_devices = self.discover_hardware()

            devices = create_devices(active_channels, discovered_devices)

            configured_devices: list[BaseDevice] = []
            shared_task: Optional[NIDAQSharedTask] = None
            if len(devices) > 1:
                # Mehrere Geräte bekommen einen gemeinsamen Task, damit sie
                # hardwareseitig dieselbe Abtastung teilen.
                shared_task = NIDAQSharedTask()
            try:
                for device in devices:
                    device.configure(
                        config.sample_rate_hz,
                        config.samples_per_read,
                        sample_clock_source=None,
                        shared_task=shared_task,
                    )
                    configured_devices.append(device)
                if shared_task is not None:
                    # Timing des gemeinsamen Tasks erst konfigurieren, nachdem
                    # ALLE Geräte ihre Kanäle hinzugefügt haben (siehe
                    # NIDAQSharedTask.finalize()).
                    shared_task.finalize()
            except AcquisitionError:
                self._close_devices(configured_devices)
                raise

            ring_buffer = RingBuffer(
                num_channels=len(active_channels),
                capacity=config.ring_buffer_size,
            )

            try:
                for device in devices:
                    device.start()
            except AcquisitionError:
                self._close_devices(devices)
                raise

            acquisition_thread = AcquisitionThread(
                devices=devices,
                ring_buffer=ring_buffer,
                samples_per_read=config.samples_per_read,
                on_error=self._handle_acquisition_error,
            )
            acquisition_thread.start()

            self._devices = devices
            self._ring_buffer = ring_buffer
            self._acquisition_thread = acquisition_thread
            self._session = MeasurementSession(config=config, start_time=datetime.now())

            if devices:
                self._configuration_manager.update_last_device_name(
                    devices[0].device_info.device_name
                )
            self._configuration_manager.save_channel_configuration(active_channels)

            logger.info(
                "Messung '%s' gestartet: %d Kanäle, %.1f Hz",
                config.name,
                len(active_channels),
                config.sample_rate_hz,
            )
            return self._session

    def stop_measurement(self) -> Optional[MeasurementSession]:
        """Stoppt die laufende Messung (falls vorhanden).

        Returns:
            Die abgeschlossene `MeasurementSession` (mit gesetzter
            `end_time`), oder None, falls keine Messung lief.
        """
        with self._lock:
            return self._stop_measurement_locked()

    def _stop_measurement_locked(self) -> Optional[MeasurementSession]:
        """Interne Stop-Logik. Muss innerhalb von `self._lock` aufgerufen werden."""
        if self._acquisition_thread is not None:
            self._acquisition_thread.stop()
            self._acquisition_thread = None

        self._close_devices(self._devices)
        self._devices = []
        self._ring_buffer = None

        session = self._session
        if session is not None and session.end_time is None:
            session.end_time = datetime.now()
        self._session = None

        if session is not None:
            logger.info(
                "Messung '%s' gestoppt nach %.1f s",
                session.config.name,
                session.duration_seconds or 0.0,
            )
        return session

    def _close_devices(self, devices: list[BaseDevice]) -> None:
        """Schließt eine Liste von Geräten, Fehler werden geloggt, nicht weitergeworfen."""
        for device in devices:
            try:
                device.close()
            except Exception:
                logger.exception(
                    "Fehler beim Schließen von %s", device.device_info.device_name
                )

    def _handle_acquisition_error(self, exc: Exception) -> None:
        """Reagiert auf einen Fehler im DAQ-Thread.

        WICHTIG: Diese Methode wird VOM DAQ-THREAD SELBST aufgerufen (der
        `on_error`-Callback in `AcquisitionThread._run` läuft im selben
        Thread, der die Ausnahme geworfen hat). Sie ruft daher bewusst
        NICHT `stop_measurement()` auf, da `AcquisitionThread.stop()`
        versuchen würde, den DAQ-Thread mit sich selbst zu joinen
        (`Thread.join()` auf den eigenen, aktuell laufenden Thread führt
        zu einer Endlos-Wartezeit bzw. logischem Deadlock).

        Stattdessen werden nur die Hardware-Ressourcen aufgeräumt; der
        DAQ-Thread beendet sich durch die Rückkehr aus seiner Lese-Schleife
        von selbst. Ein späterer, expliziter `stop_measurement()`-Aufruf
        (z. B. durch die GUI, nachdem sie über den Fehler informiert
        wurde) findet dann einen bereits beendeten Thread vor und räumt
        idempotent auf.
        """
        with self._lock:
            logger.error(
                "Messung wird aufgrund eines Fehlers im DAQ-Thread beendet: %s", exc
            )
            self._close_devices(self._devices)
            self._devices = []
            if self._session is not None and self._session.end_time is None:
                self._session.end_time = datetime.now()

        for callback in list(self._error_listeners):
            try:
                callback(exc)
            except Exception:
                logger.exception("Fehler in einem Error-Listener")

    # ------------------------------------------------------------------ #
    # Live-Daten für Konsumenten (Live View, Storage Writer)
    # ------------------------------------------------------------------ #

    def register_reader(self) -> int:
        """Registriert einen neuen Live-Daten-Konsumenten am Ring Buffer.

        Raises:
            RuntimeError: falls aktuell keine Messung läuft.
        """
        with self._lock:
            if self._ring_buffer is None:
                raise RuntimeError("Es läuft aktuell keine Messung.")
            return self._ring_buffer.register_reader()

    def unregister_reader(self, reader_id: int) -> None:
        """Entfernt einen Live-Daten-Konsumenten (z. B. beim Schließen der Live View)."""
        with self._lock:
            if self._ring_buffer is not None:
                self._ring_buffer.unregister_reader(reader_id)

    def read_live_data(self, reader_id: int, max_samples: Optional[int] = None) -> np.ndarray:
        """Liest neue Rohdaten (UNSKALIERT) für einen registrierten Reader.

        Konsumenten müssen die Skalierung selbst über
        `core.measurement.apply_scaling(data, controller.active_channels)`
        anwenden.

        Raises:
            RuntimeError: falls aktuell keine Messung läuft.
        """
        with self._lock:
            if self._ring_buffer is None:
                raise RuntimeError("Es läuft aktuell keine Messung.")
            ring_buffer = self._ring_buffer
        # Bewusst AUSSERHALB des Locks: RingBuffer ist selbst thread-sicher;
        # so blockiert ein Lesevorgang nicht den Controller-Lock für andere
        # gleichzeitige Operationen (z. B. register_reader aus einem anderen Thread).
        return ring_buffer.read_new(reader_id, max_samples=max_samples)

    def get_ring_buffer(self) -> Optional[RingBuffer]:
        """Gibt den Ring Buffer der laufenden Messung zurück (für den Storage Writer).

        Wird von `data/exporter.py::StorageWriter` benötigt, um einen
        eigenen Reader zu registrieren, der garantiert keine Daten verliert.
        """
        with self._lock:
            return self._ring_buffer
