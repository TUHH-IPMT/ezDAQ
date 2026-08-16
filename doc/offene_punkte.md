# Offene Punkte

Sammelstelle für erkannte, aber noch nicht umgesetzte Verbesserungen -
kein vollständiges Bugtracking, sondern Notizen, damit der Kontext nicht
verloren geht.

## NI9213: Abtastrate wird nicht gegen ADC-Timing-Modus geprüft

Notiert: 2026-08-16

**Befund:** Der ADC-Timing-Modus des NI9213 (`Automatisch` / `Hohe
Auflösung` / `Hohe Geschwindigkeit` / `Beste 50-Hz-Unterdrückung` /
`Beste 60-Hz-Unterdrückung`) ist bereits pro Kanal einstellbar und wird
korrekt auf alle Kanäle desselben Moduls übertragen (siehe
`gui/widgets/channel_table.py`, `_apply_adc_timing_mode_to_module`).

Was fehlt: Die global eingestellte Abtastrate
(`gui/setup_view.py::_sample_rate_spin`, 1-100.000 Hz frei wählbar) wird
an keiner Stelle gegen den gewählten Timing-Modus geprüft. In
`gui/setup_view.py` taucht `NI9213` bisher gar nicht auf.

**Vergleichbarer, bereits behobener Fall:** Beim NI9234 (IEPE-
Beschleunigung) gab es dasselbe Problem - dort ließ sich jede beliebige
Abtastrate eingeben, obwohl der Delta-Sigma-ADC nur diskrete Werte nach
`fs = 51200 Hz / n` (n = 1..31) unterstützt. Behoben in Commit
`ed42b4f` (Validierung in `data/models.py`, Fehlermeldung in
`gui/setup_view.py`/`gui/i18n.py`), siehe dort als Vorlage für den
Lösungsansatz (Validierungsfunktion + Vorab-Check nur wenn das
jeweilige Modul aktiv genutzt wird).

**Warum das beim NI9213 nicht 1:1 übertragbar ist:** Die gültige Rate
hängt hier nicht nur vom Timing-Modus ab, sondern zusätzlich von der
Anzahl aktiver Kanäle (der NI9213-ADC ist zwischen den 16 Kanälen
multiplext, die Kanäle teilen sich die Wandlerbandbreite) - eine feste
Formel wie beim NI9234 reicht nicht aus.

**Bevor das umgesetzt wird:** Die genauen Grenzwerte pro Timing-Modus
(in Abhängigkeit von der Kanalzahl) müssen zuerst anhand des offiziellen
NI-9213-Datenblatts verifiziert werden, um keine falsche Validierung
einzubauen - analog zur Verifikation der 51200/n-Formel beim NI9234
vor dessen Umsetzung.

## Gleichzeitige Erfassung mit grundsätzlich inkompatiblen Abtastraten (z. B. NI9210 + NI9234)

Notiert: 2026-08-16

**Ausgangsproblem:** Ein NI9210 (fest 14 S/s, siehe
`NI9210_FIXED_SAMPLE_RATE_HZ`) lässt sich mit der aktuellen Architektur
niemals gemeinsam mit einem NI9234 (braucht i. d. R. deutlich höhere
Raten für Vibrationsmessung, siehe `is_valid_ni9234_sample_rate`) in
**einer** Messung betreiben. Grund: `core/controller.py` baut immer
genau einen gemeinsamen Task mit genau einer Abtastrate, sobald mehr
als ein Gerät aktiv ist (`if len(devices) > 1: shared_task =
NIDAQSharedTask()`, siehe `start_measurement()`). Es gibt kein Konzept
von "zwei Geräte, zwei unabhängige Raten, zwei Tasks".

