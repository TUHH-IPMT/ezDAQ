# Offene Punkte

Sammelstelle für erkannte, aber noch nicht umgesetzte Verbesserungen -
kein vollständiges Bugtracking, sondern Notizen, damit der Kontext nicht
verloren geht.

## NI9213: Abtastrate wird nicht gegen ADC-Timing-Modus geprüft [UMGESETZT]

Notiert: 2026-08-16, umgesetzt: 2026-08-16

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

**Umsetzung (2026-08-16):** Formel `fs_max = min(1 / (Wandlungszeit *
aktive Kanalzahl je physischem Modul), 100 S/s)` implementiert in
`data/models.py` (`NI9213_CONVERSION_TIME_S`, `ni9213_device_groups`,
`max_ni9213_sample_rate_hz`), Prüfung in `MeasurementConfig.__post_init__`
sowie GUI-Vorab-Check in `gui/setup_view.py` (Fehlermeldung
`error_ni9213_rate_too_high` in `gui/i18n.py`). Gruppiert korrekt pro
physischem Gerät (zwei separate NI9213-Module teilen sich keine
gemeinsame Wandlerbandbreite), nicht pauschal über die gesamte
Konfiguration.

**Nachtrag (2026-08-16): `AUTOMATIC`/`BEST_50_HZ_REJECTION`/
`BEST_60_HZ_REJECTION` komplett entfernt statt defensiv abgesichert.**
Grund: `ADC_TIMING_MODES` in `data/models.py` bot ursprünglich alle 5
DAQmx-Modi an (`nidaqmx.constants.ADCTimingMode` kennt tatsächlich alle
5 + `CUSTOM`, direkt im Paket verifiziert). Weder NI-MAX noch die
LabVIEW-Projekteigenschaften (und vermutlich auch DIAdem) bieten in
ihrer Bedienoberfläche aber mehr als `High Resolution`/`High Speed` zur
Auswahl an (siehe Screenshot-Beleg im
[NI-Community-Forum](https://forums.ni.com/t5/Multifunction-DAQ/NI-9213-Changing-from-High-Resolution-to-High-Speed-Screen-caps/td-p/2518188))
- die drei anderen Modi sind offenbar Nischen-/Experten-Optionen ohne
verlässlich auffindbare Wandlungszeiten. Statt sie mit einem
unverifizierten defensiven Fallback anzubieten, wurden sie aus
`ADC_TIMING_MODES`, `NI9213_CONVERSION_TIME_S`, der GUI-Combobox und den
Übersetzungen entfernt - Auswahl jetzt nur noch `HIGH_RESOLUTION`
(neuer Default, vorher `AUTOMATIC`) und `HIGH_SPEED`. Alte
Konfigurationsdateien mit einem der entfernten Werte werden nicht
aktiv abgelehnt (Rückwärtskompatibilität: `max_ni9213_sample_rate_hz`
fällt für unbekannte Werte weiterhin auf den
`HIGH_RESOLUTION`-Wandlungszeit zurück), sind aber über die GUI nicht
mehr neu wählbar.

**Verbleibende, unabhängig davon bestehende Unsicherheit:** Ein
Forumsbeitrag deutet an, dass die Kanalzahl in der realen NI-Formel
ggf. interne CJC-/Auto-Zero-Hilfskanäle mitzählt, die hier nicht
berücksichtigt sind (nur die vom Nutzer aktivierten TC-Kanäle werden
gezählt) - die App könnte dadurch bei sehr vielen aktiven Kanälen etwas
zu optimistisch sein. Sollte bei Gelegenheit mit echter Hardware
(NI-MAX-Testpanel) gegengeprüft werden.

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

**Grundziel (leitet alle Entscheidungen unten):** Verschiedene Module
sollen so gut wie möglich SYNCHRON gemessen werden. Mehrere Tasks sind
nur ein Mittel zum Zweck für die Fälle, in denen echte Synchronität
unmöglich ist (Ratenkonflikt, z. B. NI9210) - niemals der bequemere
Standardweg. Wo immer Module ratenkompatibel sind, bleibt ein
gemeinsamer Task mit echter Sample-Clock-Synchronität IMMER die
bevorzugte Lösung gegenüber mehreren unabhängigen Tasks, selbst wenn
Letzteres implementatorisch einfacher wäre.

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
- **Wichtig: Die Gruppierung ist NICHT "ein Task pro Modul", sondern
  "so viele Module wie möglich in einen gemeinsamen Task, nur bei
  echtem Ratenkonflikt aufteilen".** Zwei (oder mehr) Module mit
  kompatibler Rate - z. B. zwei NI9234, oder ein NI9234 zusammen mit
  einem NI9215 - MÜSSEN im selben Task bleiben, auch wenn eine
  Multi-Task-Architektur existiert. Grund: nur Kanäle im selben Task
  hängen an derselben physischen Sample-Clock-Leitung und werden damit
  exakt zeitgleich (phasenrichtig synchron) abgetastet - zwei separate
  Tasks mit z. B. beide "5000 Hz" eingestellt liefern KEINE
  Phasensynchronität, ihre Uhren laufen unabhängig und driften
  auseinander. Das ist bereits heute so - jede Kombination mehrerer
  Geräte landet aktuell automatisch in einem gemeinsamen Task
  (`core/controller.py`: `if len(devices) > 1: shared_task =
  NIDAQSharedTask()`) - und muss es auch nach Einführung mehrerer Tasks
  bleiben. Die neue "eigener Task"-Sonderbehandlung darf ausschließlich
  für nachweislich inkompatible Module greifen (aktuell nur NI9210),
  niemals als generelle "jedes Modul bekommt seinen eigenen Task"-Regel.
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

**Offene Unsicherheit - vor Umsetzung mit echter Hardware klären:** Was
DAQmx exakt tut, wenn man (ohne unsere Vorab-Validierung) ein NI9210
zusammen mit einem schnellen Modul in EINEN gemeinsamen Task zwingt,
konnte über Web-Recherche nicht eindeutig geklärt werden - zwei
mögliche, unterschiedlich schlimme Verhaltensweisen wurden gefunden,
keine davon eindeutig belegt für genau diesen Fall:
1. Task-Konfiguration schlägt hart fehl (`DaqError` bei
   `cfg_samp_clk_timing()`), oder
2. Die gesamte Task-Rate wird still auf das gekappt, was das
   langsamste Modul kann - dann liefe auch das schnelle Modul nur noch
   mit 14 S/s, nicht nur das NI9210 (ein Forumsbeleg deutet das für den
   NI9210-Einzelfall an, aber nicht explizit für den gemischten Fall).
   Ein gegenteiliger Forumsfall (NI9213 gemeinsam mit schnellen
   9229/9206-Modulen in einem 25-kS/s-Task) legt nahe, dass es
   möglicherweise doch nicht so einfach ist wie "ein Task = eine Uhr,
   die alle Kanäle gleich behandelt".
   Für die Sicherheit dieser App ist das aktuell irrelevant (unsere
   eigene Validierung verhindert den Versuch von vornherein), aber
   relevant für die Multi-Task-Architektur: **vor der Umsetzung mit
   echter Hardware testen** (NI9210 + z. B. NI9215 in einem Task, Rate
   ungleich 14 Hz anfordern, tatsächliches Verhalten/Fehlermeldung
   dokumentieren), statt sich auf die Architektur-Herleitung allein zu
   verlassen.

**Was zu tun ist (Reihenfolge):**
1. Hardware-Test wie oben (klärt, ob die Merge-Architektur überhaupt so
   nötig ist wie angenommen, oder ob DAQmx intern schon mehr abfängt
   als gedacht).
2. Task-Gruppierung in `core/controller.py`/`core/acquisition.py`
   umsetzen: Kanäle anhand der vorhandenen Validierungslogik in
   ratenkompatible Gruppen aufteilen (Standardfall weiterhin EIN
   Shared Task für alle kompatiblen Geräte, siehe Hinweis oben - nur
   bei echtem Konflikt ein zweiter Task), pro Gruppe eigener
   DAQ-Thread/Reader.
3. Merge/Forward-Fill-Baustein neu bauen (Eingang: mehrere Streams
   nativer Rate, Ausgang: eine Zeile pro Zeitpunkt der schnellsten
   Gruppe, langsame Kanäle per Zero-Order-Hold aufgefüllt) - schreibt
   in den bestehenden Ring Buffer, sodass Live View/Storage Writer/
   Analyse-Ansicht unverändert bleiben.
4. `data/models.py`: Metadaten um native Rate je Kanal ergänzen (für
   Rückmeldung an den Nutzer und als Vorsichtshinweis für die Analyse).
5. `gui/setup_view.py`/`gui/live_view.py`: tatsächlich verwendete Rate
   je Gruppe sichtbar zurückmelden statt wie DIAdem stillschweigend zu
   verschlucken.
6. `analysis/basic_analysis.py`: Rate-Bewusstsein für forward-gefüllte
   Kanäle (FFT/Filter warnen oder die native statt die gemergte Rate
   verwenden).

Noch nicht begonnen - reine Anforderungs-/Architektur-Notiz aus einer
längeren Diskussion.

## Zentraler Ratendienst / Task-Planer statt verstreuter Einzelprüfungen

Notiert: 2026-08-16

**Warum:** Die Ratenlogik steht aktuell DOPPELT im Code - einmal als harte
Validierung in `MeasurementConfig.__post_init__` (`data/models.py`), einmal
nochmal in `SetupView.build_current_config` (`gui/setup_view.py`) für die
benutzerfreundliche Fehlermeldung. Jedes neue Modul heißt: dieselbe Regel an
zwei Stellen pflegen (bei NI9234 und NI9213 gerade zweimal so gemacht).
Gleichzeitig braucht die geplante Multi-Task-Architektur (siehe Abschnitt
oben) ohnehin genau diese Information in strukturierter Form. Beides
zusammen spricht für einen zentralen Dienst, der aus **eingestellter
Abtastrate + aktueller Modul-/Kanalkonfiguration** die Task-Organisation
ableitet.

**Zwei Schichten:**

1. *Kapazität je physischem Modul* - eine einheitliche Antwort auf "welche
   Raten kannst du, bei dieser Kanalzahl und diesen Einstellungen?", statt
   der heutigen Einzelfunktionen:

   | Modul  | Kapazitätstyp                                    |
   |--------|--------------------------------------------------|
   | NI9215 | kontinuierlich bis 100 kHz                       |
   | NI9234 | diskretes Raster `51200/n`, n = 1..31            |
   | NI9213 | kontinuierlich bis `min(1/(t_conv·N), 100)` S/s   |
   | NI9210 | genau ein Wert: 14,0 S/s                          |

2. *Planer* - macht aus (gewünschte Rate, Kanalliste) einen Plan: welche
   Geräte teilen sich einen Task, welche echte Rate bekommt jede Gruppe,
   welche Kanäle werden hinterher forward-gefüllt. Ersetzt die verstreuten
   Prüfungen; GUI, Controller und Metadaten konsumieren denselben Plan.

**Festgelegte Politik (2026-08-16):**

- **Im Zweifel nach OBEN runden, nie nach unten.** Wenn die gewünschte Rate
  nicht auf das Raster eines Moduls passt, ist der nächstgrößere gültige
  Wert maßgeblich. Begründung: eine zu hohe Rate kostet nur Speicherplatz,
  eine zu niedrige verliert unwiederbringlich Signalanteile (das
  Antialiasing-Filter zieht mit der Rate mit). *Bereits umgesetzt* für das
  NI9234: `next_ni9234_sample_rate_at_or_above()` in `data/models.py`
  ersetzt in den Fehlermeldungen die frühere "nächstgelegener Wert"-Logik
  (die bei 20000 Hz noch 17066,7 vorgeschlagen hatte, jetzt 25600).
- **Nie stillschweigend korrigieren.** Der Nutzer muss die gültige Rate
  selbst eintragen; die App schlägt sie nur vor und lehnt bis dahin ab.
  Genau der DIAdem-/NI-MAX-Fallstrick, der diese ganze Diskussion ausgelöst
  hat (dort wird eine ungültige Rate ohne Hinweis gekappt). Gilt auch für
  den späteren Planer: er darf Gruppen bilden und Raten vorschlagen, aber
  nichts hinter dem Rücken des Nutzers ändern.

**Angenehme Folge für die Gruppierungsregel:** Mit "nach oben runden"
erübrigt sich der zuvor angedachte prozentuale Schwellwert ("wie viel
Ratenverlust ist Synchronität wert"). Ein Modul mit Raster zieht die
gemeinsame Gruppe nämlich nach OBEN, nicht nach unten - z. B. NI9215 +
NI9234 bei gewünschten 20000 Hz laufen gemeinsam bei 25600 Hz, ohne dass
irgendein Kanal langsamer wird als gewünscht. Damit bleibt eine klare,
schwellwertfreie Regel:

- Modul kann auf eine gemeinsame Rate **hochgehen** → bleibt im gemeinsamen
  Task (voll synchron, kein Verlust).
- Modul hat eine harte **Obergrenze unterhalb** der gewünschten Rate
  (NI9210 mit 14 S/s, NI9213 mit seinem `fs_max`) → eigener Task, sonst
  würde es alle anderen mit herunterziehen.

**Konkrete Fallunterscheidung der heute unterstützten Module** (geprüft,
nicht hergeleitet - relevante Grenzwerte: NI9234 langsamste Rate
1651,6 S/s, NI9213 absolute Obergrenze 100 S/s, NI9210 fest 14 S/s):

| Kombination              | Gemeinsame Rate? | Ergebnis                          |
|--------------------------|------------------|-----------------------------------|
| NI9234 + NI9215          | ja               | **ein Task** (z. B. beide 25600 Hz) |
| NI9215 + NI9213          | ja (≤ 100 S/s)   | **ein Task**                      |
| NI9215 + NI9210          | ja (14 S/s)      | **ein Task**                      |
| NI9234 + NI9213          | **nie**          | zwei Tasks nötig                  |
| NI9234 + NI9210          | **nie**          | zwei Tasks nötig                  |

Thermoelement- und IEPE-Modul können sich also grundsätzlich keinen Task
teilen - deren gültige Wertebereiche überschneiden sich bei keiner
denkbaren Abtastrate. Das NI9215 (kontinuierlich) passt dagegen zu jedem
anderen Modul.

**Heutige Lücke, die der Planer schließen soll:** Der Ratenvorschlag in der
Fehlermeldung wird nur von dem Modul berechnet, dessen Prüfung gerade
anschlägt - nicht über alle aktiven Module hinweg. Bei NI9234 + NI9213
schlägt die App deshalb weiterhin 25600 Hz vor; wer das einträgt, bekommt
prompt die nächste Fehlermeldung ("NI9213 unterstützt maximal 4,5 S/s").
Der Nutzer klappert also Einzelfehler ab, statt einmal die eigentliche
Aussage zu bekommen. Der Planer soll den Vorschlag global rechnen und im
Fall "keine gemeinsame Rate möglich" das direkt benennen (bzw. später:
automatisch zwei Taktgruppen planen), statt raten zu lassen.

Noch nicht begonnen (außer der bereits umgesetzten Aufrund-Politik beim
NI9234) - Anforderungsnotiz.
