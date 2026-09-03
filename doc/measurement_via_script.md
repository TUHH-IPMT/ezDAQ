# Controlling a Measurement from a Python Script

*[Deutsche Version](messung_per_skript.md)*

`MeasurementController` + `MeasurementRunner` can be used directly from your
own script, without the GUI. `MeasurementRunner` automatically takes care of
storage (if the configuration calls for it) including metadata, and - if
desired - the live display. You don't need to handle any of that yourself.

## 1. Setup

```python
from pathlib import Path

from config.configuration_manager import ConfigurationManager
from core.controller import MeasurementController
from core.measurement_runner import MeasurementRunner

configuration_manager = ConfigurationManager()
controller = MeasurementController(configuration_manager)
runner = MeasurementRunner(controller, storage_dir=Path("C:/Measurements"))
```

**Optional: with live display.** The same `LiveView` window as in the GUI -
**create it once and reuse it across all measurements**, don't reopen it per
measurement. If it's passed to the `Runner`, the runner automatically turns
the display on/off in `start()`/`stop()`:

```python
from PyQt6.QtWidgets import QApplication
from gui.live_view import LiveView

app = QApplication([])
live_view = LiveView(controller)
live_view.show()
runner = MeasurementRunner(controller, storage_dir=Path("C:/Measurements"), live_view=live_view)
```

Without `live_view`, the measurement runs exactly as it otherwise would,
with no window at all. (`runner.live_view = live_view` also works after the
fact, in case the window is created later.)

## 2. Configuration

```python
# Previously created in the GUI via "File -> Save configuration..."
config = configuration_manager.load_measurement_config(
    Path("C:/Measurements/my_setup.json")
)
```

Also works entirely without a saved configuration - just build a
`MeasurementConfig` object from `data/models.py` yourself (fields: `name`,
`sample_rate_hz`, `channels`, ...).

## 3. File naming

File naming lives in the configuration, not in the script: `naming` belongs
to `MeasurementConfig` just like `save_to_disk` and `storage_format`. A
configuration saved in the GUI therefore carries its naming scheme with it,
and `runner.start()` applies it by itself - there is nothing to pass in.

`config.name` is thus only the base name: `"probe"` becomes `probe_001`, the
next start `probe_002`, and so on. `runner.start()` resolves the name before
touching the hardware and puts it into the returned session and the metadata
file.

Without a saved configuration the scheme can be set directly:

```python
from data.models import NamingScheme

config.naming = NamingScheme(
    use_number_suffix=True,
    number_suffix_digits=3,
    include_date=True,
    include_time=False,
)
```

An existing measurement is never overwritten. If the name is taken and no
number suffix can free one up, `runner.start()` raises
`MeasurementNameConflict` from `data/naming.py` - the hardware has not been
started at that point, so the measurement is cleanly aborted.

## 4. Start/Stop

From here on it doesn't matter whether a live display is involved or not -
`runner` handles that internally:

```python
import time

runner.start(config)
try:
    time.sleep(10)  # replace with your own stop condition here
finally:
    runner.stop()
```

**Exception with a live display:** `time.sleep()` would freeze the window,
because Qt can't process events while it's sleeping. Keep it responsive with
`app.processEvents()` instead:

```python
deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    app.processEvents()
    time.sleep(0.01)
```

## 5. Starting/stopping repeatedly

`controller` and `runner` (and an optional `live_view`) are reusable - just
keep using the same objects in a loop, nothing needs to be recreated:

```python
for config in configurations:
    runner.start(config)
    time.sleep(10)
    runner.stop()
```

## Good to know

- **Whether the measurement actually made it to disk** is answered by the
  `StorageWriter` after `runner.stop()` - it does not raise, it reports:

  ```python
  writer = runner.storage_writer      # grab it BEFORE runner.stop()
  runner.stop()

  writer.last_error            # writing aborted (disk full etc.)
  writer.lost_samples          # ring buffer overrun: samples are missing
  writer.total_samples_written # 0 = no file was created at all
  ```

  `lost_samples` is the important one: after an overrun the file looks
  perfectly fine, because the time column is derived from the samples
  actually written and closes over the gap seamlessly.
- **Hardware errors during a measurement** do not surface as an exception
  from `start()`/`stop()` but through
  `controller.add_error_listener(callback)`. Without such a listener the
  measurement ends silently and the next start proceeds as if nothing had
  happened. The callback runs IN THE DAQ THREAD - only record there,
  evaluate in your own thread.
- Only **one measurement at a time** can run - otherwise `runner.start()`
  raises `RuntimeError`.
- For reading live data without a visible window, custom error handling, or
  how `MeasurementRunner` works internally: see the docstrings in
  `core/controller.py` and `core/measurement_runner.py`.