**Aktuelles Verhalten (geprüft):** Die App blockiert diese Kombination
bereits zuverlässig - für jede denkbare Abtastrate schlägt entweder die
NI9210- oder die NI9234-Validierung in `MeasurementConfig.__post_init__`
an, da sich die beiden gültigen Wertebereiche (exakt 14,0 Hz vs.
1.651,6-51.200 Hz in diskreten Schritten) nirgends überschneiden. Die
Fehlermeldung nennt aber nie die eigentliche Ursache ("diese Module
können nicht kombiniert werden"), sondern springt je nach eingegebener
Rate zwischen den beiden Einzel-Fehlermeldungen hin und her - für
Nutzer verwirrend, auch wenn funktional nichts falsch gemacht wird.

**Realer Hintergrund (verifiziert):** Die 14 S/s des NI9210 sind eine
echte physikalische Obergrenze des Delta-Sigma-ADCs (NI-Datenblatt:
"maximum sample rate of 14 samples per second"), keine willkürliche
Software-Vorgabe. NI-MAX/DIAdem validieren das allerdings nicht sauber:
laut NI-Community wird eine zu hoch eingestellte Rate beim NI9210 ohne
Fehlermeldung still auf 14 S/s gekappt ("NI MAX does not raise an error
message when you set the Sample Rate too high" -
[forums.ni.com](https://forums.ni.com/t5/Multifunction-DAQ/NI-9213-Thermocouple-logging-rate/td-p/3795435)).
Das erklärt vermutlich auch den ursprünglichen Anlass dieser ganzen
Diskussion: eine frühere DIAdem-Messung, bei der 2000 Hz für ein NI9210
eingegeben wurde, DIAdem das anstandslos akzeptierte, die Hardware aber
weiterhin nur mit 14 S/s gewandelt hat, ohne das kenntlich zu machen.

**Wie DAQmx/DIAdem das grundsätzlich lösen:** Ein Task = eine
gemeinsame Sample-Clock-Rate ist eine generische DAQmx-Regel, die für
JEDE Gerätekombination gilt - nicht modulspezifisch hartkodiert. Um
unterschiedliche Raten gleichzeitig zu fahren, nutzt man einfach
**mehrere Tasks** (NI-Doku: "to acquire from two modules at multiple
rates you need to use two data acquisition objects"), optional über
einen gemeinsamen Hardware-Trigger für einen synchronen Start
verkoppelt. Reale Grenze bei (älteren) Gen-II-cDAQ-Chassis: maximal
zwei gleichzeitige Sync-Pulse-Signale, d. h. praktisch max. zwei
unabhängige Taktgruppen pro Chassis - für den Fall "eine langsame
Gruppe + eine schnelle Gruppe" ausreichend.

**Angedachte Architektur für dieses Tool ("Merge vor Ring Buffer"):**

```
9210-Task (14 S/s)     ─┐
                         ├─► Merge/Align (Forward-Fill) ─► Ring Buffer ─► Live View
schnelle Gruppe (2 kHz)─┘                                              ─► Storage Writer
```

- Kanäle werden automatisch nach Ratenkompatibilität gruppiert (anhand
  der bereits vorhandenen Validierungslogik - `NI9210_FIXED_SAMPLE_RATE_HZ`,
  `is_valid_ni9234_sample_rate`, künftig auch die NI9213-Prüfung von
  oben) - keine hartkodierte Modul-Kombinationsliste.
- Es bleibt bei **einem** Eingabefeld für die Abtastrate (wie heute/wie
  DIAdem). Dieser Zielwert wird pro Taktgruppe unabhängig interpretiert
  (geclippt/gerundet auf das, was die jeweilige Gruppe tatsächlich
  kann) - aber anders als DIAdem wird die tatsächlich verwendete Rate
  je Gruppe hinterher sichtbar zurückgemeldet (Metadaten, Live View),
  statt es stillschweigend zu verschlucken.
- Jede Taktgruppe bekommt einen eigenen Hardware-Task und eigenen
  DAQ-Thread/Reader, läuft mit ihrer eigenen nativen Rate weiter.
- Ein neuer Merge-Baustein zwischen den Hardware-Tasks und dem
  bestehenden Ring Buffer führt die Streams auf eine gemeinsame
  Zeitachse zusammen: pro "Tick" der schnellsten/nominellen Gruppe wird
  eine Zeile gebaut, langsamere Kanäle liefern dabei ihren zuletzt
  gültigen Wert erneut (Zero-Order-Hold/Plateau), bis ihr nächster
  echter Messwert eintrifft.
- Alles unterhalb des Ring Buffers (Live View, Storage Writer,
  Analyse-Ansicht) bleibt dadurch konzeptionell unverändert - sieht
  weiterhin nur "eine Zeile pro Zeitpunkt, alle Kanäle", genau wie bei
  einer heutigen Single-Rate-Messung.

**Zu beachtende Folgen, bevor das umgesetzt wird:**
1. Dateigröße: langsame Kanäle bekommen genauso viele Zeilen wie die
   schnelle Gruppe (viele Wiederholungen) - Parquet komprimiert das
   gut, CSV nicht.
2. Analyse-Vorsicht: FFT/Filter dürfen nicht blind auf einer
   hochgezogenen/forward-gefüllten Spalte laufen (künstliche Stufen im
   Spektrum) - braucht eine Kennzeichnung je Kanal (native Rate vs.
   nominelle/gemergte Rate) in den Metadaten, die die Analyse-Ansicht
   auswerten kann.

**Betroffene Bereiche (grobe Einschätzung, noch kein Umsetzungsplan im
Detail):** `core/controller.py`/`core/acquisition.py` (Task-Erzeugung
pro Gruppe statt immer genau ein Shared Task), neuer
Merge/Forward-Fill-Baustein, `data/models.py` (Metadaten für native
Rate je Kanal), `analysis/basic_analysis.py` (Rate-Bewusstsein),
`gui/setup_view.py`/`gui/live_view.py` (Rückmeldung der tatsächlichen
Rate je Gruppe). Noch nicht begonnen - reine Anforderungs-/
Architektur-Notiz aus einer längeren Diskussion.
