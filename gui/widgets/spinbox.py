"""
gui/widgets/spinbox.py

`PrecisionDoubleSpinBox`: `QDoubleSpinBox`-Variante ohne künstliches
Nachkommastellen-Limit beim Eintippen/Einfügen, mit "sauberer" Anzeige
(keine überflüssigen Nachkommanullen) und ohne die deutsche System-Locale
als Fallstrick beim Einfügen von Werten mit Punkt als Dezimaltrennzeichen.

Hintergrund (siehe Kanalparameter-Dialog in `gui/widgets/channel_table.py`):
    Ein normaler `QDoubleSpinBox` koppelt über `setDecimals()` zwei
    unabhängige Dinge aneinander: wie viele Nachkommastellen beim
    Eintippen überhaupt akzeptiert werden UND wie viele beim Anzeigen
    ausgegeben werden (immer exakt diese Anzahl, mit Nachkommanullen
    aufgefüllt). Ein niedriger Wert (z. B. 4) begrenzt die Eingabe-
    präzision künstlich; ein hoher Wert lässt dafür selbst einen Wert wie
    2.0 als "2.0000000000" erscheinen.

    Zusätzlich verwendet ein `QDoubleSpinBox` ohne explizite Locale die
    System-Locale (`QLocale.system()`) zum Parsen von eingegebenem/
    eingefügtem Text - unter deutscher Windows-Locale ("," als Dezimal-,
    "." als Tausendertrennzeichen) wird ein eingefügter Wert wie "1.5"
    dadurch NICHT als 1,5 interpretiert, sondern als 1500 (!) - ein
    eingefügter Punkt-Dezimalwert wird so klammheimlich um den Faktor
    1000 verfälscht, ohne jede Fehlermeldung.

    Damit trotz erzwungener "C"-Locale (Punkt-Anzeige) auch die deutsche
    Tippgewohnheit ("," als Dezimaltrennzeichen) weiterhin funktioniert,
    wird ein eingegebenes/eingefügtes "," beim Validieren transparent wie
    "." behandelt - die Anzeige selbst bleibt aber bei "." (siehe
    `textFromValue`).
"""

from __future__ import annotations

from PyQt6.QtCore import QLocale
from PyQt6.QtGui import QValidator
from PyQt6.QtWidgets import QDoubleSpinBox, QWidget


class PrecisionDoubleSpinBox(QDoubleSpinBox):
    """`QDoubleSpinBox` mit hoher Eingabepräzision, sauberer Anzeige und
    Punkt ODER Komma als Dezimaltrennzeichen beim Eintippen/Einfügen,
    unabhängig von der System-Locale (Anzeige selbst immer mit Punkt).

    `decimals` (Standard 10) legt nur noch die *maximal* akzeptierte
    Eingabepräzision fest - `textFromValue()` zeigt trotzdem stets die
    kürzestmögliche Darstellung ohne überflüssige Nachkommanullen.
    """

    def __init__(self, decimals: int = 10, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # "C"-Locale erzwingt "." als Dezimaltrennzeichen beim Parsen von
        # eingegebenem/eingefügtem Text, unabhängig von der System-Locale
        # (siehe Moduldoc) - muss VOR setDecimals()/setValue() gesetzt
        # werden, damit der interne Validator sie von Anfang an nutzt.
        self.setLocale(QLocale(QLocale.Language.C))
        self.setDecimals(decimals)

    def validate(self, text: str, pos: int) -> tuple[QValidator.State, str, int]:  # noqa: N802
        # "," wie "." behandeln, BEVOR der (auf "."-Locale eingestellte)
        # Standard-Validator prüft - sonst würde ein getipptes/
        # eingefügtes "," als ungültiges Zeichen abgelehnt. Anzeige
        # springt dadurch beim Tippen sofort auf ".", was hier bewusst in
        # Kauf genommen wird (siehe Klassendoc).
        return super().validate(text.replace(",", "."), pos)

    def valueFromText(self, text: str) -> float:  # noqa: N802 - Qt-API
        # Zusätzliche Absicherung (z. B. bei programmatischem setText()
        # ohne den validate()-Pfad) - siehe validate().
        return super().valueFromText(text.replace(",", "."))

    def textFromValue(self, value: float) -> str:  # noqa: N802 - Qt-API
        text = f"{value:.{self.decimals()}f}"
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text
