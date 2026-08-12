"""
gui/workers.py

Generischer Hintergrund-Worker für rechenintensive Operationen, die sonst
den GUI-Thread blockieren würden - z. B. Geräteerkennung
(`gui/main_window.py`) oder Datei laden/Analysefunktionen
(`gui/analysis_view.py`).

Bewusst als eigener `QThread` statt `threading.Thread` (anders als
`core/acquisition.py::AcquisitionThread`/`data/exporter.py::StorageWriter`):
diese Worker sind kurzlebige Einzelaufträge, die ihr Ergebnis über
Qt-Signale an den GUI-Thread zurückmelden sollen - dafür ist `QThread` die
naheliegende Wahl, da Signal/Slot-Verbindungen über Threads hinweg bereits
automatisch thread-sicher (queued) sind.
"""

from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtCore import QThread, pyqtSignal


class BackgroundWorker(QThread):
    """Führt eine Funktion in einem eigenen Thread aus.

    Meldet das Ergebnis über `succeeded` bzw. eine aufgetretene Exception
    (nur deren Text, siehe unten) über `failed` zurück - beide Signale
    werden thread-sicher im GUI-Thread empfangen, sofern der verbindende
    Slot dort lebt (Standardverhalten von Qt bei Signal/Slot über
    Thread-Grenzen).

    Der Aufrufer MUSS eine Referenz auf den Worker halten, bis dieser
    fertig ist (z. B. in einer Liste wie `self._background_workers`) -
    andernfalls könnte Python das Objekt vorzeitig einsammeln, während der
    Thread noch läuft.

    `failed` überträgt bewusst nur `str(exc)`, keine Exception-Instanz:
    Exception-Objekte sind nicht garantiert thread-sicher weiterreichbar
    (z. B. hängen bei manchen Fehlern Traceback-Objekte mit Referenzen auf
    Thread-lokale Frames dran) - für die Anzeige in der GUI reicht der Text.
    """

    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self) -> None:
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:  # noqa: BLE001 - bewusst breit, Fehlertext geht an die GUI
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(result)
