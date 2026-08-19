"""
gui/workers.py

Generic background worker for compute-intensive operations that would
otherwise block the GUI thread - e.g. device discovery
(`gui/main_window.py`) or file loading/analysis functions
(`gui/analysis_view.py`).

Deliberately implemented as its own `QThread` instead of `threading.Thread`
(unlike `core/acquisition.py::AcquisitionThread`/`data/exporter.py::StorageWriter`):
these workers are short-lived one-off jobs that report their result back to
the GUI thread via Qt signals - `QThread` is the natural choice for this,
since signal/slot connections across threads are already automatically
thread-safe (queued).
"""

from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtCore import QThread, pyqtSignal


class BackgroundWorker(QThread):
    """Runs a function in its own thread.

    Reports the result via `succeeded`, or an exception that occurred
    (only its text, see below) via `failed` - both signals are received
    thread-safely on the GUI thread, provided the connecting slot lives
    there (Qt's default behavior for signal/slot connections across
    threads).

    The caller MUST hold a reference to the worker until it finishes
    (e.g. in a list like `self._background_workers`) - otherwise Python
    could garbage-collect the object prematurely while the thread is
    still running.

    `failed` deliberately transmits only `str(exc)`, not the exception
    instance: exception objects are not guaranteed to be safely passable
    across threads (e.g. for some errors, traceback objects carrying
    references to thread-local frames are attached) - for display in the
    GUI, the text is enough.
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
        except Exception as exc:  # noqa: BLE001 - deliberately broad, error text goes to the GUI
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(result)
