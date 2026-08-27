"""
main.py

Entry point of the ezDAQ application.

Initializes logging, the ConfigurationManager, and the
MeasurementController, and starts the Qt GUI (main window).

Start:
    python main.py
"""

from __future__ import annotations

import logging
import os
import sys
import time

# Splash stays visible for at least this long, even if configuration/
# sensor database/main window finish faster - otherwise on fast machines
# it would flash by for only the blink of an eye and look more like a
# graphics glitch than deliberate startup feedback.
#
# Sized against the trace animation rather than picked freely: at
# `gui/splash.py::_TRACE_SPEED_HZ` = 0.5 one sweep takes two seconds,
# so anything below that cut the curve off mid-sweep and the animation
# never showed what it was.
_SPLASH_MIN_SECONDS = 3.0

# Number of steps the splash progress bar is divided into - must match
# the `_set_splash_status()` calls in `main()`.
_SPLASH_STEPS = 4


def configure_logging() -> None:
    """Configures application-wide logging.

    `logging` is deliberately used instead of `print`, so that log
    level, timestamps, and later file logs are centrally controllable
    (see coding style guideline).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _set_windows_app_user_model_id() -> None:
    """Sets an explicit AppUserModelID on Windows.

    Without an explicit ID, Windows often groups Python programs in the
    taskbar as "python.exe" and shows the default Python icon.

    The identifier suffix ("v1") is there deliberately: Windows caches
    the taskbar icon persistently per AppUserModelID (on disk, survives
    even an Explorer restart). If the app was ever started under the
    same ID without (or with the wrong) icon during development, the
    generic icon otherwise stays stuck permanently, even once the code
    correctly sets its own icon. A new ID string forces a fresh cache
    entry.

    History: under the earlier name "DAQSoftware", the count had
    already reached "v3" (the previous IDs had each been started with
    an inconsistent icon state). With the rename to "ezDAQ", a
    dedicated ID namespace begins, hence "v1" again.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "ezDAQ.App.v1"
        )
    except Exception:
        logging.getLogger(__name__).debug(
            "AppUserModelID konnte nicht gesetzt werden", exc_info=True
        )


def main() -> int:
    configure_logging()
    logger = logging.getLogger(__name__)
    logger.info("ezDAQ wird gestartet ...")
    _set_windows_app_user_model_id()

    # Imports deliberately inside main(), so a plain import of main.py
    # (e.g. by tooling) does not immediately load PyQt6.
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QIcon, QPixmap
    from PyQt6.QtWidgets import QApplication

    from config.configuration_manager import ConfigurationManager
    from config.sensor_database import SensorDatabaseManager
    from config.settings import APP_VERSION, get_resource_path, read_stored_theme
    from core.controller import MeasurementController
    from gui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("ezDAQ")

    from gui.theme import init_theme, set_theme
    init_theme(app)
    # Restore the stored theme BEFORE the splash is built. The full
    # configuration is only loaded behind the splash, so without this the
    # splash - and the first moments of the application - would be painted
    # with the default palette and flash white on every start under the
    # dark theme. The later `set_theme()` with the fully loaded settings
    # then finds the theme already active and returns early.
    set_theme(read_stored_theme())

    # .ico instead of .png: contains multiple resolutions (16-256px),
    # from which Windows picks the appropriate one for title bar/taskbar/
    # Alt-Tab. A single 256px PNG causes the taskbar icon to not be
    # displayed at all on some Windows systems.
    icon_path = get_resource_path("icon.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    else:
        logger.warning("Anwendungs-Icon nicht gefunden unter %s", icon_path)

    # Splash screen: PyQt6/pyqtgraph import as well as config/sensor
    # database loading take noticeable time before the main window
    # appears - without visible feedback, the app would look frozen
    # during this time.
    splash = None
    splash_path = get_resource_path("ezDAQ_logo_full.png")
    if splash_path.exists():
        pixmap = QPixmap(str(splash_path))
        if not pixmap.isNull():
            from gui.splash import StartupSplash

            pixmap = pixmap.scaledToWidth(
                420, Qt.TransformationMode.SmoothTransformation
            )
            # Trace band, progress bar, status line and version are drawn
            # BELOW the logo, never on top of it: the logo already carries
            # the "ezDAQ / EASY DATA ACQUISITION" text along its bottom
            # edge (see `gui/splash.py`).
            splash = StartupSplash(pixmap, f"v{APP_VERSION}", _SPLASH_STEPS)
            splash.show()
    else:
        logger.warning("Splash-Grafik nicht gefunden unter %s", splash_path)

    def _set_splash_status(message: str) -> None:
        """Advances the splash progress bar by one step and shows
        `message`.

        Repainting is handled by the splash itself - its animation
        timer redraws anyway, so the manual `processEvents()` this used
        to need is gone."""
        if splash is None:
            return
        splash.set_step(message)

    splash_start = time.monotonic()

    _set_splash_status("Lade Konfiguration ...")
    configuration_manager = ConfigurationManager()

    _set_splash_status("Lade Sensor-Datenbank ...")
    sensor_database = SensorDatabaseManager()

    from gui.i18n import set_language
    set_language(configuration_manager.settings.language)

    from gui.theme import set_theme
    set_theme(configuration_manager.settings.theme)

    _set_splash_status("Initialisiere Messsystem ...")
    controller = MeasurementController(configuration_manager)

    _set_splash_status("Baue Hauptfenster auf ...")
    window = MainWindow(controller, configuration_manager, sensor_database)

    if splash is not None:
        # `splash.wait()` instead of `time.sleep()`: the sleep blocked the
        # event loop, so the splash stood frozen for exactly the time it
        # was meant to be looked at - and the trace animation with it.
        splash.wait(_SPLASH_MIN_SECONDS - (time.monotonic() - splash_start))
        # Order matters: `window.show()` BEFORE the splash goes away,
        # otherwise the empty desktop briefly flashes before the main
        # window appears (Qt's example code for `QSplashScreen` follows
        # the same order).
        window.show()
        splash.finish_with_fade(window)
    else:
        window.show()

    exit_code = app.exec()
    logger.info("ezDAQ wurde beendet.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
