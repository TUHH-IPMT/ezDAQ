"""
main.py

Einstiegspunkt der DAQSoftware-Anwendung.

Initialisiert Logging, den ConfigurationManager und den
MeasurementController und startet die Qt-GUI (Hauptfenster).

Start:
    python main.py
"""

from __future__ import annotations

import logging
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


def main() -> int:
    configure_logging()
    logger = logging.getLogger(__name__)
    logger.info("DAQSoftware wird gestartet ...")

    # Importe bewusst innerhalb von main(), damit ein reiner Import von
    # main.py (z. B. durch Tooling) nicht sofort PyQt6 lädt.
    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import QApplication

    from config.configuration_manager import ConfigurationManager
    from config.settings import get_resource_path
    from core.controller import MeasurementController
    from gui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("DAQSoftware")

    icon_path = get_resource_path("icon.png")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    else:
        logger.warning("Anwendungs-Icon nicht gefunden unter %s", icon_path)

    configuration_manager = ConfigurationManager()
    controller = MeasurementController(configuration_manager)

    window = MainWindow(controller, configuration_manager)
    window.show()

    exit_code = app.exec()
    logger.info("DAQSoftware wurde beendet.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
