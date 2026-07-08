"""
config/settings.py

Definiert Speicherorte und Default-Werte für die persistente
Anwendungs-Konfiguration (Fenstergeometrie, zuletzt verwendete Hardware,
Benutzereinstellungen).

Dieses Modul enthält bewusst keine Lade-/Speicherlogik - das übernimmt
`config/configuration_manager.py`. Hier stehen ausschließlich Pfade und
reine Datenstrukturen.
"""

from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

APP_NAME = "DAQSoftware"

CONFIG_FILE_NAME = "settings.json"
CHANNEL_CONFIG_FILE_NAME = "last_channel_configuration.json"


def get_resource_path(*parts: str) -> Path:
    """Liefert den Pfad zu einer mitgelieferten Ressourcendatei (z. B. Icon).

    Funktioniert sowohl im Entwicklungsbetrieb (``python main.py``, Basis ist
    dann das Projektverzeichnis) als auch in einer mit PyInstaller gepackten
    Anwendung: Im "onefile"-Modus liegen mit ``--add-data`` gebündelte
    Dateien im temporären Entpackverzeichnis (``sys._MEIPASS``), im
    "onedir"-Modus (siehe README) neben der ``.exe`` (``sys.executable``).
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).parent)
    else:
        base = Path(__file__).resolve().parent.parent
    return base.joinpath("resources", *parts)


def get_config_directory() -> Path:
    """Liefert das plattformspezifische Verzeichnis für Konfigurationsdateien.

    Unter Windows wird ``%APPDATA%/DAQSoftware`` verwendet, da dies der
    Standardort für benutzerbezogene Anwendungsdaten ist. Auf anderen
    Plattformen (Entwicklungs-/Testumgebung) wird ``~/.config/DAQSoftware``
    als Fallback genutzt, damit main.py auch dort lauffähig bleibt.
    """
    appdata = os.environ.get("APPDATA")
    if appdata:
        base = Path(appdata)
    else:
        base = Path.home() / ".config"
    return base / APP_NAME


@dataclass
class WindowGeometry:
    """Persistierte Fenstergeometrie des Hauptfensters."""

    width: int = 1400
    height: int = 900
    pos_x: int = 100
    pos_y: int = 100
    maximized: bool = False


@dataclass
class AppSettings:
    """Persistente Benutzereinstellungen der Anwendung.

    Attributes:
        window: Zuletzt verwendete Fenstergeometrie.
        last_project_path: Pfad des zuletzt geöffneten Messprojekts.
        last_device_name: Name des zuletzt verwendeten NI-cDAQ-Geräts.
        default_sample_rate_hz: Vorbelegte Abtastrate fuer neue Messungen.
        default_storage_format: Vorbelegtes Speicherformat
            (Wert von ``data.models.StorageFormat``, z. B. "parquet").
        last_measurement_name: Zuletzt verwendeter Messname.
        last_live_only: Letzte Auswahl fuer "Nur Live anzeigen".
    """

    window: WindowGeometry = field(default_factory=WindowGeometry)
    last_project_path: Optional[str] = None
    last_storage_path: Optional[str] = None
    last_device_name: Optional[str] = None
    default_sample_rate_hz: float = 1000.0
    default_storage_format: str = "parquet"
    last_measurement_name: str = "messung_001"
    last_live_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialisiert die Einstellungen in ein JSON-kompatibles Dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppSettings":
        """Erstellt AppSettings aus einem Dictionary, robust gegen fehlende Felder.

        Fehlende oder unbekannte Felder fallen auf die Default-Werte zurück,
        damit ältere oder unvollständige Konfigurationsdateien nicht zu
        einem Absturz führen.
        """
        window_data = data.get("window", {}) or {}
        window = WindowGeometry(
            width=window_data.get("width", 1400),
            height=window_data.get("height", 900),
            pos_x=window_data.get("pos_x", 100),
            pos_y=window_data.get("pos_y", 100),
            maximized=window_data.get("maximized", False),
        )
        return cls(
            window=window,
            last_project_path=data.get("last_project_path"),
            last_storage_path=data.get("last_storage_path"),
            last_device_name=data.get("last_device_name"),
            default_sample_rate_hz=data.get("default_sample_rate_hz", 1000.0),
            default_storage_format=data.get("default_storage_format", "parquet"),
            last_measurement_name=data.get("last_measurement_name", "messung_001"),
            last_live_only=data.get("last_live_only", False),
        )
