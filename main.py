"""
main.py

Einstiegspunkt der ezDAQ-Anwendung.

Initialisiert Logging, den ConfigurationManager und den
MeasurementController und startet die Qt-GUI (Hauptfenster).

Start:
    python main.py
"""

from __future__ import annotations

import logging
import os
import sys


def configure_logging() -> None:
    """Konfiguriert das anwendungsweite Logging.

    Es wird bewusst `logging` statt `print` verwendet, damit Log-Level,
    Zeitstempel und spätere Datei-Logs zentral steuerbar sind (siehe
    Coding-Style-Vorgabe).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _set_windows_app_user_model_id() -> None:
    """Setzt unter Windows eine explizite AppUserModelID.

    Ohne explizite ID gruppiert Windows Python-Programme in der Taskbar
    häufig als "python.exe" und zeigt das Standard-Python-Icon.

    Der Bezeichner-Suffix ("v1") ist bewusst da: Windows cached das
    Taskleisten-Icon persistent pro AppUserModelID (auf Disk, übersteht
    auch einen Explorer-Neustart). Wurde während der Entwicklung schon
    einmal ohne (oder mit falschem) Icon unter derselben ID gestartet,
    bleibt das generische Icon sonst dauerhaft hängen, selbst wenn der
    Code jetzt korrekt ein eigenes Icon setzt. Ein neuer ID-String
    erzwingt einen frischen Cache-Eintrag.

    Historie: Unter dem früheren Namen "DAQSoftware" war die Zählung
    bereits bei "v3" angekommen (die Vorgänger-IDs waren jeweils mit
    inkonsistentem Icon-Zustand gestartet worden). Mit der Umbenennung
    auf "ezDAQ" beginnt ein eigener ID-Namensraum, daher wieder "v1".
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

    # Importe bewusst innerhalb von main(), damit ein reiner Import von
    # main.py (z. B. durch Tooling) nicht sofort PyQt6 lädt.
    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import QApplication

    from config.configuration_manager import ConfigurationManager
    from config.sensor_database import SensorDatabaseManager
    from config.settings import get_resource_path
    from core.controller import MeasurementController
    from gui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("ezDAQ")

    from gui.theme import init_theme
    init_theme(app)

    # .ico statt .png: enthaelt mehrere Aufloesungen (16-256px), die
    # Windows fuer Titelleiste/Taskleiste/Alt-Tab jeweils passend waehlt.
    # Eine einzelne 256px-PNG fuehrt auf manchen Windows-Systemen dazu,
    # dass das Taskleisten-Icon gar nicht angezeigt wird.
    icon_path = get_resource_path("icon.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    else:
        logger.warning("Anwendungs-Icon nicht gefunden unter %s", icon_path)

    configuration_manager = ConfigurationManager()
    sensor_database = SensorDatabaseManager()

    from gui.i18n import set_language
    set_language(configuration_manager.settings.language)

    from gui.theme import set_theme
    set_theme(configuration_manager.settings.theme)

    controller = MeasurementController(configuration_manager)

    window = MainWindow(controller, configuration_manager, sensor_database)
    window.show()

    exit_code = app.exec()
    logger.info("ezDAQ wurde beendet.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
