<p align="center">
  <img src="resources/ezDAQ_logo_full.png" alt="ezDAQ logo" width="600">
</p>

# ezDAQ - Easy Data Acquisition

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Platform: Windows](https://img.shields.io/badge/platform-Windows-lightgrey.svg)

*[Deutsche Version](README.de.md)*

Windows desktop application for data acquisition and analysis with
NI cDAQ systems (NI 9215, NI 9234, NI 9210, NI 9213, NI 9235).

## Features (current state)

**Hardware & Acquisition**
- Device discovery and configuration for NI 9215 (±10 V voltage),
  NI 9234 (voltage or IEPE acceleration/microphone), NI 9210 (4-channel
  thermocouple), NI 9213 (16-channel thermocouple, J/K/T/E/N/R/S/B), and
  NI 9235 (8-channel 120 Ω quarter-bridge strain gauge)
- Modules the driver reports but this application does not (yet)
  recognize are flagged with a warning dialog on every device refresh,
  and their channels cannot be selected in the channel configuration
- Synchronized acquisition across multiple modules (shared nidaqmx task),
  including automatic splitting into independent, internally
  synchronized tasks when combined modules cannot share one sample clock
  (e.g. NI 9210's fixed 14 S/s, or NI 9234/NI 9235 sample-rate grids that
  don't intersect for the requested rate)
- Live data acquisition on a dedicated DAQ thread via a thread-safe ring
  buffer (designed for up to 100 kHz)
- Storage during measurement as Parquet (preferred) or CSV, including a
  `metadata` JSON file (start time, sample rate, hardware, channels,
  scaling, units)
- Per-channel parameters depending on signal type (scaling, offset,
  sensitivity, thermocouple type, gage factor/bridge type for strain)
  via a dedicated settings dialog per channel, including 2-point
  calibration for thermocouples

**Live View**
- Real-time visualization of multiple channels (PyQtGraph,
  OpenGL-accelerated)
- Freely configurable channel display (curve color, background, Y range,
  hybrid autoscaling) – part of the saved configuration
- Preview of configured channels before a measurement is even started

**Analysis View**
- Loading saved measurements (drag & drop or file picker), channel
  selection via tree view, zoom/pan, switchable plot layouts
- Analysis functions: FFT (frequency spectrum), low-pass/high-pass
  filters, smoothing (moving average) – results are stored as new
  channels under the source file and can be exported as CSV/Parquet
- RMS, statistics, and automatic reports are scaffolded but not yet
  implemented (see `analysis/basic_analysis.py`)

**Other**
- Multilingual (German/English) and light/dark theme, both switchable at
  runtime
- Project management (one project at a time) with `project.json`,
  `measurements/`, and `metadata/`
- Persistent application settings (window geometry, last-used
  hardware/channels, language, theme)
- Measurements can also be run entirely without the GUI from a Python
  script (`core/measurement_runner.py`, see `doc/messung_per_skript.md`)

## Installation

A virtual environment is recommended:

    python -m venv .venv
    .venv\Scripts\activate        # Windows
    pip install -r requirements.txt

To communicate with real hardware, the **NI-DAQmx driver** from National
Instruments must also be installed. Without the driver, the application
still starts – device discovery then returns an empty list, and starting
a measurement reports a clean error.

## Running

    python main.py

Alternatively, measurements can be run entirely without the GUI from your
own script, see `doc/messung_per_skript.md`.

## Architecture

The application is strictly layered; the GUI never talks to `nidaqmx`
directly:

    GUI  ->  MeasurementController  ->  Hardware Interface  ->  nidaqmx  ->  NI cDAQ

Data flow during a measurement:

    DAQ thread  ->  Ring buffer  ->  Live View
                                 ->  Storage Writer

Directories:

- `core/` – ring buffer, DAQ thread, measurement controller,
  channel/device logic, `MeasurementRunner` for GUI-less scripted use
- `hardware/` – hardware abstraction and NI cDAQ modules (`ni9215.py`,
  `ni9234.py`, `ni9210.py`, `ni9213.py`, `ni9235.py`) – the only place
  that touches `nidaqmx`
- `data/` – data models (`models.py`), metadata/projects, export
  (Parquet/CSV), loading saved measurements (`loader.py`, for the
  Analysis view)
- `gui/` – main window and views (Setup, Live, Analysis), theming
  (`theme.py`) and translations (`i18n.py`, DE/EN)
- `analysis/` – analysis functions (`basic_analysis.py`): FFT, filters,
  and smoothing are implemented; RMS, statistics, and reports are
  scaffolded but not yet implemented
- `config/` – persistent configuration
- `resources/` – application icon (`icon.png`/`icon.ico`), accessed at
  runtime via `config.settings.get_resource_path()`
- `doc/` – supplementary documentation (currently: scripted/headless
  measurement usage)

## Packaging as a portable Windows application (PyInstaller)

    pip install pyinstaller
    pyinstaller --noconfirm --windowed --name ezDAQ ^
        --icon resources\icon.ico --add-data "resources;resources" main.py

`--icon` sets the icon of the generated `.exe` (Explorer/taskbar),
`--add-data` bundles the `resources/` folder so `get_resource_path()` can
still find the icon at runtime in the packaged build (window/taskbar
icon, About dialog).

Note: `nidaqmx` loads the native NI-DAQmx library from the target system
at runtime, so the NI-DAQmx driver must also be installed on the target
machine. Depending on the PyInstaller version, an additional
`--hidden-import nidaqmx` or collecting `pyqtgraph` resources may be
necessary.

## Important note on hardware testing

The hardware layer (`hardware/nidaq_device.py`, `ni9215.py`, `ni9234.py`,
`ni9210.py`, `ni9213.py`, `ni9235.py`) was developed and checked against
the official `nidaqmx` API signatures. NI 9215, NI 9234, and NI 9210
(including combined multi-rate measurements with NI 9210's fixed
14 S/s alongside a faster module) have since been extensively verified
against real hardware. NI 9213 has **not** been tested against real
hardware so far. NI 9235 has been verified against real hardware for
device discovery, channel configuration, and sample-rate handling
(including a combined measurement with NI 9234 forcing an automatic
task split) – but **not yet with an actual strain gauge/bridge
connected** (only with an open/unconnected channel), so the accuracy of
real strain readings is still unverified. Testing with connected
hardware is strongly recommended before relying on this for production
measurements. All other layers (ring buffer, controller, storage, GUI)
have been tested end-to-end with simulated hardware.

## Authors

Malte Flehmke, Sebastian Junghans – originally developed at the Institute
of Production Management and Technology (IPMT), Hamburg University of
Technology (TUHH).

## Logo

The duck mascot logo (`resources/ezDAQ_logo_full.png`) was AI-generated
using ChatGPT.

## License

Released under the [GNU General Public License v3](LICENSE) (GPLv3).
