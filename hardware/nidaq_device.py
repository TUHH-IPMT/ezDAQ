"""
hardware/nidaq_device.py

Gemeinsame nidaqmx-Task-Verwaltung für NI-cDAQ-Module.

Dieses Modul (zusammen mit `ni9215.py`/`ni9234.py`) ist die EINZIGE Stelle
der Anwendung, die `nidaqmx` direkt importiert und verwendet. GUI,
Messcontroller und Ring Buffer kennen ausschließlich `BaseDevice` aus
`hardware/base_device.py`.

Enthält außerdem `discover_devices()` zur Erkennung angeschlossener
NI-cDAQ-Module für die Setup-Ansicht.

Hinweis zur Entwicklungsumgebung:
    `nidaqmx` (das Python-Paket) kann auch ohne installierten NI-DAQmx-
    Treiber und ohne angeschlossene Hardware importiert werden - Methoden-
    signaturen wurden gegen die installierte Paketversion geprüft. Ein
    tatsächlicher Task-Aufruf (`nidaqmx.Task()`, `System.local()`, ...)
    schlägt jedoch ohne Treiber/Hardware mit einer Exception aus
    `nidaqmx.errors` fehl (z. B. `DaqNotFoundError` ohne Treiber,
    `DaqError` mit Treiber aber ohne Hardware). Diese Datei wurde daher
    NICHT gegen echte NI-9215/NI-9234-
    Hardware getestet - das war in dieser Umgebung nicht möglich. Ein
    Test mit echter Hardware wird dringend empfohlen, bevor produktiv
    gemessen wird.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from typing import Optional

import numpy as np

from data.models import Channel, DeviceInfo, ModuleType
from hardware.base_device import AcquisitionError, BaseDevice

logger = logging.getLogger(__name__)

try:
    import nidaqmx
    from nidaqmx.constants import AcquisitionType
    # nidaqmx.errors.Error ist die GEMEINSAME Basisklasse aller nidaqmx-
    # Fehler. Wichtig: DaqNotFoundError (fehlender NI-DAQmx-Treiber) und
    # DaqNotSupportedError erben NICHT von DaqError, sondern nur (wie
    # DaqError selbst) von Error. Ein `except DaqError` wuerde daher z.B.
    # den Fall "kein Treiber installiert" NICHT abfangen. Deshalb wird
    # hier durchgehend gegen die gemeinsame Basisklasse `Error` gefangen.
    from nidaqmx.errors import Error as DaqmxError
    from nidaqmx.stream_readers import AnalogMultiChannelReader
    from nidaqmx.system import System

    NIDAQMX_AVAILABLE = True
except ImportError:  # pragma: no cover - nur relevant ohne installiertes nidaqmx
    NIDAQMX_AVAILABLE = False

    class DaqmxError(Exception):
        """Fallback, falls `nidaqmx` nicht installiert ist."""

    AcquisitionType = None  # type: ignore[assignment]
    AnalogMultiChannelReader = None  # type: ignore[assignment]
    System = None  # type: ignore[assignment]
    nidaqmx = None  # type: ignore[assignment]


def discover_devices() -> list[DeviceInfo]:
    """Erkennt angeschlossene NI-cDAQ-Module über das lokale NI-DAQmx-System.

    Wird von der Setup-Ansicht aufgerufen, um dem Nutzer eine Liste
    verfügbarer Geräte/Module zur Auswahl anzuzeigen.

    Returns:
        Liste erkannter Geräte. Leer, falls `nidaqmx`/der NI-DAQmx-Treiber
        auf diesem Rechner nicht verfügbar ist - dies wird geloggt, führt
        aber nicht zum Absturz (z. B. relevant während der GUI-Entwicklung
        ohne angeschlossene Hardware).
    """
    if not NIDAQMX_AVAILABLE:
        logger.warning(
            "nidaqmx/NI-DAQmx-Treiber nicht verfügbar - Geräteerkennung "
            "liefert eine leere Liste."
        )
        return []

    devices: list[DeviceInfo] = []
    try:
        system = System.local()
        for device in system.devices:
            module_type = _map_product_type(device.product_type)
            try:
                phys_chs = [ch.name for ch in device.ai_physical_chans]
                num_channels = len(phys_chs)
            except DaqmxError:
                phys_chs = []
                num_channels = 0
            devices.append(
                DeviceInfo(
                    device_name=device.name,
                    product_type=device.product_type,
                    module_type=module_type,
                    num_channels=num_channels,
                    physical_channels=phys_chs,
                )
            )
    except DaqmxError as exc:
        logger.error("Fehler bei der Geräteerkennung: %s", exc)

    return devices


def _map_product_type(product_type: str) -> Optional[ModuleType]:
    """Ordnet eine NI-Produktbezeichnung (z. B. "NI 9215") einem ModuleType zu.

    Gibt None zurück, falls das Modul (noch) nicht unterstützt wird - die
    Setup-Ansicht kann solche Module dann als "nicht unterstützt" anzeigen,
    statt abzustürzen.
    """
    normalized = product_type.replace(" ", "").replace("-", "").upper()
    for module_type in ModuleType:
        if module_type.value in normalized:
            return module_type
    return None


class NIDAQSharedTask:
    """Verwaltet einen gemeinsamen nidaqmx-Task für mehrere Geräte."""

    def __init__(self) -> None:
        self._task: Optional["nidaqmx.Task"] = None
        self._reader: Optional["AnalogMultiChannelReader"] = None
        self._channel_count = 0
        self._configured = False
        self._started = False
        self._sample_rate_hz = 0.0
        self._samples_per_read = 0

    def configure(self, sample_rate_hz: float, samples_per_read: int) -> None:
        """Erzeugt den zugrunde liegenden Task.

        Das Sample-Clock-Timing wird bewusst NICHT hier konfiguriert:
        `cfg_samp_clk_timing()` schlägt mit "no devices in the task" fehl,
        solange der Task noch keine Kanäle enthält. Kanäle werden erst
        danach von `NIDAQDevice.configure()` (einmal pro Gerät, über
        `_add_channel_to_task()`) direkt zu `self._task` hinzugefügt; das
        Timing wird abschließend über `finalize()` konfiguriert, sobald
        alle Geräte ihre Kanäle hinzugefügt haben.
        """
        if not NIDAQMX_AVAILABLE:
            raise AcquisitionError("nidaqmx ist nicht installiert oder der NI-DAQmx-Treiber ist auf diesem System nicht verfügbar.")

        self._task = nidaqmx.Task()
        self._sample_rate_hz = sample_rate_hz
        self._samples_per_read = samples_per_read
        self._configured = True

    def finalize(self) -> None:
        """Konfiguriert das Sample-Clock-Timing, nachdem alle Geräte ihre
        Kanäle hinzugefügt haben. Muss genau einmal aufgerufen werden,
        bevor der Task gestartet wird."""
        if self._task is None:
            raise AcquisitionError("Shared task ist nicht konfiguriert.")
        if self._channel_count == 0:
            raise AcquisitionError("Shared task hat keine Kanäle.")

        try:
            self._task.timing.cfg_samp_clk_timing(
                rate=self._sample_rate_hz,
                sample_mode=AcquisitionType.CONTINUOUS,
                samps_per_chan=self._samples_per_read,
                source="OnboardClock",
            )
            self._reader = AnalogMultiChannelReader(self._task.in_stream)
        except DaqmxError as exc:
            raise AcquisitionError(
                f"Timing-Konfiguration des gemeinsamen Tasks fehlgeschlagen: {exc}"
            ) from exc

    def start(self) -> None:
        if self._task is None:
            raise AcquisitionError("Shared task ist nicht konfiguriert.")
        if self._started:
            # Mehrere Geräte teilen sich diesen Task und rufen start()
            # jeweils einzeln auf - ein erneuter Start des bereits
            # laufenden Tasks würde nidaqmx einen Fehler werfen lassen.
            return
        self._task.start()
        self._started = True

    def stop(self) -> None:
        if self._task is not None:
            self._task.stop()
            self._task.close()
            self._task = None
            self._reader = None
            self._configured = False
            self._started = False

    def read(self, samples_per_channel: int, timeout: float = 10.0) -> np.ndarray:
        if not self._configured or self._reader is None:
            raise AcquisitionError("Shared task ist nicht konfiguriert.")

        buffer = np.zeros((self._channel_count, samples_per_channel), dtype=np.float64)
        self._reader.read_many_sample(
            buffer,
            number_of_samples_per_channel=samples_per_channel,
            timeout=timeout,
        )
        return buffer


class NIDAQDevice(BaseDevice):
    """Basisklasse für NI-cDAQ-Module; verwaltet den Lebenszyklus eines
    einzelnen Tasks oder eines gemeinsamen Shared-Tasks.

    Konkrete Subklassen (`NI9215`, `NI9234`) implementieren ausschließlich
    `_add_channel_to_task()`, um den modulspezifischen nidaqmx-Kanaltyp
    hinzuzufügen (z. B. Spannung vs. IEPE-Beschleunigung). Task-Erzeugung,
    Timing-Konfiguration, Start/Stop und blockweises Lesen sind hier
    einmalig implementiert.
    """

    def __init__(self, device_info: DeviceInfo, channels: list[Channel]) -> None:
        super().__init__(device_info, channels)
        self._task: Optional["nidaqmx.Task"] = None
        self._reader: Optional["AnalogMultiChannelReader"] = None
        self._shared_task: Optional[NIDAQSharedTask] = None
        self._channel_offset = 0

        if not NIDAQMX_AVAILABLE:
            logger.warning(
                "nidaqmx ist nicht installiert - %s kann instanziiert, "
                "aber nicht konfiguriert/gestartet werden.",
                device_info.device_name,
            )

    def configure(
        self,
        sample_rate_hz: float,
        samples_per_read: int,
        sample_clock_source: str | None = None,
        shared_task: Optional[NIDAQSharedTask] = None,
    ) -> None:
        if not NIDAQMX_AVAILABLE:
            raise AcquisitionError(
                "nidaqmx ist nicht installiert oder der NI-DAQmx-Treiber "
                "ist auf diesem System nicht verfügbar."
            )
        if not self.active_channels:
            raise AcquisitionError(
                f"Gerät {self.device_info.device_name} hat keine aktiven Kanäle."
            )

        try:
            if shared_task is not None:
                # Mehrere Geräte teilen sich denselben Hardware-Task, damit
                # ihre Samples aus derselben Abtastung stammen.
                self._shared_task = shared_task
                if not self._shared_task._configured:
                    self._shared_task.configure(sample_rate_hz, samples_per_read)
                self._channel_offset = self._shared_task._channel_count
                for channel in self.active_channels:
                    # Bewusst dieselbe modulspezifische Methode wie im
                    # Standalone-Task-Pfad verwenden (siehe unten), damit
                    # Sensitivität, Messbereich, Anregungsstrom etc. auch
                    # im gemeinsamen Task korrekt gesetzt werden. Ein
                    # separates NIDAQSharedTask.add_channel() mit nur
                    # physical_channel/name würde für IEPE-Kanäle auf
                    # nidaqmx-Defaults zurückfallen (z. B. sensitivity=1000
                    # mV/g statt des konfigurierten Sensorwerts) und so
                    # falsche Messwerte liefern.
                    self._add_channel_to_task(self._shared_task._task, channel)
                    self._shared_task._channel_count += 1
                self._is_configured = True
                logger.info(
                    "%s konfiguriert als Teil eines gemeinsamen Tasks: %d Kanäle",
                    self.device_info.device_name,
                    len(self.active_channels),
                )
                return

            self._task = nidaqmx.Task()
            for channel in self.active_channels:
                self._add_channel_to_task(self._task, channel)

            timing_kwargs = {
                "rate": sample_rate_hz,
                "sample_mode": AcquisitionType.CONTINUOUS,
                "samps_per_chan": samples_per_read,
            }
            if sample_clock_source is None:
                timing_kwargs["source"] = "OnboardClock"
            else:
                timing_kwargs["source"] = sample_clock_source

            self._task.timing.cfg_samp_clk_timing(**timing_kwargs)

            self._reader = AnalogMultiChannelReader(self._task.in_stream)
            self._is_configured = True
            logger.info(
                "%s konfiguriert: %d Kanäle, %.1f Hz, %d Samples/Read",
                self.device_info.device_name,
                len(self.active_channels),
                sample_rate_hz,
                samples_per_read,
            )
        except DaqmxError as exc:
            self._cleanup_task()
            raise AcquisitionError(
                f"Konfiguration von {self.device_info.device_name} fehlgeschlagen: {exc}"
            ) from exc

    @abstractmethod
    def _add_channel_to_task(self, task: "nidaqmx.Task", channel: Channel) -> None:
        """Fügt einen einzelnen Kanal modulspezifisch zum Task hinzu.

        Muss von konkreten Subklassen implementiert werden, z. B. via
        `task.ai_channels.add_ai_voltage_chan(...)` (NI9215) oder
        `task.ai_channels.add_ai_accel_chan(...)` (NI9234).
        """

    def start(self) -> None:
        if not self._is_configured:
            raise AcquisitionError(
                f"{self.device_info.device_name} ist nicht konfiguriert. "
                f"configure() muss zuerst aufgerufen werden."
            )
        try:
            if self._shared_task is not None:
                self._shared_task.start()
            else:
                if self._task is None:
                    raise AcquisitionError(
                        f"{self.device_info.device_name} ist nicht konfiguriert."
                    )
                self._task.start()
            self._is_running = True
            logger.info("%s gestartet", self.device_info.device_name)
        except DaqmxError as exc:
            raise AcquisitionError(
                f"Start von {self.device_info.device_name} fehlgeschlagen: {exc}"
            ) from exc

    def stop(self) -> None:
        if self._shared_task is not None:
            try:
                self._shared_task.stop()
            except Exception as exc:
                logger.warning(
                    "Fehler beim Stoppen des gemeinsamen Tasks für %s (wird ignoriert): %s",
                    self.device_info.device_name,
                    exc,
                )
            finally:
                self._shared_task = None
        if self._task is not None:
            try:
                self._task.stop()
            except DaqmxError as exc:
                logger.warning(
                    "Fehler beim Stoppen von %s (wird ignoriert): %s",
                    self.device_info.device_name,
                    exc,
                )
            finally:
                self._cleanup_task()
        self._is_running = False
        self._is_configured = False
        logger.info("%s gestoppt", self.device_info.device_name)

    def read_shared_block(self, samples_per_channel: int, timeout: float = 10.0) -> np.ndarray:
        if self._shared_task is None:
            raise AcquisitionError(
                f"{self.device_info.device_name} hat keinen gemeinsamen Task."
            )
        if not self._is_running:
            self._shared_task.start()
            self._is_running = True
        return self._shared_task.read(samples_per_channel, timeout=timeout)

    def read_from_shared_block(self, shared_block: np.ndarray) -> np.ndarray:
        if self._shared_task is None:
            raise AcquisitionError(
                f"{self.device_info.device_name} hat keinen gemeinsamen Task."
            )
        start = self._channel_offset
        end = start + len(self.active_channels)
        return shared_block[start:end, :]

    def read(self, samples_per_channel: int, timeout: float = 10.0) -> np.ndarray:
        if not self._is_running:
            if self._shared_task is not None:
                self._shared_task.start()
                self._is_running = True
            else:
                raise AcquisitionError(
                    f"{self.device_info.device_name} läuft nicht - start() muss "
                    f"zuerst aufgerufen werden."
                )

        if self._shared_task is not None:
            block = self._shared_task.read(samples_per_channel, timeout=timeout)
            return self.read_from_shared_block(block)

        if self._reader is None:
            raise AcquisitionError(
                f"{self.device_info.device_name} läuft nicht - start() muss "
                f"zuerst aufgerufen werden."
            )

        buffer = np.zeros((len(self.active_channels), samples_per_channel), dtype=np.float64)
        try:
            self._reader.read_many_sample(
                buffer,
                number_of_samples_per_channel=samples_per_channel,
                timeout=timeout,
            )
        except DaqmxError as exc:
            raise AcquisitionError(
                f"Lesefehler bei {self.device_info.device_name}: {exc}"
            ) from exc
        return buffer

    def _cleanup_task(self) -> None:
        """Schließt den nidaqmx-Task und setzt interne Referenzen zurück."""
        if self._task is not None:
            try:
                self._task.close()
            except DaqmxError:
                pass
            self._task = None
        self._reader = None
