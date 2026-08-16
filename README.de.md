# ezDAQ - Easy Data Acquisition

*[English version](README.md)*

Windows-Desktopanwendung zur Datenerfassung und Analyse von Messdaten mit
NI-cDAQ-Systemen (NI 9215, NI 9234, NI 9210, NI 9213).

## Funktionsumfang (aktueller Stand)

**Hardware & Erfassung**
- Geräteerkennung und -konfiguration für NI 9215 (±10 V Spannung),
  NI 9234 (Spannung oder IEPE-Beschleunigung/Mikrofon), NI 9210 (4-Kanal
  Thermoelement) und NI 9213 (16-Kanal Thermoelement, J/K/T/E/N/R/S/B)
- Synchronisierte Erfassung über mehrere Module hinweg (gemeinsamer
  nidaqmx-Task)
- Live-Datenerfassung in einem eigenen DAQ-Thread über einen
  thread-sicheren Ring Buffer (ausgelegt bis 100 kHz)
- Speicherung während der Messung als Parquet (bevorzugt) oder CSV,
  inklusive `metadata`-JSON (Startzeit, Abtastrate, Hardware, Kanäle,
  Skalierungen, Einheiten)
- Kanalparameter je nach Signaltyp (Skalierung, Offset, Sensitivität,
  Thermoelement-Typ) über einen eigenen Einstellungsdialog pro Kanal,
  inklusive 2-Punkt-Kalibrierung für Thermoelemente

**Live View**
- Echtzeit-Visualisierung mehrerer Kanäle (PyQtGraph, OpenGL-beschleunigt)
- Kanal-Darstellung frei konfigurierbar (Kurvenfarbe, Hintergrund,
  Y-Bereich, hybride Autoskalierung) – Teil der gespeicherten Konfiguration
- Vorschau der konfigurierten Kanäle bereits vor Messstart

**Analyse-Ansicht**
- Laden gespeicherter Messungen (Drag & Drop oder Dateiauswahl),
  Kanalauswahl per Baumansicht, Zoom/Pan, umschaltbare Plot-Layouts
- Analysefunktionen: FFT (Frequenzspektrum), Tiefpass-/Hochpassfilter,
  Glättung (gleitender Mittelwert) – Ergebnisse werden als neue Kanäle
  unter der Quelldatei abgelegt und lassen sich als CSV/Parquet speichern
- RMS, Statistik und automatische Reports sind vorbereitet, aber noch
  nicht implementiert (siehe `analysis/basic_analysis.py`)

**Sonstiges**
- Mehrsprachig (Deutsch/Englisch) und Hell/Dunkel-Theme, beides zur
  Laufzeit umschaltbar
- Projektverwaltung (ein Projekt gleichzeitig) mit `project.json`,
  `measurements/` und `metadata/`
- Persistente Anwendungseinstellungen (Fenstergeometrie, zuletzt
  verwendete Hardware/Kanäle, Sprache, Theme)
- Messungen auch ganz ohne GUI aus einem eigenen Python-Skript steuerbar
  (`core/measurement_runner.py`, siehe `doc/messung_per_skript.md`)

## Installation

Empfohlen wird eine virtuelle Umgebung:

    python -m venv .venv
    .venv\Scripts\activate        # Windows
    pip install -r requirements.txt

Für die Kommunikation mit echter Hardware muss zusätzlich der
**NI-DAQmx-Treiber** von National Instruments installiert sein. Ohne
Treiber startet die Anwendung dennoch – die Geräteerkennung liefert dann
eine leere Liste, und ein Messstart meldet einen sauberen Fehler.

## Start

    python main.py

Alternativ ganz ohne GUI aus einem eigenen Skript heraus steuerbar, siehe
`doc/messung_per_skript.md`.

## Architektur

Die Anwendung ist strikt geschichtet; die GUI kommuniziert niemals direkt
mit `nidaqmx`:

    GUI  ->  MeasurementController  ->  Hardware Interface  ->  nidaqmx  ->  NI cDAQ

Datenfluss während einer Messung:

    DAQ-Thread  ->  Ring Buffer  ->  Live View
                                 ->  Storage Writer

Verzeichnisse:

- `core/` – Ring Buffer, DAQ-Thread, Messcontroller, Kanal-/Geräte-Logik,
  `MeasurementRunner` für den GUI-losen Skript-Gebrauch
- `hardware/` – Hardware-Abstraktion und NI-cDAQ-Module (`ni9215.py`,
  `ni9234.py`, `ni9210.py`, `ni9213.py`) – einzige Stelle mit
  `nidaqmx`-Zugriff
- `data/` – Datenmodelle (`models.py`), Metadaten/Projekte, Export
  (Parquet/CSV), Laden gespeicherter Messungen (`loader.py`, für die
  Analyse-Ansicht)
- `gui/` – Hauptfenster und Ansichten (Setup, Live, Analyse), Theming
  (`theme.py`) und Übersetzungen (`i18n.py`, DE/EN)
- `analysis/` – Analysefunktionen (`basic_analysis.py`): FFT, Filter,
  Glättung implementiert; RMS, Statistik, Reports vorbereitet, aber noch
  nicht implementiert
- `config/` – persistente Konfiguration
- `resources/` – Anwendungs-Icon (`icon.png`/`icon.ico`), Zugriff zur
  Laufzeit über `config.settings.get_resource_path()`
- `doc/` – ergänzende Dokumentation (aktuell: Messung per Skript steuern)

## Verpacken als portable Windows-Anwendung (PyInstaller)

    pip install pyinstaller
    pyinstaller --noconfirm --windowed --name ezDAQ ^
        --icon resources\icon.ico --add-data "resources;resources" main.py

`--icon` setzt das Icon der erzeugten `.exe` (Explorer/Taskbar), `--add-data`
bündelt den `resources/`-Ordner mit, damit `get_resource_path()` das Icon
auch im gepackten Programm zur Laufzeit findet (Fenster-/Taskbar-Icon,
About-Dialog).

Hinweis: `nidaqmx` lädt die native NI-DAQmx-Bibliothek zur Laufzeit vom
Zielsystem; der NI-DAQmx-Treiber muss daher auch auf dem Zielrechner
installiert sein. Je nach PyInstaller-Version kann ein zusätzliches
`--hidden-import nidaqmx` bzw. das Einsammeln von `pyqtgraph`-Ressourcen
nötig sein.

## Wichtiger Hinweis zum Hardware-Test

Die Hardware-Schicht (`hardware/nidaq_device.py`, `ni9215.py`,
`ni9234.py`, `ni9210.py`, `ni9213.py`) wurde gegen die offiziellen
`nidaqmx`-API-Signaturen entwickelt und geprüft, jedoch **nicht
durchgängig gegen echte Hardware** getestet – NI 9210/NI 9213
(Thermoelement, inkl. 2-Punkt-Kalibrierung) bislang gar nicht. Ein Test
mit angeschlossener Hardware wird dringend empfohlen, bevor produktiv
gemessen wird. Alle übrigen Schichten (Ring Buffer, Controller,
Speicherung, GUI) wurden mit simulierter Hardware end-to-end getestet.

## Autoren

Malte Flehmke, Sebastian Junghans – ursprünglich entwickelt am IPMT,
TUHH.

## Logo

Das Enten-Maskottchen (`resources/ezDAQ_logo_full.png`) wurde mit
ChatGPT KI-generiert.

## Lizenz

Veröffentlicht unter der [GNU General Public License v3](LICENSE) (GPLv3).
