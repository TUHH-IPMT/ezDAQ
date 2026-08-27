# -*- mode: python ; coding: utf-8 -*-
"""
ezDAQ.spec

PyInstaller build definition for ezDAQ - see the "Deployment" section of
the README for how to run it.

ONEDIR, deliberately not onefile: onefile unpacks the whole bundle
(several hundred MB, dominated by pyarrow/scipy/PyQt6) into %TEMP% on
EVERY start, which costs seconds of startup time and is a pattern virus
scanners regularly flag. Onedir starts immediately and can be updated by
replacing a folder. `config/settings.py::get_resource_path` supports
both modes (`sys._MEIPASS` vs. `sys.executable`), so switching back is
possible - the mode is the only thing that would have to change here.

NOT bundled, and not bundleable: the NI-DAQmx driver. It is a system
driver installed separately (administrator rights, reboot); `nidaqmx`
only loads its DLL at runtime. Every machine therefore needs the
NI-DAQmx runtime regardless of how this application is packaged - the
app itself starts without it and reports the missing driver in the
device browser (see `hardware/nidaq_device.py::discover_devices`).
"""

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

# pyqtgraph resolves parts of itself dynamically (e.g. the graphics
# backend and its widget/exporter registries), which the static import
# analysis cannot follow.
hiddenimports = collect_submodules("pyqtgraph")

# `nidaqmx/__init__.py` and `nitypes/__init__.py` both call
# `importlib.metadata.version(...)` on themselves at import time.
# PyInstaller does not bundle the .dist-info directories by default, so
# without this the very first `import nidaqmx` dies with
# "PackageNotFoundError: No package metadata was found for nitypes".
#
# That failure is easy to miss: `hardware/nidaq_device.py` catches
# ImportError and sets `NIDAQMX_AVAILABLE = False`, so the packaged
# application starts normally and merely claims the driver is not
# installed - on a machine where it IS installed.
metadata = copy_metadata("nidaqmx") + copy_metadata("nitypes")

# The gRPC stubs shipped by nidaqmx 1.6 are deliberately NOT collected:
# they need `grpc` and `google.protobuf`, which are not dependencies of
# this application (the local driver is addressed through ctypes).
# nidaqmx imports them lazily, so leaving them out costs nothing.

# PyQtGraph is configured with `useOpenGL=True`
# (see `gui/live_view.py`), which pulls PyOpenGL in only at runtime.
hiddenimports += [
    "OpenGL.platform.win32",
    "OpenGL.arrays.ctypesarrays",
    "OpenGL.arrays.numpymodule",
    "OpenGL.arrays.lists",
    "OpenGL.arrays.numbers",
    "OpenGL.arrays.strings",
]

analysis = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    # Icons and the splash logo, addressed via
    # `config/settings.py::get_resource_path`.
    datas=[("resources", "resources")] + metadata,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Test-only and interactive dependencies that would otherwise be
    # dragged in through pandas/scipy and inflate the bundle without ever
    # being used by the application.
    excludes=[
        "tkinter",
        "matplotlib",
        "IPython",
        "jupyter",
        "notebook",
        "pytest",
        "sphinx",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="ezDAQ",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # No console window - this is a GUI application. Note that anything
    # written to stdout/stderr is therefore lost; the application logs
    # through `logging` (see `main.py`).
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="resources/icon.ico",
)

coll = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    # UPX off on purpose: it compresses the Qt DLLs only marginally,
    # noticeably slows the first start, and is a recurring source of
    # false positives in virus scanners - which matters when the result
    # is rolled out to a number of lab PCs.
    upx=False,
    upx_exclude=[],
    name="ezDAQ",
)
