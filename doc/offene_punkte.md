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
