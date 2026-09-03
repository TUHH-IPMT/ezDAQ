# Messung per Python-Skript steuern

*[English version](measurement_via_script.md)*

`MeasurementController` + `MeasurementRunner` lassen sich ohne GUI direkt aus
einem eigenen Skript verwenden. `MeasurementRunner` übernimmt dabei
automatisch das Speichern (falls die Konfiguration das vorsieht) inklusive
Metadaten sowie - falls gewünscht - die Live-Anzeige. Darum musst du dich
nicht selbst kümmern.

## 1. Einrichtung

```python
from pathlib import Path

from config.configuration_manager import ConfigurationManager
from core.controller import MeasurementController
from core.measurement_runner import MeasurementRunner

configuration_manager = ConfigurationManager()
controller = MeasurementController(configuration_manager)
runner = MeasurementRunner(controller, storage_dir=Path("C:/Messungen"))
```

**Optional: mit Live-Anzeige.** Dasselbe `LiveView`-Fenster wie in der GUI -
**einmal erzeugen und über alle Messungen hinweg wiederverwenden**, nicht pro
Messung neu öffnen. Wird es dem `Runner` übergeben, schaltet dieser die
Anzeige bei `start()`/`stop()` automatisch mit ein/aus:

```python
from PyQt6.QtWidgets import QApplication
from gui.live_view import LiveView

app = QApplication([])
live_view = LiveView(controller)
live_view.show()
runner = MeasurementRunner(controller, storage_dir=Path("C:/Messungen"), live_view=live_view)
```

Ohne `live_view` läuft die Messung genau wie sonst auch, ganz ohne Fenster.
(`runner.live_view = live_view` geht auch nachträglich, falls das Fenster
erst später erzeugt wird.)

## 2. Konfiguration

```python
# Vorher in der GUI unter "Datei -> Konfiguration speichern..." erzeugt
config = configuration_manager.load_measurement_config(
    Path("C:/Messungen/mein_setup.json")
)
```

Geht auch ganz ohne gespeicherte Konfiguration - dafür einfach selbst ein
`MeasurementConfig`-Objekt aus `data/models.py` bauen (Felder: `name`,
`sample_rate_hz`, `channels`, ...).

## 3. Dateibenennung

Die Dateibenennung steht in der Konfiguration, nicht im Skript: `naming`
gehört zu `MeasurementConfig` wie `save_to_disk` und `storage_format`. Eine
in der GUI gespeicherte Konfiguration bringt ihr Namensschema also mit, und
`runner.start()` wendet es von selbst an - es gibt nichts durchzureichen.

`config.name` ist damit nur der Namensstamm: aus `"antasten"` wird
`antasten_001`, beim nächsten Start `antasten_002` und so weiter.
`runner.start()` löst den Namen auf, bevor die Hardware angefasst wird, und
schreibt ihn in die zurückgegebene Session und in die Metadatendatei.

Ohne gespeicherte Konfiguration lässt sich das Schema direkt setzen:

```python
from data.models import NamingScheme

config.naming = NamingScheme(
    use_number_suffix=True,
    number_suffix_digits=3,
    include_date=True,
    include_time=False,
)
```

Eine vorhandene Messung wird nie überschrieben. Ist der Name belegt und
sorgt kein Nummernsuffix für einen freien, wirft `runner.start()` ein
`MeasurementNameConflict` aus `data/naming.py` - die Hardware läuft dann
noch nicht, die Messung ist sauber abgebrochen.

## 4. Start/Stop

Ab hier ist es egal, ob eine Live-Anzeige beteiligt ist oder nicht - `runner`
kümmert sich intern darum:

```python
import time

runner.start(config)
try:
    time.sleep(10)  # hier durch die eigene Abbruchbedingung ersetzen
finally:
    runner.stop()
```

**Ausnahme mit Live-Anzeige:** `time.sleep()` würde das Fenster einfrieren
lassen, weil Qt währenddessen keine Events verarbeiten kann. Stattdessen mit
`app.processEvents()` wach halten:

```python
deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    app.processEvents()
    time.sleep(0.01)
```

## 5. Mehrfach starten/stoppen

`controller` und `runner` (und ein eventuelles `live_view`) sind
wiederverwendbar - einfach dieselben Objekte in einer Schleife
weiterbenutzen, nichts muss neu angelegt werden:

```python
for config in konfigurationen:
    runner.start(config)
    time.sleep(10)
    runner.stop()
```

## Gut zu wissen

- **Ob die Messung wirklich gespeichert wurde**, sagt der `StorageWriter`
  nach `runner.stop()` - er wirft nicht, sondern beantwortet Fragen:

  ```python
  schreiber = runner.storage_writer      # VOR runner.stop() merken
  runner.stop()

  schreiber.last_error            # Schreiben abgebrochen (Platte voll o.ae.)
  schreiber.lost_samples          # Ring-Buffer-Overrun: Werte fehlen
  schreiber.total_samples_written # 0 = es wurde gar keine Datei angelegt
  ```

  `lost_samples` ist der wichtigste davon: bei einem Overrun sieht die Datei
  unauffaellig aus, weil die Zeitspalte aus den *geschriebenen* Werten
  berechnet wird und die Luecke nahtlos schliesst.
- **Hardwarefehler waehrend der Messung** kommen nicht als Ausnahme aus
  `start()`/`stop()`, sondern ueber
  `controller.add_error_listener(callback)`. Ohne einen solchen Horcher
  endet die Messung still, und der naechste Start laeuft, als waere nichts
  gewesen. Der Rueckruf laeuft IM DAQ-Thread - dort nur merken, auswerten im
  eigenen Thread.
- Es kann immer nur **eine Messung gleichzeitig** laufen - `runner.start()`
  wirft sonst `RuntimeError`.
- Live-Daten ohne sichtbares Fenster selbst auslesen, eigene Fehlerbehandlung,
  oder wie `MeasurementRunner` intern funktioniert: siehe Docstrings in
  `core/controller.py` und `core/measurement_runner.py`.
