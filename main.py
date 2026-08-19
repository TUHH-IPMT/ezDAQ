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
_SPLASH_MIN_SECONDS = 1.5


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
    from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
    from PyQt6.QtWidgets import QApplication, QSplashScreen

    from config.configuration_manager import ConfigurationManager
    from config.sensor_database import SensorDatabaseManager
    from config.settings import get_resource_path
    from core.controller import MeasurementController
    from gui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("ezDAQ")

    from gui.theme import init_theme
    init_theme(app)

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
            pixmap = pixmap.scaledToWidth(
                420, Qt.TransformationMode.SmoothTransformation
            )
            # Own text line placed BELOW the logo instead of overlaid: the
            # logo already has the "ezDAQ / EASY DATA ACQUISITION" text
            # burned in at the bottom, so a `showMessage()` right at the
            # bottom edge of the image would overlap with it.
            padded = QPixmap(pixmap.width(), pixmap.height() + 28)
            padded.fill(QColor("white"))
            painter = QPainter(padded)
            painter.drawPixmap(0, 0, pixmap)
            painter.end()

            splash = QSplashScreen(padded)
            splash.show()
    else:
        logger.warning("Splash-Grafik nicht gefunden unter %s", splash_path)

    def _set_splash_status(message: str) -> None:
        """Displays `message` at the bottom of the splash and immediately
        processes pending paint events - without this, Qt would defer
        the repaint until the next event loop iteration, so the text
        would stay invisible for as long as the next (potentially slow)
        initialization step runs synchronously."""
        if splash is None:
            return
        splash.showMessage(
            message,
            Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter,
            QColor("#13233d"),
        )
        app.processEvents()

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
        remaining = _SPLASH_MIN_SECONDS - (time.monotonic() - splash_start)
        if remaining > 0:
            time.sleep(remaining)
        # Order matters: `window.show()` BEFORE `splash.finish()`,
        # otherwise the empty desktop briefly flashes before the main
        # window appears (Qt's example code for `QSplashScreen` follows
        # the same order).
        window.show()
        splash.finish(window)
    else:
        window.show()

    exit_code = app.exec()
    logger.info("ezDAQ wurde beendet.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
