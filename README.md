# DAQSoftware

Schlanke Windows-Desktopanwendung zur Datenerfassung und Analyse von
Messdaten mit NI-cDAQ-Systemen (NI 9215, NI 9234) – als leichtgewichtige
Alternative zu umfangreichen Mess- und Analyseprogrammen.

## Funktionsumfang (aktueller Stand)

- Hardware-Konfiguration und Geräteerkennung (NI 9215 ±10 V, NI 9234 IEPE)
- Live-Datenerfassung in einem eigenen DAQ-Thread über einen
  thread-sicheren Ring Buffer (ausgelegt bis 100 kHz)
- Speicherung während der Messung als Parquet (bevorzugt) oder CSV,
  inklusive `metadata`-JSON (Startzeit, Abtastrate, Hardware, Kanäle,
  Skalierungen, Einheiten)
- Echtzeit-Visualisierung mehrerer Kanäle (PyQtGraph)
- Analyse-Ansicht mit Laden gespeicherter Messungen (Drag & Drop),
  Kanalauswahl, Zoom/Pan
- Projektverwaltung (ein Projekt gleichzeitig) mit `project.json`,
  `measurements/` und `metadata/`
- Persistente Anwendungseinstellungen (Fenstergeometrie, zuletzt
  verwendete Hardware/Kanäle)

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

## Architektur

Die Anwendung ist strikt geschichtet; die GUI kommuniziert niemals direkt
mit `nidaqmx`:

    GUI  ->  MeasurementController  ->  Hardware Interface  ->  nidaqmx  ->  NI cDAQ

Datenfluss während einer Messung:

    DAQ-Thread  ->  Ring Buffer  ->  Live View
                                 ->  Storage Writer

Verzeichnisse:

- `core/` – Ring Buffer, DAQ-Thread, Messcontroller, Kanal-/Geräte-Logik
- `hardware/` – Hardware-Abstraktion und NI-cDAQ-Module (einzige Stelle
  mit `nidaqmx`-Zugriff)
- `data/` – Datenmodelle, Metadaten/Projekte, Export (Parquet/CSV), Laden
- `gui/` – Hauptfenster und Ansichten (Setup, Live, Analyse)
- `analysis/` – vorbereitete Erweiterungspunkte (FFT, Filter, RMS,
  Statistik – noch nicht implementiert)
- `config/` – persistente Konfiguration

## Verpacken als portable Windows-Anwendung (PyInstaller)

    pip install pyinstaller
    pyinstaller --noconfirm --windowed --name DAQSoftware main.py

Hinweis: `nidaqmx` lädt die native NI-DAQmx-Bibliothek zur Laufzeit vom
Zielsystem; der NI-DAQmx-Treiber muss daher auch auf dem Zielrechner
installiert sein. Je nach PyInstaller-Version kann ein zusätzliches
`--hidden-import nidaqmx` bzw. das Einsammeln von `pyqtgraph`-Ressourcen
nötig sein.

## Wichtiger Hinweis zum Hardware-Test

Die Hardware-Schicht (`hardware/nidaq_device.py`, `ni9215.py`,
`ni9234.py`) wurde gegen die offiziellen `nidaqmx`-API-Signaturen
entwickelt und geprüft, jedoch **nicht gegen echte NI-9215/NI-9234-
Hardware** getestet. Ein Test mit angeschlossener Hardware wird dringend
empfohlen, bevor produktiv gemessen wird. Alle übrigen Schichten (Ring
Buffer, Controller, Speicherung, GUI) wurden mit simulierter Hardware
end-to-end getestet.
