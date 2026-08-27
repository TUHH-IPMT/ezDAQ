<p align="center">
  <img src="resources/ezDAQ_logo_full.png" alt="ezDAQ logo" width="600">
</p>

# ezDAQ - Easy Data Acquisition

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Platform: Windows](https://img.shields.io/badge/platform-Windows-lightgrey.svg)
[![DOI](https://zenodo.org/badge/1336784211.svg)](https://doi.org/10.5281/zenodo.21976180)

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
  script (`core/measurement_runner.py`, see `doc/measurement_via_script.md`)

## Architecture

The application is strictly layered; the GUI never talks to `nidaqmx`
directly:

    GUI  ->  MeasurementController  ->  Hardware Interface  ->  nidaqmx  ->  NI cDAQ

**Threading model:** acquisition runs on one or more dedicated DAQ
threads (`core/acquisition.py`), separate from the Qt GUI thread.
Longer-running operations that would otherwise block the UI - device
discovery, loading a saved measurement - run on short-lived background
worker threads (`gui/workers.py`) and report back to the GUI thread via
Qt signals.

Data flow during a measurement:

    DAQ thread(s)  ->  Ring buffer  ->  Live View
                                     ->  Storage Writer

**Multi-rate acquisition:** most C Series modules can share a single
DAQmx task and sample clock. A few can't - the NI 9210 has a
hardware-fixed rate of 14 S/s, and the NI 9234/NI 9235 each have their
own sample-rate grid (`fs = base / n`) that may not intersect with
another module's grid at the requested target rate. `resolve_rate_groups()`
(`data/models.py`) partitions the active channels into one or more
`RateGroup`s accordingly; each group becomes its own DAQmx task,
started in parallel (`core/controller.py`). Groups running slower than
the fastest one are merged onto its tick rate via zero-order-hold
forward-fill (`core/rate_merge.py::RateMerger`) before the combined
block reaches the (single) ring buffer - so everything downstream (Live
View, storage, analysis) sees one consistent, tick-rate-aligned stream,
with each channel's real native rate tagged in the measurement metadata.

Directories:

- `core/` – ring buffer, DAQ thread(s), measurement controller,
  rate-group resolution and merging, channel/device logic,
  `MeasurementRunner` for GUI-less scripted use
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
- `doc/` – supplementary documentation (scripted/headless measurement
  usage, an Arduino sketch for testing the serial trigger)

## Deployment (Windows executable)

For rolling ezDAQ out to several lab PCs, it is packaged with
PyInstaller so users do not need Python or a virtual environment.

**The NI-DAQmx driver cannot be bundled.** It is a National Instruments
system driver (administrator rights, usually a reboot); `nidaqmx` only
loads its DLL at runtime. Every machine therefore needs the NI-DAQmx
runtime installed separately, no matter how ezDAQ itself is packaged.
Without it the application still starts and reports the missing driver
in the device browser.

### Building the bundle

```
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean installer\ezDAQ.spec
```

The result is `dist\ezDAQ\` (roughly 310 MB, dominated by pyarrow,
PyQt6 and scipy) with `ezDAQ.exe` at its root.

`installer/ezDAQ.spec` uses **onedir**, not onefile: onefile unpacks the whole
bundle into `%TEMP%` on every start, which costs seconds of startup time
and is a pattern virus scanners regularly flag.
`config/settings.py::get_resource_path` supports both modes.

Build on the oldest Windows version that has to be supported - a bundle
built on Windows 11 runs on Windows 10, but not necessarily the other
way round.

### Distributing it

- **Simplest:** put `dist\ezDAQ\` on a network share and give users a
  shortcut to `ezDAQ.exe`. No installation, and an update is a folder
  replacement.
- **Installer:** `installer/installer.iss` builds one with
  [Inno Setup](https://jrsoftware.org/isinfo.php) (free, must be
  installed separately):

  ```
  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\installer.iss
  ```

  This produces `dist\ezDAQ-Setup-<version>.exe` with a Start menu
  entry and an uninstaller. At the start of the wizard the user chooses:

  - **for all users** - elevates, installs into `Program Files`, one
    copy shared by everyone. Right for a shared lab PC.
  - **for me only** - **no administrator rights**, installs into
    `%LOCALAPPDATA%\Programs\ezDAQ`. Right when the user does not have
    admin on their own machine. Costs the full bundle size per user
    profile.

  Either way is safe because ezDAQ never writes next to its executable -
  the configuration lives in `%APPDATA%\ezDAQ` and measurement data
  wherever the user chose.

  The NI-DAQmx driver always needs administrator rights, so a per-user
  install only removes that requirement for ezDAQ itself, not for
  getting a machine into a state where it can measure.

Note that an unsigned executable triggers a SmartScreen "unknown
publisher" warning on every machine. Either suppress it by policy or
sign the build with a code-signing certificate.

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
