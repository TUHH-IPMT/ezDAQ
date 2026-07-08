"""
hardware/base_device.py

Abstrakte Schnittstelle für Hardware-Module.

Die GUI und der Messcontroller (`core/controller.py`) kommunizieren
AUSSCHLIESSLICH über diese Schnittstelle - niemals direkt mit `nidaqmx`
oder einer konkreten Geräteklasse. Das erlaubt es, später weitere Module
(z. B. NI 9411 Digital-I/O, ein Simulationsgerät für Tests ohne
Hardware, oder Geräte anderer Hersteller) hinzuzufügen, ohne GUI oder
Controller anzupassen.

Architektur (siehe Vorgabe):

    GUI -> Measurement Controller -> Hardware Interface -> nidaqmx -> NI cDAQ
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np

from data.models import Channel, DeviceInfo


class AcquisitionError(Exception):
    """Wird bei Fehlern während Konfiguration, Start oder Erfassung geworfen.

    Fasst nidaqmx-spezifische Fehler (`nidaqmx.DaqError`) in einen
    hardware-unabhängigen Fehlertyp, damit GUI/Controller nicht gegen
    `nidaqmx`-Exceptions programmieren müssen.
    """


class BaseDevice(ABC):
    """Abstrakte Basisklasse für ein einzelnes Hardware-Modul.

    Ein konkretes Gerät (z. B. `NI9215`, `NI9234`) kapselt die komplette
    Kommunikation mit der zugrunde liegenden Treiber-API. Diese Basisklasse
    definiert einen synchronen Lebenszyklus:

        configure() -> start() -> read() [wiederholt] -> stop()

    Die kontinuierliche Erfassung in einem eigenen Thread (DAQ Thread) und
    das Schreiben in den Ring Buffer sind NICHT Teil dieser Klasse, sondern
    Aufgabe von `core/acquisition.py`. Diese Trennung hält die
    Hardware-Schicht testbar (synchron, ohne Threading-Komplexität).
    """

    def __init__(self, device_info: DeviceInfo, channels: list[Channel]) -> None:
        """Initialisiert das Gerät mit Geräteinformationen und Kanalliste.

        Args:
            device_info: Beschreibung des physischen Moduls.
            channels: Zu erfassende Kanäle (nur `enabled`-Kanäle werden
                von den konkreten Implementierungen tatsächlich konfiguriert).
        """
        self.device_info = device_info
        self.channels = channels
        self._is_configured = False
        self._is_running = False

    @property
    def is_configured(self) -> bool:
        """True, sobald `configure()` erfolgreich aufgerufen wurde."""
        return self._is_configured

    @property
    def is_running(self) -> bool:
        """True, während die Erfassung läuft (zwischen `start()` und `stop()`)."""
        return self._is_running

    @property
    def active_channels(self) -> list[Channel]:
        """Nur die aktivierten Kanäle (siehe `Channel.enabled`)."""
        return [ch for ch in self.channels if ch.enabled]

    @abstractmethod
    def configure(self, sample_rate_hz: float, samples_per_read: int) -> None:
        """Konfiguriert die Hardware für eine Messung.

        Legt typischerweise den zugrunde liegenden Task an, fügt alle
        aktiven Kanäle hinzu und konfiguriert das Sample-Clock-Timing.

        Args:
            sample_rate_hz: Abtastrate in Hz.
            samples_per_read: Blockgröße für spätere `read()`-Aufrufe
                (bestimmt u. a. den Timing-Buffer der Hardware).

        Raises:
            AcquisitionError: falls die Konfiguration fehlschlägt
                (z. B. ungültiger Kanal, Gerät nicht erreichbar).
        """

    @abstractmethod
    def start(self) -> None:
        """Startet die Erfassung.

        Raises:
            AcquisitionError: falls das Gerät nicht konfiguriert ist
                oder der Start fehlschlägt.
        """

    @abstractmethod
    def stop(self) -> None:
        """Stoppt die Erfassung und gibt Hardware-Ressourcen frei.

        Muss idempotent sein (mehrfacher Aufruf darf nicht fehlschlagen).
        """

    @abstractmethod
    def read(self, samples_per_channel: int, timeout: float = 10.0) -> np.ndarray:
        """Liest einen Block von Rohwerten (unskaliert) von der Hardware.

        Args:
            samples_per_channel: Anzahl zu lesender Samples pro Kanal.
            timeout: Timeout in Sekunden, bevor ein Fehler geworfen wird.

        Returns:
            Array der Form (num_active_channels, samples_per_channel).

        Raises:
            AcquisitionError: bei Timeout oder Lesefehler.
        """

    def close(self) -> None:
        """Räumt Ressourcen auf. Idempotent, ruft `stop()` falls nötig."""
        if self._is_running:
            try:
                self.stop()
            except AcquisitionError:
                pass

    def __enter__(self) -> "BaseDevice":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
