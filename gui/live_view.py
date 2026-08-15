"""
gui/live_view.py

Live View: Echtzeit-Darstellung der Messdaten während einer laufenden Messung.

Funktionen (siehe Vorgabe):
    * Echtzeitplots mehrerer Kanäle gleichzeitig (PyQtGraph)
    * Kanallegende (ein Subplot pro Kanal, da unterschiedliche
      physikalische Einheiten pro Kanal möglich sind - z. B. Kraft in N
      und Beschleunigung in g gemeinsam in einem Diagramm zu zeichnen
      wäre irreführend)
    * Zoom/Pan (nativ durch PyQtGraph)
    * Start/Stop, Messdauer, Samplingrate

Darstellungsart (Sweep, wie ein Oszilloskop):
    Die X-Achse jedes Subplots ist fensterbreit (Default 5 s) und scrollt
    NICHT kontinuierlich mit. Die Kurve zeichnet innerhalb dieses festen
    Fensters von links nach rechts durch; ist das Fenster voll, beginnt
    sofort ein neuer Durchlauf und die alte Kurve verschwindet komplett
    (siehe `_write_to_display_buffer`/`_get_channel_display_view`). Das
    unterscheidet sich bewusst von einem scrollenden Ringpuffer, bei dem
    alte Daten langsam am linken Rand herauslaufen.

    Die Achsenbeschriftung zeigt dabei die tatsächliche Messzeit (z. B.
    "40-45s" im 9. Durchlauf eines 5s-Fensters), nicht immer "0-5s" - der
    X-Bereich springt bei jedem neuen Durchlauf auf die nächste absolute
    Zeitspanne (siehe `_cycle_start_seconds`), auch wenn die Kurve selbst
    weiterhin bei x=Fensterstart neu beginnt.

Architektur-Hinweis (bewusst KEIN Reveal-Pacing):
    Der DAQ-Thread liefert neue Daten in Bloecken von ~25ms (Hardware-
    Read-Granularitaet, siehe
    `gui/setup_view.py::_calculate_samples_per_read` - bewusst nicht
    kleiner, sonst Pufferueberlauf-Risiko bei echter Hardware). Dadurch
    ist mit blossem Auge ein leicht "blockweises" Kurvenwachstum sichtbar.
    Ein Versuch, das ueber eine kuenstliche, zeitbasierte Nachzieh-
    Verzoegerung der Anzeige zu glaetten, wurde bewusst wieder verworfen:
    das fuehrte bei einem direkten Reiz-Reaktions-Test (Klopftest auf
    einen Beschleunigungssensor waehrend die App laeuft) zu spuerbarer
    zusaetzlicher Latenz. Fuer ein Live-Messinstrument ist Latenz
    wichtiger als Anzeige-Glaette - `_get_channel_display_view()` zeigt
    daher IMMER sofort den vollen aktuell eingetroffenen Stand.

Architektur-Hinweis (Performance):
    Der Anzeigepuffer für das aktuelle Sweep-Fenster ist ein einmalig
    vorallokiertes NumPy-Array (`_ensure_display_buffer`) - keine
    Allokationen pro Datenblock. Das ist für "normale" Laborabtastraten
    (bis einige kHz über mehrere Kanäle) ausreichend performant. Bei sehr
    hohen Abtastraten (z. B. 100 kHz über viele Kanäle) über lange
    Anzeigefenster würde ein Downsampling der Anzeigedaten (z. B.
    Min/Max-Dezimierung pro Pixel) die Zeichenlast weiter reduzieren -
    das ist als spätere Optimierung vorgesehen und hier bewusst noch
    nicht implementiert (Version 1).
"""

from __future__ import annotations

import logging
import weakref

import numpy as np
import pyqtgraph as pg
from PyQt6 import sip
from PyQt6.QtCore import QPoint, QRegularExpression, QSize, QTimer, pyqtSignal, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QIcon, QRegularExpressionValidator
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.controller import MeasurementController
from core.measurement import apply_scaling
from data.exporter import StorageWriter
from data.models import Channel, TriggerConfig, TriggerDirection, TriggerKind
from gui.i18n import connect_language_changed, get_language, t
from gui.theme import (
    ACTION_BUTTON_STYLE,
    PLAY_ICON_COLOR,
    RECORD_ICON_COLOR,
    TRIGGER_ARM_BUTTON_STYLE,
    axis_tick_point_size,
    connect_theme_changed,
    curve_color,
    draw_play_icon,
    draw_record_icon,
    draw_stop_icon,
    draw_trigger_icon,
    fix_toggle_button_width,
    is_position_on_screen,
    is_theme_default_plot_background,
    plot_background_color,
    plot_container_background_color,
    plot_foreground_color,
    repolish,
    style_plot_container,
    style_plot_item,
)
from gui.widgets.spinbox import NoWheelDoubleSpinBox, PrecisionDoubleSpinBox

logger = logging.getLogger(__name__)

# Ein Diagnose-Log zeigte: die eigentliche Datenverarbeitung pro Tick
# dauert unter 1,5ms, der Abstand zwischen Ticks lag aber durchgehend bei
# ~100-130ms statt der konfigurierten ~33ms. Isolierte Tests (QTimer allein,
# QTimer + DAQ-Thread, QTimer + DAQ-Thread + sichtbares Plot) haben den
# DAQ-Thread und Antialiasing als Ursache ausgeschlossen und das eigentliche
# SOFTWARE-Rendering von PyQtGraph (QGraphicsView/GraphicsLayoutWidget ohne
# GPU-Beschleunigung) als Flaschenhals identifiziert - `useOpenGL=True`
# (benoetigt PyOpenGL, siehe requirements.txt) hat den Tick-Abstand im
# Test von durchschnittlich ~89ms auf ~34ms gesenkt.
pg.setConfigOptions(antialias=True, useOpenGL=True)

_DEFAULT_DISPLAY_WINDOW_SECONDS = 5.0
_UI_UPDATE_INTERVAL_MS = 15  # ~66 Hz; mit useOpenGL=True ist Rendering nicht mehr der Flaschenhals
_STORAGE_UPDATE_INTERVAL_MS = 1000  # Dateizugriff (stat) seltener als das Plot-Update
# Grosse Messwertanzeige neben dem Subplot (siehe `Channel.plot_show_value`)
# in Hauptraster-Spalte und Popout-Fenster, je in EIGENER Spalte/Label fuer
# Zahl und Einheit. Die Zahl wird nach einem festen Format aus
# `Channel.plot_value_integer_digits` Vorkomma- + `_VALUE_DECIMALS`
# Nachkommastellen dargestellt (siehe `_format_channel_value`) - IMMER
# exakt gleich lang (Vorzeichen-Platz reserviert, nullaufgefuellt), sonst
# wuerde die Anzeige bei jedem neuen Wert leicht hin und her springen bzw.
# bei wachsender Ziffernanzahl abgeschnitten werden. Passt ein Wert NICHT
# in das konfigurierte Format, erscheinen statt einer irrefuehrend
# abgeschnittenen Zahl Rauten (wie bei einer DIAdem/LabVIEW-
# Digitalanzeige) - die Feldbreite wird daher direkt aus dem Format
# berechnet (siehe `_number_field_width_px`), nicht geraten. Die Einheit
# bekommt zusaetzlich ein EIGENES Label, das nur einmal beim Aufbau
# gesetzt und danach NIE mehr pro Tick aktualisiert wird - stuende sie
# stattdessen im selben Text wie die Zahl, wuerde sie bei jedem neuen
# Zahlenwert mit "hin und her springen", selbst innerhalb eines fest
# breiten Gesamtfelds.
_VALUE_DECIMALS = 3
_VALUE_NUMBER_POINT_SIZE = 18
_VALUE_UNIT_POINT_SIZE = 18
_VALUE_UNIT_WIDTH = 70

_STORAGE_WARN_PERCENT = 70.0
_STORAGE_CRITICAL_PERCENT = 90.0


def _format_bytes(num_bytes: float) -> str:
    """Formatiert eine Byte-Anzahl menschenlesbar (z. B. "12.3 MB")."""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < 1024.0:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def _channel_background_color(channel: Channel) -> str:
    """Hintergrundfarbe EINES Kanals fuer die eigentliche Plotflaeche
    (ViewBox) - Theme-Default, falls keine eigene Farbe konfiguriert ist.
    Gilt bewusst NUR fuer die Plotflaeche selbst, NICHT fuer die
    umgebende Messwert-/Einheit-Anzeige (siehe
    `gui/theme.py::plot_container_background_color`) - eine individuelle
    Kanalfarbe soll ausschliesslich dort sichtbar sein, wo tatsaechlich
    Daten landen."""
    return (
        plot_background_color()
        if is_theme_default_plot_background(channel.plot_background)
        else channel.plot_background or plot_background_color()
    )


def _channel_grid_color(channel: Channel) -> str:
    """Gitterlinienfarbe EINES Kanals - Theme-Standard (Vordergrundfarbe),
    falls keine eigene Farbe konfiguriert ist. Wird ueber
    `AxisItem.setTickPen()` gesetzt (siehe `_apply_channel_curve_style`) -
    PyQtGraph leitet Gitterlinien standardmaessig aus dem Achsen-Stift
    (`setPen()`, Achsentext-/Tickfarbe) ab; ein eigener `tickPen` erlaubt
    eine davon unabhaengige Farbe, ohne die Achsentext-/Tickstrich-Farbe
    selbst zu aendern (siehe `style_plot_item`)."""
    return channel.plot_grid_color or plot_foreground_color()


def _axis_label_style() -> dict[str, str]:
    """CSS-Style-Kwargs fuer `AxisItem.setLabel()` - gleiche Punktgroesse
    wie die Achsentick-Beschriftung (siehe `gui/theme.py::axis_tick_point_size`),
    statt PyQtGraph's kleinerem Default fuer Achsentitel. MUSS vor
    `axis.setPen()`/`style_plot_item()` gesetzt werden: `setLabel(**kwargs)`
    ERSETZT `labelStyle` komplett, `setPen()` ergaenzt darin nur die Farbe
    (`labelStyle['color']`) - bei umgekehrter Reihenfolge ginge die Farbe
    wieder verloren."""
    return {"font-size": f"{axis_tick_point_size()}pt"}


def _channel_axis_label(channel: Channel) -> str:
    """Y-Achsen-Beschriftung eines Kanals: Anzeigename, plus Einheit in
    eckigen Klammern falls vorhanden (siehe `axis_time`-Zeitachsen-Label
    fuer dieselbe Klammer-Konvention) - dieselbe Kombination wie der
    Plot-Titel (siehe `_rebuild_plots`/`ChannelPopoutWindow.__init__`),
    hier zusaetzlich direkt an der Achse."""
    unit_suffix = f" [{channel.unit}]" if channel.unit else ""
    return f"{channel.display_name}{unit_suffix}"


def _channel_display_key(channel: Channel) -> tuple[str, str]:
    """Eindeutiger Schlüssel für anzeige-bezogene Dicts/Caches (Dialog-
    Zeilen, Popout-Fenster-Verwaltung, Y-Bereich-Cache) - NICHT einfach
    `hardware_channel` allein.

    Die Live-Ansicht lässt sich bewusst auch OHNE angeschlossene Hardware
    konfigurieren und vorschauen (siehe `LiveView.preview_channels`) -
    mehrere noch nicht zugewiesene Kanäle hätten dann alle denselben
    leeren `hardware_channel`-Wert und würden sich in jedem darüber
    indizierten Dict gegenseitig überschreiben (z. B. mehrere "Eigenes
    Fenster"-Häkchen, die sich am Ende dasselbe Fenster teilen). Der
    zusätzliche `display_name` macht den Schlüssel auch in diesem Fall
    eindeutig - neu angelegte Kanäle sind bereits automatisch
    durchnummeriert ("Kanal 1", "Kanal 2", ...), siehe
    `gui/widgets/channel_table.py::_on_add_clicked`.
    """
    return (channel.hardware_channel, channel.display_name)


def _space_width_px(font: QFont) -> float:
    """Breite eines Leerzeichens in Pixeln fuer `font` - fester Abstand
    zwischen Messwert und Einheit (siehe `ChannelPopoutWindow`/
    `LiveView._make_value_box`), statt eines je nach Layout/Ausrichtung
    unterschiedlich grossen, "zufaelligen" Zwischenraums."""
    return QFontMetrics(font).horizontalAdvance(" ")


def _format_channel_value(
    value: float, integer_digits: int, decimals: int = _VALUE_DECIMALS
) -> str:
    """Formatiert `value` nach einem festen Zahlen-Format mit
    `integer_digits` Vorkomma- und `decimals` Nachkommastellen (siehe
    `Channel.plot_value_integer_digits`).

    IMMER exakt gleich lang - Vorzeichen-Platz reserviert (Leerzeichen bei
    positiven Werten statt eines fehlenden Zeichens) und Vorkommastellen
    mit Nullen aufgefuellt - sonst wuerde die Anzeige bei jedem neuen Wert
    (Vorzeichenwechsel, wachsende Ziffernanzahl) sichtbar hin und her
    springen (siehe `_rebuild_plots`/`ChannelPopoutWindow`).

    Passt der Wert NICHT in das Format (mehr Vorkommastellen als
    vorgesehen), wird statt einer irrefuehrend abgeschnittenen Zahl ein
    Rauten-Platzhalter angezeigt - wie bei einer DIAdem/LabVIEW-
    Digitalanzeige, deren Ziffernbreite ebenfalls fest konfiguriert ist.
    """
    sign = "-" if value < 0 else " "
    text = f"{abs(value):.{decimals}f}"
    int_part, _, frac_part = text.partition(".")
    if len(int_part) > integer_digits:
        int_part = "#" * integer_digits
        frac_part = "#" * decimals
    else:
        int_part = int_part.zfill(integer_digits)
    return f"{sign}{int_part}.{frac_part}" if decimals else f"{sign}{int_part}"


def _number_field_width_px(
    font: QFont, integer_digits: int, decimals: int = _VALUE_DECIMALS
) -> int:
    """Pixelbreite, die `_format_channel_value` fuer `font`/`integer_digits`
    maximal benoetigt (plus kleiner Sicherheitsabstand) - die feste
    Feldbreite wird so direkt aus dem konfigurierten Zahlenformat
    berechnet statt geraten (siehe Kommentar bei `_VALUE_DECIMALS`)."""
    mask = "-" + ("0" * integer_digits) + ("." + "0" * decimals if decimals else "")
    return QFontMetrics(font).horizontalAdvance(mask) + 10


_VALUE_FORMAT_PATTERN = QRegularExpression(r"0{1,6}(\.0{0,6})?")


def _value_format_text(integer_digits: int, decimal_digits: int) -> str:
    """Baut das im Dialog editierbare Format-Muster (z. B. "000.0000") aus
    Vorkomma-/Nachkommastellen - Kehrfunktion zu `_parse_value_format`."""
    text = "0" * integer_digits
    if decimal_digits:
        text += "." + "0" * decimal_digits
    return text


def _parse_value_format(text: str) -> tuple[int, int]:
    """Liest ein Format-Muster wie "000.0000" (siehe `_value_format_text`)
    zurück in (Vorkommastellen, Nachkommastellen) - toleriert leere/nicht
    exakt passende Eingaben (fällt auf mindestens 1 Vorkommastelle
    zurück), der `QRegularExpressionValidator` am Eingabefeld verhindert
    ohnehin die meisten Fehleingaben schon beim Tippen."""
    int_part, _, dec_part = text.partition(".")
    integer_digits = max(1, min(6, len(int_part)))
    decimal_digits = max(0, min(6, len(dec_part)))
    return integer_digits, decimal_digits


class ChannelDisplayDialog(QDialog):
    """Dialog zur Konfiguration von Kurvenfarbe, Hintergrundfarbe,
    Y-Bereich und Autoskalierungs-Verhalten - jeweils PRO KANAL.

    Wird über Optionen -> "Kanal-Darstellung festlegen..." geöffnet (siehe
    `gui/main_window.py::_build_menu`). Schon vor dem Messstart nutzbar
    (Kanäle kommen dafür aus der Setup-Konfiguration, siehe
    `gui/main_window.py::_on_open_channel_display_dialog`).

    "Autoskalierung" ist hier kein reines An/Aus: ist der Haken gesetzt,
    wird der eingestellte feste Bereich (Min/Max) verwendet, SOLANGE die
    tatsächlichen Messwerte darin liegen - überschreiten sie ihn, schaltet
    die Skalierung für diesen Kanal automatisch auf den tatsächlichen
    Wertebereich um (siehe `LiveView._apply_channel_y_range`). Ist der
    Haken NICHT gesetzt, bleibt der feste Bereich immer aktiv, egal was
    die Messwerte tun.

    Die "Eigenes Fenster"-Checkbox (wie alle anderen Felder hier) wirkt
    erst nach OK - anders als frühere Versionen dieses Dialogs öffnet ein
    Klick auf die Checkbox NICHT sofort ein Fenster. Das eigentliche
    Öffnen/Schließen übernimmt `LiveView._rebuild_plots()` anhand von
    `Channel.plot_popout`, nachdem `results()` über `OK` angewendet wurde
    (siehe `LiveView._apply_display_settings_to_live_channels`).
    """

    def __init__(
        self,
        channels: list[Channel],
        default_color: str,
        default_background: str,
        default_grid_color: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("channel_display_dialog_title"))

        self._colors: dict[tuple[str, str], str] = {}
        self._backgrounds: dict[tuple[str, str], str] = {}
        self._grid_colors: dict[tuple[str, str], str] = {}
        self._rows: dict[tuple[str, str], dict[str, QWidget]] = {}

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        for channel in channels:
            key = _channel_display_key(channel)
            hw_default_min = channel.min_range if channel.min_range is not None else -10.0
            hw_default_max = channel.max_range if channel.max_range is not None else 10.0
            current_min = channel.plot_y_min if channel.plot_y_min is not None else hw_default_min
            current_max = channel.plot_y_max if channel.plot_y_max is not None else hw_default_max
            self._colors[key] = channel.plot_color or default_color
            self._backgrounds[key] = channel.plot_background or default_background
            self._grid_colors[key] = channel.plot_grid_color or default_grid_color

            row = QHBoxLayout()

            # Betrifft NUR, ob der Kanal als Subplot im Hauptraster
            # erscheint (siehe `LiveView._rebuild_plots`) - Erfassung/
            # Speicherung laufen unabhängig davon unverändert weiter. Ganz
            # links platziert (statt hinten bei den anderen Haken): ist der
            # Kanal inaktiv, wird der gesamte Rest der Zeile ausgegraut
            # (siehe `_on_visible_toggled` unten) - die Reihenfolge soll
            # das widerspiegeln ("erst An/Aus, dann Details").
            visible_check = QCheckBox(t("plot_visible_checkbox"))
            visible_check.setToolTip(t("plot_visible_checkbox_tooltip"))
            visible_check.setChecked(channel.plot_visible)
            row.addWidget(visible_check)

            color_button = QPushButton()
            color_button.setFixedSize(24, 24)
            color_button.setToolTip(t("plot_color"))
            self._update_swatch(color_button, self._colors[key])
            color_button.clicked.connect(
                lambda _checked, k=key, b=color_button: self._pick_color(k, b, self._colors)
            )
            color_label = QLabel(f"{t('plot_color')}:")
            row.addWidget(color_label)
            row.addWidget(color_button)

            bg_button = QPushButton()
            bg_button.setFixedSize(24, 24)
            bg_button.setToolTip(t("plot_background"))
            self._update_swatch(bg_button, self._backgrounds[key])
            bg_button.clicked.connect(
                lambda _checked, k=key, b=bg_button: self._pick_color(k, b, self._backgrounds)
            )
            bg_label = QLabel(f"{t('plot_background')}:")
            row.addWidget(bg_label)
            row.addWidget(bg_button)

            grid_button = QPushButton()
            grid_button.setFixedSize(24, 24)
            grid_button.setToolTip(t("plot_grid_color"))
            self._update_swatch(grid_button, self._grid_colors[key])
            grid_button.clicked.connect(
                lambda _checked, k=key, b=grid_button: self._pick_color(k, b, self._grid_colors)
            )
            grid_label = QLabel(f"{t('plot_grid_color')}:")
            row.addWidget(grid_label)
            row.addWidget(grid_button)

            min_spin = PrecisionDoubleSpinBox()
            min_spin.setRange(-1e9, 1e9)
            min_spin.setValue(current_min)
            max_spin = PrecisionDoubleSpinBox()
            max_spin.setRange(-1e9, 1e9)
            max_spin.setValue(current_max)
            min_label = QLabel(f"{t('min')}:")
            max_label = QLabel(f"{t('max')}:")
            row.addWidget(min_label)
            row.addWidget(min_spin)
            row.addWidget(max_label)
            row.addWidget(max_spin)

            autoscale_check = QCheckBox(t("autoscale_checkbox"))
            autoscale_check.setToolTip(t("autoscale_checkbox_tooltip"))
            autoscale_check.setChecked(channel.plot_autoscale)
            row.addWidget(autoscale_check)

            time_window_spin = NoWheelDoubleSpinBox()
            time_window_spin.setRange(0.1, 3600.0)
            time_window_spin.setDecimals(1)
            time_window_spin.setSingleStep(0.5)
            time_window_spin.setValue(channel.plot_time_window_seconds)
            time_window_label = QLabel(f"{t('plot_time_window_seconds')}:")
            row.addWidget(time_window_label)
            row.addWidget(time_window_spin)

            show_value_check = QCheckBox(t("plot_show_value_checkbox"))
            show_value_check.setToolTip(t("plot_show_value_checkbox_tooltip"))
            show_value_check.setChecked(channel.plot_show_value)
            row.addWidget(show_value_check)

            # Format-Muster statt reiner Vorkommastellen-Zahl (z. B.
            # "000.0000") - Nullen vor dem Punkt = Vorkommastellen, Nullen
            # danach = Nachkommastellen (optional, ganz weglassbar für eine
            # reine Ganzzahl-Anzeige). Siehe `_parse_value_format`.
            value_format_edit = QLineEdit(
                _value_format_text(
                    channel.plot_value_integer_digits, channel.plot_value_decimal_digits
                )
            )
            value_format_edit.setValidator(QRegularExpressionValidator(_VALUE_FORMAT_PATTERN))
            value_format_edit.setMaximumWidth(80)
            value_format_edit.setToolTip(t("plot_value_integer_digits_tooltip"))
            value_format_label = QLabel(f"{t('plot_value_integer_digits')}:")
            row.addWidget(value_format_label)
            row.addWidget(value_format_edit)

            # Wirkt (wie "Aktiv" ganz links) erst nach OK über `results()` -
            # siehe Klassendoc oben.
            popout_check = QCheckBox(t("popout_button"))
            popout_check.setToolTip(t("popout_button_tooltip"))
            popout_check.setChecked(channel.plot_visible and channel.plot_popout)
            row.addWidget(popout_check)

            # Ist der Kanal inaktiv, ergibt der ganze Rest der Zeile keinen
            # Sinn (nichts davon wirkt sich sichtbar aus) - statt nur den
            # Popout-Haken zu sperren (bisheriges Verhalten), jetzt die
            # GESAMTE restliche Zeile ausgrauen.
            row_widgets = [
                color_label, color_button, bg_label, bg_button,
                grid_label, grid_button,
                min_label, min_spin, max_label, max_spin,
                autoscale_check, time_window_label, time_window_spin,
                show_value_check, value_format_label, value_format_edit,
                popout_check,
            ]

            def _on_visible_toggled(
                checked: bool,
                widgets: list[QWidget] = row_widgets,
                popout_checkbox: QCheckBox = popout_check,
            ) -> None:
                for widget in widgets:
                    widget.setEnabled(checked)
                if not checked:
                    popout_checkbox.setChecked(False)

            visible_check.toggled.connect(_on_visible_toggled)
            _on_visible_toggled(channel.plot_visible)

            form.addRow(channel.display_name, row)
            self._rows[key] = {
                "min": min_spin,
                "max": max_spin,
                "autoscale": autoscale_check,
                "time_window": time_window_spin,
                "show_value": show_value_check,
                "value_format": value_format_edit,
                "visible": visible_check,
                "popout": popout_check,
            }

        button_box = QDialogButtonBox()
        ok_button = button_box.addButton(t("ok"), QDialogButtonBox.ButtonRole.AcceptRole)
        cancel_button = button_box.addButton(t("cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        ok_button.clicked.connect(self.accept)
        cancel_button.clicked.connect(self.reject)
        layout.addWidget(button_box)

    def _pick_color(
        self, key: tuple[str, str], button: QPushButton, store: dict[tuple[str, str], str]
    ) -> None:
        initial = QColor(store.get(key, "#ffffff"))
        color = QColorDialog.getColor(initial, self)
        if not color.isValid():
            return
        store[key] = color.name()
        self._update_swatch(button, color.name())

    @staticmethod
    def _update_swatch(button: QPushButton, hex_color: str) -> None:
        button.setStyleSheet(f"background-color: {hex_color}; border: 1px solid #888888;")

    def results(self) -> dict[tuple[str, str], dict]:
        """Gibt die eingestellten Werte pro Kanal zurück (nur bei OK gültig).

        Schlüssel ist `_channel_display_key(channel)` (siehe dort), NICHT
        einfach `hardware_channel` - Format je Kanal passend zu
        `Channel.plot_*`/`ChannelTableWidget.apply_display_settings`/
        `LiveView._apply_display_settings_to_live_channels`.
        """
        results: dict[tuple[str, str], dict] = {}
        for key, row in self._rows.items():
            integer_digits, decimal_digits = _parse_value_format(row["value_format"].text())
            results[key] = {
                "plot_color": self._colors[key],
                "plot_background": self._backgrounds[key],
                "plot_grid_color": self._grid_colors[key],
                "plot_y_min": row["min"].value(),
                "plot_y_max": row["max"].value(),
                "plot_autoscale": row["autoscale"].isChecked(),
                "plot_time_window_seconds": row["time_window"].value(),
                "plot_show_value": row["show_value"].isChecked(),
                "plot_value_integer_digits": integer_digits,
                "plot_value_decimal_digits": decimal_digits,
                "plot_visible": row["visible"].isChecked(),
                "plot_popout": row["popout"].isChecked(),
            }
        return results


class ChannelPopoutWindow(QWidget):
    """Eigenständiges Fenster mit dem Live-Plot EINES einzelnen Kanals.

    Wird geöffnet, wenn die "Eigenes Fenster"-Checkbox im Kanal-
    Darstellung-Dialog per OK übernommen wurde (siehe
    `LiveView._rebuild_plots`/`_open_popout_window`). Hält bewusst KEINEN
    eigenen Timer und fragt den Ring Buffer nicht selbst ab - Kurve und
    Y-Bereich werden vom selben Timer-Tick wie die Haupt-Plots
    mitaktualisiert (siehe `LiveView._on_timer_tick`), damit nicht doppelt
    aus dem Ring Buffer gelesen wird.
    """

    def __init__(self, channel: Channel, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.hardware_channel = channel.hardware_channel
        # Schlüssel für `LiveView._popout_windows` - NICHT `hardware_channel`
        # allein (siehe `_channel_display_key`).
        self.display_key = _channel_display_key(channel)
        # Lebende Referenz auf dasselbe Channel-Objekt wie `LiveView`
        # (siehe `LiveView._open_popout_window`) - NICHT kopiert, damit
        # spaeter geaenderte Werte (z. B. `plot_background` ueber den
        # Kanal-Darstellung-Dialog) hier ohne Extra-Zutun sichtbar sind
        # (siehe `_style_value_labels`).
        self._channel = channel
        # Schliesst der Nutzer das Fenster, soll das C++/Qt-Objekt
        # tatsaechlich zerstoert werden (nicht nur versteckt) - darauf
        # reagiert `LiveView._on_popout_window_closed` ueber das
        # `destroyed`-Signal, um die eigene Nachverfolgung aufzuraeumen
        # UND (falls der Nutzer das Fenster direkt schliesst, statt die
        # Checkbox im Dialog zu nutzen) den Kanal wieder im Hauptraster
        # erscheinen zu lassen.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        unit_suffix = f" [{channel.unit}]" if channel.unit else ""
        title = f"{channel.display_name}{unit_suffix}"
        self.setWindowTitle(title)
        self.resize(640, 400)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(6, 6, 6, 6)
        row = QHBoxLayout()
        # KEIN Standard-Abstand zwischen Messwertanzeige und Plot: die
        # Luecke, die die QHBoxLayout-Default-Spacing sonst freilaesst,
        # gehoert dem FENSTER-Hintergrund (self), nicht der Kanalfarbe -
        # sichtbar als weiterer andersfarbiger Balken zwischen Einheit und
        # Plot (siehe `_value_container` fuer denselben Effekt zwischen
        # Zahl und Einheit).
        row.setSpacing(0)
        outer_layout.addLayout(row)

        # Grosse, aktuelle Messwertanzeige links neben dem Plot (siehe
        # `Channel.plot_show_value`/`LiveView._on_timer_tick`) - Zahl und
        # Einheit in ZWEI getrennten, fest breiten Labels (Breite direkt aus
        # `Channel.plot_value_integer_digits` berechnet, siehe
        # `_number_field_width_px`), sonst wuerde die Einheit bei jedem
        # neuen Zahlenwert sichtbar mitwandern. `unit_label` wird hier
        # einmalig gesetzt und danach NIE mehr pro Tick aktualisiert.
        #
        # BEIDE Labels stecken in einem GEMEINSAMEN Container-Widget
        # (`self._value_container`), NICHT direkt im `row`-Layout: der
        # Zwischenraum, den `QHBoxLayout.setSpacing()` zwischen ihnen
        # freilaesst, gehoert sonst zum FENSTER-Hintergrund (nicht zur
        # Kanalfarbe) und erscheint als sichtbarer, andersfarbiger Balken
        # zwischen Zahl und Einheit - der Container selbst bekommt daher in
        # `_style_value_labels()` dieselbe Hintergrundfarbe wie die beiden
        # Labels, sodass die Luecke farblich mit einschliesst.
        self._value_container = QWidget()
        value_row = QHBoxLayout(self._value_container)
        value_row.setContentsMargins(0, 0, 0, 0)

        number_font = QFont()
        number_font.setPointSize(_VALUE_NUMBER_POINT_SIZE)
        number_font.setBold(True)
        number_field_width = _number_field_width_px(
            number_font, channel.plot_value_integer_digits, channel.plot_value_decimal_digits
        )

        self.value_label = QLabel("--")
        self.value_label.setFixedWidth(number_field_width)
        self.value_label.setContentsMargins(0, 0, 0, 0)
        # RECHTS-ausgerichtet, nicht zentriert: der Abstand zur Einheit soll
        # exakt einer Leerzeichenbreite entsprechen (siehe
        # `value_row.setSpacing` unten) - zentriert waere der tatsaechliche
        # Zwischenraum vom umgebenden Leerraum der (fest formatierten,
        # siehe `_format_channel_value`) Zahl im Feld abhaengig.
        self.value_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        value_row.addWidget(self.value_label)

        self.unit_label = QLabel(channel.unit)
        self.unit_label.setFixedWidth(_VALUE_UNIT_WIDTH)
        self.unit_label.setContentsMargins(0, 0, 0, 0)
        self.unit_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        value_row.addWidget(self.unit_label)

        value_row.setSpacing(round(_space_width_px(number_font)))

        # Sichtbarkeit steuert NUR der Container (siehe
        # `LiveView._apply_channel_appearance`) - fuer die beiden Labels
        # selbst bleibt die individuelle Sichtbarkeit auf dem Qt-Default
        # (sichtbar), sie folgen ihrem Elternwidget ohnehin automatisch.
        self._value_container.setVisible(channel.plot_show_value)
        row.addWidget(self._value_container)

        self.plot_widget = pg.PlotWidget()
        self.plot_item = self.plot_widget.getPlotItem()
        self.plot_item.setTitle(title)
        self.plot_item.showGrid(x=True, y=True, alpha=0.3)
        # `units=` NICHT genutzt: PyQtGraph rendert das intern immer in
        # runden Klammern - fest "[s]" im Text selbst statt dessen, damit
        # die Zeiteinheit ueberall konsistent in eckigen Klammern steht.
        self.plot_item.setLabel("bottom", f"{t('axis_time')} [s]", **_axis_label_style())
        self.plot_item.setLabel("left", _channel_axis_label(channel), **_axis_label_style())
        style_plot_container(self.plot_widget)
        style_plot_item(self.plot_item)

        self.curve = self.plot_item.plot(pen=pg.mkPen(color=curve_color(), width=1.5))
        self.curve.setDownsampling(auto=True, method="mean")
        self.curve.setClipToView(True)
        self.plot_item.enableAutoRange(x=False)

        row.addWidget(self.plot_widget, stretch=1)

        self._retheme()
        connect_theme_changed(self._retheme)

    def _apply_number_width(self) -> None:
        """Passt die feste Breite von `value_label` an
        `Channel.plot_value_integer_digits` an - separat von
        `_style_value_labels`, da eine Aenderung der Vorkommastellen
        (anders als Farbe/Theme) die Feldbreite selbst betrifft (siehe
        `LiveView._apply_display_settings_to_live_channels`)."""
        font = QFont()
        font.setPointSize(_VALUE_NUMBER_POINT_SIZE)
        font.setBold(True)
        self.value_label.setFixedWidth(
            _number_field_width_px(
                font, self._channel.plot_value_integer_digits, self._channel.plot_value_decimal_digits
            )
        )

    def _style_value_labels(self) -> None:
        """Faerbt Text- UND Hintergrundfarbe von Zahl/Einheit ein - gemeinsam
        genutzt von `_retheme` (Theme-Wechsel) UND
        `LiveView._apply_channel_appearance` (Kanal-Darstellung-Dialog live
        geaendert).

        Hintergrund ist bewusst IMMER die Fenster-Hintergrundfarbe
        (`plot_container_background_color()`), NICHT die individuelle
        Kanalfarbe (`_channel_background_color`) - letztere gilt nur fuer
        die eigentliche Plotflaeche selbst (siehe `LiveView._rebuild_plots`).

        Faerbt AUCH `self._value_container` (nicht nur die beiden Labels
        selbst): der Zwischenraum, den `QHBoxLayout.setSpacing()` zwischen
        Zahl und Einheit freilaesst, gehoert dem Container, nicht den
        Labels - ohne dessen eigene Hintergrundfarbe bliebe dort ein
        sichtbarer, andersfarbiger Balken."""
        foreground = plot_foreground_color()
        background = plot_container_background_color()
        self._value_container.setStyleSheet(f"background-color: {background};")
        self.value_label.setStyleSheet(
            f"color: {foreground}; background-color: {background}; "
            f"font-size: {_VALUE_NUMBER_POINT_SIZE}pt; font-weight: bold;"
        )
        self.unit_label.setStyleSheet(
            f"color: {foreground}; background-color: {background}; "
            f"font-size: {_VALUE_UNIT_POINT_SIZE}pt;"
        )

    def _retheme(self) -> None:
        style_plot_container(self.plot_widget)
        style_plot_item(self.plot_item)
        self._style_value_labels()

    def moveEvent(self, event) -> None:  # noqa: N802 - Qt-API
        # Haelt `Channel.plot_popout_x/y` kontinuierlich mit der
        # tatsaechlichen Fensterposition synchron (nicht nur beim
        # Schliessen) - `self._channel` ist dieselbe lebende Referenz wie
        # in `LiveView` (siehe Klassendoc), Aenderungen sind also sofort
        # auch dort sichtbar. Wird spaeter (z. B. beim App-Beenden, siehe
        # `gui/main_window.py`) in die Setup-Kanaltabelle uebernommen und
        # so dauerhaft gespeichert.
        super().moveEvent(event)
        self._channel.plot_popout_x = self.x()
        self._channel.plot_popout_y = self.y()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt-API
        super().resizeEvent(event)
        self._channel.plot_popout_width = self.width()
        self._channel.plot_popout_height = self.height()


class LiveView(QWidget):
    """Zeigt Messdaten einer laufenden Messung in Echtzeit an.

    Signals:
        start_requested: Der Nutzer hat auf Play (nur Live-Anzeige) oder
            Aufnahme (mit Speicherung) geklickt - bool = `live_only`.
            `gui/main_window.py` startet die Messung dann mit der aktuell
            konfigurierten Setup-Konfiguration und passend gesetztem
            `MeasurementConfig.save_to_disk`.
        stop_requested: Der Nutzer hat auf Stop geklickt (nur klickbar,
            waehrend tatsaechlich etwas laeuft, siehe `set_start_enabled`).
            `gui/main_window.py` ist dafür zuständig, die Messung über
            den `MeasurementController` tatsächlich zu stoppen.
        trigger_fired: Ein scharf geschalteter Schwellwert-Trigger (siehe
            `enter_armed_state`) hat ausgelöst - `gui/main_window.py`
            erzeugt daraufhin den StorageWriter (ggf. rückwirkend, siehe
            `_on_trigger_fired`).
        trigger_arm_toggled: Nutzer hat den Scharf-Button geklickt (siehe
            `gui/setup_view.py::trigger_arm_toggled` - identisches
            Gegenstück hier in der Live-Ansicht, damit beide Buttons
            gleichzeitig bedienbar sind).
    """

    start_requested = pyqtSignal(bool)  # live_only
    stop_requested = pyqtSignal()
    trigger_fired = pyqtSignal()
    trigger_arm_toggled = pyqtSignal(bool)

    def __init__(self, controller: MeasurementController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller

        self._reader_id: int | None = None
        self._channels: list[Channel] = []
        self._sample_rate_hz: float = 1.0
        self._display_window_seconds = _DEFAULT_DISPLAY_WINDOW_SECONDS

        # Sweep-Anzeigepuffer für den AKTUELLEN Durchlauf (Oszilloskop-Art:
        # die Kurve zeichnet von links nach rechts durch das Zeitfenster;
        # am rechten Rand beginnt ein neuer Durchlauf bei x=0, die alte
        # Kurve verschwindet komplett). `_channel_buffer_positions` ist
        # zugleich die Anzahl gültiger Samples im aktuellen Durchlauf pro
        # Kanal (siehe `_write_to_display_buffer`/`_get_channel_display_view`).
        self._display_buffer: np.ndarray | None = None
        self._display_capacity_samples: int = 0
        self._buffer_write_pos: int = 0
        self._channel_buffer_positions: dict[int, int] = {}
        # Absolute Messzeit (Sekunden seit Messstart), bei der der AKTUELLE
        # Durchlauf des jeweiligen Kanals begonnen hat - die
        # Achsenbeschriftung soll die echte Messzeit zeigen (z. B. "40-45s"
        # statt immer "0-5s"), auch wenn der Sweep selbst weiterhin bei
        # jedem Durchlauf zurücksetzt. Pro Kanal, da die Fensterlänge
        # (`Channel.plot_time_window_seconds`) pro Kanal unterschiedlich
        # sein kann und die Durchläufe damit unabhängig voneinander enden
        # (siehe `_write_to_display_buffer`).
        self._channel_cycle_starts: dict[int, float] = {}
        # Zuletzt pro Kanal auf die Plots angewendeter
        # `_channel_cycle_starts`-Wert (siehe `_on_timer_tick`) - der
        # X-Bereich wird nur bei einem tatsächlichen Zyklus-Wechsel neu
        # gesetzt, nicht bei jedem Tick.
        self._channel_x_range_applied: dict[int, float | None] = {}

        # Darstellung pro Kanal (Kurvenfarbe, Hintergrund, Y-Bereich,
        # Autoskalierungs-Verhalten) lebt direkt auf den `Channel`-Objekten
        # selbst (`plot_color`/`plot_background`/`plot_y_min`/`plot_y_max`/
        # `plot_autoscale`, siehe `data/models.py`) - dadurch nimmt jeder
        # `Channel` seine Darstellung automatisch mit (auch beim
        # Speichern/Laden der Konfiguration), ohne dass die Live View
        # eigene, separat zu pflegende Zuordnungs-Dicts bräuchte. Siehe
        # `open_channel_display_dialog`, Menüpunkt in
        # `gui/main_window.py::_build_menu`.

        # Pro Kanal, ob die Y-Achse GERADE (dieser Tick) im Autoscale-Modus
        # ist oder den festen Bereich nutzt - nur zur Vermeidung
        # unnötiger `setYRange`/`enableAutoRange`-Aufrufe, wenn sich am
        # effektiven Modus nichts geändert hat (siehe
        # `_apply_channel_y_range`).
        self._channel_y_auto_active: dict[tuple[str, str], bool] = {}

        self._plot_widget = pg.GraphicsLayoutWidget()
        self._plot_items: list = []
        self._curves: list = []
        # `self._curves[i]`/`self._plot_items[i]`/`self._value_labels[i]`
        # gehören zum Kanal `self._channels[self._curve_channel_indices[i]]`
        # - NICHT mehr zwangsläufig `self._channels[i]`, seit unsichtbare
        # Kanäle (`Channel.plot_visible=False`) keinen Subplot mehr
        # bekommen (siehe `_rebuild_plots`).
        self._curve_channel_indices: list[int] = []
        # Grosse, aktuelle Messwertanzeige neben jedem Subplot im
        # Hauptraster (siehe `_make_value_box`/`_rebuild_plots`/
        # `_on_timer_tick`) - eigene Fenster (`ChannelPopoutWindow`) haben
        # ihre eigenen, gleichnamigen Attribute auf der Fenster-Instanz
        # selbst. `_value_boxes[i]`/`_value_unit_boxes[i]` sind die
        # `ViewBox`en (Hintergrundfarbe/Sichtbarkeit), `_value_labels[i]`/
        # `_value_unit_labels[i]` die darin zentrierten `TextItem`s
        # (Textinhalt) - `_value_unit_labels[i]` wird NUR beim Aufbau
        # gesetzt und nie pro Tick neu geschrieben (siehe `_VALUE_UNIT_WIDTH`).
        self._value_boxes: list = []
        self._value_labels: list = []
        self._value_unit_boxes: list = []
        self._value_unit_labels: list = []

        # Eigene Fenster einzelner Kanäle (siehe `ChannelPopoutWindow`,
        # `_on_popout_requested`), nach `_channel_display_key()` (NICHT
        # `hardware_channel` allein - siehe dort) - unabhängig von
        # `plot_visible`: ein Kanal kann im Hauptraster ausgeblendet UND
        # trotzdem in einem eigenen Fenster sichtbar sein. Eigener
        # Autoscale-Zustands-Cache (siehe `_apply_channel_y_range`), damit
        # sich Popout und Hauptraster-Subplot eines Kanals nicht
        # gegenseitig die Skalierung "wegcachen".
        self._popout_windows: dict[tuple[str, str], ChannelPopoutWindow] = {}
        self._popout_y_auto_active: dict[tuple[str, str], bool] = {}

        # StorageWriter der laufenden Messung (None bei "Nur Live anzeigen"
        # UND waehrend der Scharf-Phase eines Schwellwert-/seriellen
        # Triggers, bevor er ausgeloest hat - siehe `attach_storage_writer`).
        self._storage_writer: StorageWriter | None = None

        # Zustand fuer automatische Mess-Trigger (siehe
        # `data/models.py::TriggerConfig`, `enter_armed_state`). Hardware-
        # Erfassung + Anzeige laufen waehrend der Scharf-Phase bereits, nur
        # der StorageWriter fehlt noch (siehe
        # `gui/main_window.py::_on_start_measurement`). Start UND Stopp
        # sind unabhaengig konfigurierbar (siehe `TriggerConfig`) - daher
        # getrennte Kanal-Indizes/Flankendetektoren fuer beide Seiten.
        self._trigger_config: TriggerConfig | None = None
        self._armed: bool = False
        self._start_trigger_channel_index: int | None = None
        self._stop_trigger_channel_index: int | None = None
        # None = noch kein Tick seit dem jeweiligen Reset-Punkt beobachtet -
        # verhindert ein sofortiges Ausloesen, falls der Kanal zu diesem
        # Zeitpunkt bereits jenseits der Schwelle liegt (siehe
        # `_check_threshold_trigger`/`_check_stop_threshold_trigger`).
        # Start-Seite wird in `start_display()` zurueckgesetzt, Stopp-Seite
        # zusaetzlich in `mark_recording_started()` (die Aufzeichnung kann
        # bei Serien-/manuellem Start erst SPAETER als `start_display()`
        # tatsaechlich beginnen).
        self._start_trigger_last_condition: bool | None = None
        self._stop_trigger_last_condition: bool | None = None
        # Nullpunkt fuer das Aufnahme-Limit (siehe
        # `data/models.py::MeasurementConfig.is_recording_limit_reached`) -
        # bei getriggerten Messungen NICHT der Beginn der Hardware-
        # Erfassung (das waere der Scharf-Zeitpunkt), sondern der
        # tatsaechliche Trigger-Zeitpunkt (siehe `mark_recording_started`).
        # Bleibt bei manuellem Start 0, also unveraendertes Verhalten.
        self._recording_baseline_samples: int = 0

        self._timer = QTimer(self)
        # PreciseTimer statt des Qt-Default (CoarseTimer, an Windows'
        # ~15,6ms-Systemtick ausgerichtet, +-Abweichung moeglich) - bei
        # einem so kurzen Intervall (siehe `_UI_UPDATE_INTERVAL_MS`) macht
        # sich die grobe Standardaufloesung sonst als zusaetzliches Timing-
        # Jitter bemerkbar.
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(_UI_UPDATE_INTERVAL_MS)
        self._timer.timeout.connect(self._on_timer_tick)

        self._storage_timer = QTimer(self)
        self._storage_timer.setInterval(_STORAGE_UPDATE_INTERVAL_MS)
        self._storage_timer.timeout.connect(self._on_storage_timer_tick)

        self._build_ui()
        style_plot_container(self._plot_widget)
        connect_language_changed(self.retranslate_ui)
        connect_theme_changed(self.retheme_plots)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 0, 9, 9)

        info_row = QHBoxLayout()
        info_row.setContentsMargins(0, 0, 0, 8)
        info_row.setSpacing(10)
        self._duration_label = QLabel(t("duration_value", value="-"))
        self._sample_rate_label = QLabel(t("sample_rate_value", value="-"))

        # Scharf-Button: identisches Gegenstück zu
        # `gui/setup_view.py::_trigger_arm_button` (gleicher Stil, gleiche
        # Bedeutung) - links vom Start-Button, damit beide Buttons von
        # hier aus bedienbar sind, ohne in die Setup-Ansicht wechseln zu
        # müssen. Nur sichtbar, wenn tatsächlich ein Trigger konfiguriert
        # ist (siehe `set_trigger_arm_available`).
        self._trigger_arm_button = QPushButton()
        self._trigger_arm_button.setCheckable(True)
        self._trigger_arm_button.setIconSize(QSize(24, 24))
        self._trigger_arm_button.setStyleSheet(TRIGGER_ARM_BUTTON_STYLE)
        self._trigger_arm_button.setVisible(False)
        self._trigger_arm_button.toggled.connect(self._on_trigger_arm_button_toggled)

        # Play (gruenes Icon, nur Live-Anzeige)/Aufnahme (rotes Kreis-Icon,
        # mit Speicherung)/Stop - identisches Gegenstück zu
        # `gui/setup_view.py` (siehe dort fuer die Begruendung des
        # Drei-Button-Designs statt frueher einem Start-Button + "Nur
        # Live-Ansicht"-Haken). `ACTION_BUTTON_STYLE` setzt bewusst KEINEN
        # `background-color` im Normalzustand (anders als
        # `_trigger_arm_button`) - folgen normal der QPalette/dem
        # aktuellen Theme, nur die Play-/Aufnahme-Icon-Farbe ist fest
        # (siehe `_retheme_action_button_icons`); nur Hover/Press bekommen
        # einen dezenten Palette-basierten Effekt.
        self._play_button = QPushButton()
        self._play_button.setIconSize(QSize(24, 24))
        self._play_button.setStyleSheet(ACTION_BUTTON_STYLE)
        self._play_button.clicked.connect(lambda: self.start_requested.emit(True))

        self._record_button = QPushButton()
        self._record_button.setIconSize(QSize(24, 24))
        self._record_button.setStyleSheet(ACTION_BUTTON_STYLE)
        self._record_button.clicked.connect(lambda: self.start_requested.emit(False))

        self._stop_button = QPushButton()
        self._stop_button.setIconSize(QSize(24, 24))
        self._stop_button.setStyleSheet(ACTION_BUTTON_STYLE)
        self._stop_button.setEnabled(False)
        self._stop_button.clicked.connect(self.stop_requested.emit)

        self._retheme_action_button_icons()
        self._update_action_button_labels()
        # ERST NACH Icon/Stylesheet setzen: `_set_trigger_arm_button_text()`
        # fixiert ueber `fix_toggle_button_width()` die Buttonbreite anhand
        # von `sizeHint()`, der Icon UND Stylesheet-Padding braucht, um
        # korrekt zu messen.
        self._set_trigger_arm_button_text()

        # Play/Aufnahme/Stop (+ Scharf-Button) links, die laufenden
        # Messwerte (Dauer/Abtastrate) direkt daneben - vorher rechts vom
        # Stretch, jetzt zusammen mit den Buttons links gruppiert.
        info_row.addWidget(self._trigger_arm_button, 0, Qt.AlignmentFlag.AlignVCenter)
        info_row.addWidget(self._play_button, 0, Qt.AlignmentFlag.AlignVCenter)
        info_row.addWidget(self._record_button, 0, Qt.AlignmentFlag.AlignVCenter)
        info_row.addWidget(self._stop_button, 0, Qt.AlignmentFlag.AlignVCenter)
        info_row.addWidget(self._duration_label, 0, Qt.AlignmentFlag.AlignVCenter)
        info_row.addWidget(self._sample_rate_label, 0, Qt.AlignmentFlag.AlignVCenter)
        info_row.addStretch(1)
        layout.addLayout(info_row)

        # "Scharf, wartet auf Trigger"-Banner (siehe `enter_armed_state`) -
        # deutlich hervorgehoben, standardmaessig unsichtbar. Der bestehende
        # Stop-Button dient waehrenddessen unveraendert als Abbrechen-Funktion
        # (siehe `gui/main_window.py::_on_stop_measurement`).
        self._armed_banner = QLabel()
        self._armed_banner.setWordWrap(True)
        self._armed_banner.setStyleSheet(
            "QLabel { background-color: #fd7e14; color: #1a1a1a; padding: 6px 10px;"
            " border-radius: 4px; font-weight: 600; }"
        )
        self._armed_banner.setVisible(False)
        layout.addWidget(self._armed_banner)

        layout.addWidget(self._plot_widget, stretch=1)

        # Pufferauslastung des Storage Writers (Schreib-Rückstand ggü. DAQ-Thread).
        self._storage_group = QGroupBox(t("storage_buffer_group"))
        storage_layout = QHBoxLayout(self._storage_group)
        self._storage_progress = QProgressBar()
        self._storage_progress.setRange(0, 100)
        self._storage_progress.setTextVisible(True)
        self._storage_progress.setFormat("%p%")
        self._storage_detail_label = QLabel("-")
        storage_layout.addWidget(self._storage_progress, stretch=1)
        storage_layout.addWidget(self._storage_detail_label)
        layout.addWidget(self._storage_group)
        self._storage_group.setVisible(False)

    # ------------------------------------------------------------------ #
    # Öffentliche API (von main_window.py aufgerufen)
    # ------------------------------------------------------------------ #

    def preview_channels(self, channels: list[Channel]) -> None:
        """Baut die Plot-Anordnung schon anhand der im Setup konfigurierten
        Kanäle auf, BEVOR eine Messung gestartet wird - die Live View muss
        so nicht leer bleiben, bis tatsächlich gestartet wird.

        Wirkt nur, solange keine Messung läuft (`self._reader_id is None`);
        eine laufende Messung darf ihre eigenen, tatsächlich erfassten
        Plots nicht durch eine Vorschau der (evtl. seitdem geänderten)
        Setup-Konfiguration ersetzt bekommen.

        Baut NUR neu auf, wenn sich die Kanalkonfiguration gegenüber der
        zuletzt angezeigten tatsächlich geändert hat (`Channel` ist ein
        `@dataclass`, Listenvergleich also elementweise nach Inhalt) -
        sonst würde jeder Klick auf die Live-View-Kachel die Plots
        unnötig neu aufbauen, selbst wenn sich im Setup nichts geändert hat.
        """
        if self._reader_id is not None or channels == self._channels:
            return
        self._channels = channels
        self._rebuild_plots()

    def start_display(
        self,
        channels: list[Channel],
        sample_rate_hz: float,
        storage_writer: StorageWriter | None = None,
        trigger_config: TriggerConfig | None = None,
    ) -> None:
        """Beginnt die Live-Anzeige für eine neu gestartete Messung.

        Registriert einen eigenen, unabhängigen Ring-Buffer-Reader (siehe
        `MeasurementController.register_reader`) - die Live View darf
        Samples verlieren/überspringen, ohne den Storage Writer zu
        beeinträchtigen (siehe `core/ringbuffer.py`).

        IMMER aufgerufen, auch bei manuellem Start (nicht nur bei einem
        Schwellwert-/seriellen Start-Trigger) - der Stopp-Trigger (siehe
        `TriggerConfig.stop`) muss unabhaengig von der Art des Starts
        ueberwacht werden koennen. Loest hier BEIDE Kanal-Indizes auf
        (Start- und Stopp-Seite) und setzt beide Flankendetektoren zurueck.

        Args:
            storage_writer: Der `StorageWriter` der laufenden Messung, falls
                gespeichert wird. `None` bei "Nur Live anzeigen" - dann
                bleibt die Speicherpuffer-Anzeige ausgeblendet.
            trigger_config: Aktuelle Start-/Stopp-Trigger-Konfiguration
                (siehe `data/models.py::TriggerConfig`). `None` entspricht
                einer leeren Konfiguration (kein Trigger).
        """
        self._channels = channels
        self._sample_rate_hz = sample_rate_hz
        self._reader_id = self._controller.register_reader()

        self._trigger_config = trigger_config or TriggerConfig()
        self._start_trigger_channel_index = None
        self._stop_trigger_channel_index = None
        for index, channel in enumerate(channels):
            if channel.hardware_channel == self._trigger_config.start.threshold_channel_hardware_id:
                self._start_trigger_channel_index = index
            if channel.hardware_channel == self._trigger_config.stop.threshold_channel_hardware_id:
                self._stop_trigger_channel_index = index
        self._start_trigger_last_condition = None
        self._stop_trigger_last_condition = None

        self._rebuild_plots()
        self._ensure_display_buffer(len(channels))
        # Explizit zuruecksetzen (nicht nur in `_ensure_display_buffer`
        # implizit ueber einen Formwechsel): sonst wuerde bei gleicher
        # Kanalzahl/Abtastrate wie in der vorherigen Messung die alte
        # Schreibposition (und damit ein Rest alter Messdaten) sichtbar
        # in den neuen Sweep-Durchlauf hineinragen.
        self._buffer_write_pos = 0
        self._channel_buffer_positions = {index: 0 for index in range(len(channels))}
        self._channel_cycle_starts = {index: 0.0 for index in range(len(channels))}
        self._channel_x_range_applied = {index: None for index in range(len(channels))}
        self._timer.start()

        self.attach_storage_writer(storage_writer)

        logger.info(
            "Live View gestartet für %d Kanäle bei %.1f Hz", len(channels), sample_rate_hz
        )

    def attach_storage_writer(self, storage_writer: StorageWriter | None) -> None:
        """Setzt (oder entfernt) den StorageWriter der laufenden Messung.

        Bei manuellem Start ruft `start_display()` dies direkt mit dem
        bereits fertigen StorageWriter auf (bzw. `None` bei "Nur Live
        anzeigen"). Bei einem automatischen Trigger (siehe
        `enter_armed_state`) existiert waehrend der Scharf-Phase noch KEIN
        StorageWriter - `gui/main_window.py::_on_trigger_fired` ruft diese
        Methode dann NACHTRAEGLICH auf, sobald der Trigger tatsaechlich
        ausgeloest hat.
        """
        self._storage_writer = storage_writer
        self._storage_group.setVisible(storage_writer is not None)
        if storage_writer is not None:
            self._on_storage_timer_tick()
            self._storage_timer.start()
        else:
            self._storage_timer.stop()

    def mark_recording_started(self, baseline_samples: int) -> None:
        """Setzt den Nullpunkt fuer das Aufnahme-Limit auf den
        tatsaechlichen Aufzeichnungs-Beginn (siehe
        `_recording_baseline_samples`, `_on_timer_tick`) und ist der
        universelle Reset-Punkt fuer den Stopp-Trigger-Flankendetektor
        (siehe `_check_stop_threshold_trigger`) - eine bereits beim
        tatsaechlichen Aufzeichnungsbeginn erfuellte Stopp-Bedingung darf
        nicht sofort (faelschlich) ausloesen.

        Bei manuellem Start mit `0` aufgerufen (Nullpunkt = Erfassungsstart,
        unveraendertes Verhalten) - MUSS auch dort aufgerufen werden, da der
        Stopp-Flankendetektor sonst nie zurueckgesetzt wird. Bei einem
        Start-Trigger ruft `gui/main_window.py::_on_trigger_fired` dies mit
        der Samplezahl auf, bei der der Trigger tatsaechlich ausgeloest hat.
        """
        self._recording_baseline_samples = baseline_samples
        self._stop_trigger_last_condition = None

    def enter_armed_state(self) -> None:
        """Versetzt die Live View in den "scharf, wartet auf Trigger"-Zustand.

        Kanal-Aufloesung und Flankendetektor-Reset sind bereits durch
        `start_display()` erledigt (IMMER aufgerufen, auch bei manuellem
        Start) - hier nur noch Zustand + Banner. Hardware-Erfassung und
        Anzeige laufen zu diesem Zeitpunkt bereits (siehe
        `gui/main_window.py::_on_start_measurement`) - nur der
        StorageWriter fehlt noch. Bei einem Schwellwert-Trigger prueft
        `_on_timer_tick`/`_check_threshold_trigger` ab jetzt jeden Tick den
        konfigurierten Kanal; bei einem seriellen Trigger geschieht die
        eigentliche Ueberwachung extern (siehe
        `gui/serial_trigger.py::SerialTriggerListener`), dieser Zustand
        steuert hier nur die Banner-Anzeige.
        """
        self._armed = True
        self._update_armed_banner()

    def exit_armed_state(self) -> None:
        """Beendet den "scharf, wartet auf Trigger"-Zustand (Trigger
        ausgeloest ODER Messung waehrenddessen abgebrochen). Idempotent."""
        self._armed = False
        self._armed_banner.setVisible(False)

    def _update_armed_banner(self) -> None:
        if self._trigger_config is None:
            return
        start = self._trigger_config.start
        if start.kind == TriggerKind.SERIAL:
            text = t("armed_waiting_serial", port=start.serial_port)
        else:
            channel_name = (
                self._channels[self._start_trigger_channel_index].display_name
                if self._start_trigger_channel_index is not None
                else start.threshold_channel_hardware_id
            )
            text = t(
                "armed_waiting_threshold",
                channel=channel_name,
                threshold=start.threshold_value,
            )
        self._armed_banner.setText(text)
        self._armed_banner.setVisible(True)

    @staticmethod
    def _evaluate_threshold_condition(latest: float, condition) -> bool:
        """Wertet eine `TriggerCondition` (Schwellwert-Art) fuer einen
        einzelnen Messwert aus - gemeinsam genutzt von Start- und
        Stopp-Pruefung."""
        threshold = condition.threshold_value
        direction = condition.threshold_direction
        if direction == TriggerDirection.RISES_ABOVE:
            return latest > threshold
        if direction == TriggerDirection.FALLS_BELOW:
            return latest < threshold
        return abs(latest) > threshold  # ABS_EXCEEDS

    def _check_threshold_trigger(self, scaled: np.ndarray) -> None:
        """Prueft den konfigurierten Start-Kanal jeden Tick gegen den
        Schwellwert (siehe `enter_armed_state`) und emittiert
        `trigger_fired`, sobald die Bedingung FLANKENARTIG eintritt (also
        beim Wechsel von "nicht erfuellt" auf "erfuellt", nicht bei jedem
        Tick waehrend sie weiter erfuellt bleibt). `_start_trigger_last_condition`
        startet bei jedem Scharfschalten bewusst bei `None`, damit ein
        Kanal, der beim Scharfschalten bereits jenseits der Schwelle liegt,
        NICHT sofort ausloest - der Nutzer muss eine tatsaechliche
        Ueberschreitung sehen, wie bei einem Oszilloskop-Trigger.

        Bewusste Vereinfachung: geprueft wird nur der letzte Sample-Wert
        des jeweiligen Ticks (~15ms-Granularitaet), nicht der gesamte
        Datenblock - bei der geforderten "ca. 5s"-Vorlauftoleranz ist das
        unerheblich, gleiches Praezisionsniveau wie das bestehende
        Aufnahme-Limit (siehe `_on_timer_tick`).
        """
        if (
            not self._armed
            or self._trigger_config is None
            or self._trigger_config.start.kind != TriggerKind.THRESHOLD
            or self._start_trigger_channel_index is None
        ):
            return
        values = scaled[self._start_trigger_channel_index]
        if values.size == 0:
            return
        latest = float(values[-1])
        condition = self._evaluate_threshold_condition(latest, self._trigger_config.start)

        fired = self._start_trigger_last_condition is False and condition is True
        self._start_trigger_last_condition = condition
        if fired:
            self._armed = False
            self._armed_banner.setVisible(False)
            self.trigger_fired.emit()

    def _check_stop_threshold_trigger(self, scaled: np.ndarray) -> None:
        """Prueft den konfigurierten Stopp-Kanal jeden Tick gegen den
        Schwellwert, solange tatsaechlich aufgezeichnet wird (`not
        self._armed`) - gleiche Flankenlogik wie `_check_threshold_trigger`,
        loest aber ueber `stop_requested` aus (denselben Pfad wie
        Aufnahme-Limit und manueller Stopp-Button), statt ueber
        `trigger_fired`. `_stop_trigger_last_condition` wird von
        `mark_recording_started()` zurueckgesetzt - dem Zeitpunkt, ab dem
        die Aufzeichnung tatsaechlich beginnt (nicht notwendigerweise
        `start_display()`, z. B. bei einem Start-Trigger).
        """
        if (
            self._armed
            or self._trigger_config is None
            or self._trigger_config.stop.kind != TriggerKind.THRESHOLD
            or self._stop_trigger_channel_index is None
        ):
            return
        values = scaled[self._stop_trigger_channel_index]
        if values.size == 0:
            return
        latest = float(values[-1])
        condition = self._evaluate_threshold_condition(latest, self._trigger_config.stop)

        fired = self._stop_trigger_last_condition is False and condition is True
        self._stop_trigger_last_condition = condition
        if fired:
            self.stop_requested.emit()

    def stop_display(self) -> None:
        """Beendet die Live-Anzeige (nach Messungsende)."""
        self._timer.stop()
        self._storage_timer.stop()
        self.exit_armed_state()
        if self._reader_id is not None:
            self._controller.unregister_reader(self._reader_id)
            self._reader_id = None
        logger.info("Live View gestoppt")

    def retranslate_ui(self) -> None:
        """Aktualisiert alle statischen Texte nach einem Sprachwechsel."""
        self._update_action_button_labels()
        self._set_trigger_arm_button_text()
        self._storage_group.setTitle(t("storage_buffer_group"))

        for plot_item in self._plot_items:
            # `units=` NICHT genutzt (siehe `ChannelPopoutWindow.__init__`)
            # - Zeiteinheit ueberall einheitlich in eckigen Klammern.
            plot_item.setLabel("bottom", f"{t('axis_time')} [s]")

        # Laufende Dauer/Abtastrate korrigieren sich beim nächsten Timer-
        # Tick von selbst - nur der Leerlauf-Platzhalter würde sonst
        # dauerhaft in der alten Sprache hängen bleiben.
        if self._reader_id is None:
            self._duration_label.setText(t("duration_value", value="-"))
            self._sample_rate_label.setText(t("sample_rate_value", value="-"))

    def retheme_plots(self) -> None:
        """Färbt Plot-Hintergrund/-Achsen/-Kurven nach einem Theme-Wechsel um.

        PyQtGraph-Widgets folgen der `QApplication`-Palette nicht
        automatisch (siehe `gui/theme.py`) - bereits vorhandene Plots
        müssen daher explizit nachgefärbt werden.
        """
        style_plot_container(self._plot_widget)
        self._retheme_action_button_icons()
        for pos, plot_item in enumerate(self._plot_items):
            style_plot_item(plot_item)
            channel = self._channels[self._curve_channel_indices[pos]]
            plot_item.getViewBox().setBackgroundColor(_channel_background_color(channel))
            # Boxen/Labels existieren nur, wenn `channel.plot_show_value`
            # gesetzt ist (siehe `_rebuild_plots`) - sonst gibt der Plot
            # deren Spaltenplatz zurueck, statt nur unsichtbar leeren Raum
            # zu belegen. Bewusst IMMER Fenster-Hintergrund, NICHT die
            # individuelle Kanalfarbe (siehe `_rebuild_plots` fuer die
            # Begruendung) - die eigene Farbe gilt nur fuer die Plotflaeche
            # selbst.
            if self._value_boxes[pos] is not None:
                self._value_boxes[pos].setBackgroundColor(plot_container_background_color())
            if self._value_unit_boxes[pos] is not None:
                self._value_unit_boxes[pos].setBackgroundColor(plot_container_background_color())
            if self._value_labels[pos] is not None:
                self._value_labels[pos].setColor(plot_foreground_color())
            if self._value_unit_labels[pos] is not None:
                self._value_unit_labels[pos].setColor(plot_foreground_color())
        # Kurvenfarbe/Hintergrund NICHT pauschal auf den Theme-Default
        # zurücksetzen - individuell konfigurierte Kanalfarben (siehe
        # `open_channel_display_dialog`) sollen einen Theme-Wechsel
        # überstehen; `_apply_channel_appearance()` wendet für Kanäle OHNE
        # eigene Farbe ohnehin den (jetzt neuen) Theme-Default an.
        self._apply_channel_appearance()

    def _retheme_action_button_icons(self) -> None:
        # Play/Aufnahme haben feste, theme-unabhaengige Symbolfarben (siehe
        # `gui/theme.py::PLAY_ICON_COLOR`/`RECORD_ICON_COLOR`). Stop UND
        # der Scharf-Button haben KEINEN fest codierten Hintergrund mehr
        # (siehe `ACTION_BUTTON_STYLE`/`TRIGGER_ARM_BUTTON_STYLE`) und
        # bleiben daher bei der normalen theme-abhaengigen
        # `nav_icon_color()` (kein `color=` uebergeben).
        self._play_button.setIcon(QIcon(draw_play_icon(24, y_offset=0.6, color=PLAY_ICON_COLOR)))
        self._record_button.setIcon(
            QIcon(draw_record_icon(24, y_offset=0.6, color=RECORD_ICON_COLOR))
        )
        self._stop_button.setIcon(QIcon(draw_stop_icon(24, y_offset=0.6)))
        self._trigger_arm_button.setIcon(QIcon(draw_trigger_icon(24, y_offset=0.6)))
        # `ACTION_BUTTON_STYLE`/`TRIGGER_ARM_BUTTON_STYLE` referenzieren
        # `palette(...)` - ohne manuelles unpolish()/polish() bleiben
        # Rahmen/Hintergrund nach einem Live-Theme-Wechsel optisch im
        # alten Theme haengen (gleicher Befund wie bei den
        # Navigationskacheln, siehe
        # `gui/main_window.py::_retheme_nav_icons`).
        for button in (
            self._play_button,
            self._record_button,
            self._stop_button,
            self._trigger_arm_button,
        ):
            repolish(button)

    def _update_action_button_labels(self) -> None:
        # Kurzer Button-Text (siehe `play_button_label`/`record_button_label`/
        # `stop_button_label`) UND ausfuehrlicherer Tooltip (bestehende
        # `live_only`/`start_measurement`/`stop_measurement`-Keys).
        self._play_button.setText(f"  {t('play_button_label')}")
        self._play_button.setToolTip(t("live_only"))
        self._record_button.setText(f"  {t('record_button_label')}")
        self._record_button.setToolTip(t("start_measurement"))
        self._stop_button.setText(f"  {t('stop_button_label')}")
        self._stop_button.setToolTip(t("stop_measurement"))

    def _set_trigger_arm_button_text(self) -> None:
        key = "trigger_disarm_button" if self._trigger_arm_button.isChecked() else "trigger_arm_button"
        self._trigger_arm_button.setText(f"  {t(key)}")
        fix_toggle_button_width(
            self._trigger_arm_button,
            f"  {t('trigger_arm_button')}",
            f"  {t('trigger_disarm_button')}",
        )

    def _on_trigger_arm_button_toggled(self, checked: bool) -> None:
        self._set_trigger_arm_button_text()
        self.trigger_arm_toggled.emit(checked)

    def set_trigger_arm_available(self, available: bool) -> None:
        """Siehe `gui/setup_view.py::SetupView.set_trigger_arm_available`
        - identisches Gegenstück hier in der Live-Ansicht."""
        self._trigger_arm_button.setVisible(available)
        if not available and self._trigger_arm_button.isChecked():
            self.set_trigger_armed(False)

    def set_trigger_armed(self, armed: bool) -> None:
        """Siehe `gui/setup_view.py::SetupView.set_trigger_armed` -
        identisches Gegenstück hier in der Live-Ansicht."""
        self._trigger_arm_button.blockSignals(True)
        self._trigger_arm_button.setChecked(armed)
        self._trigger_arm_button.blockSignals(False)
        self._set_trigger_arm_button_text()

    def set_start_enabled(self, enabled: bool) -> None:
        """Siehe `gui/setup_view.py::SetupView.set_start_enabled` -
        identisches Gegenstück hier: Stop folgt IMMER dem umgekehrten
        Zustand."""
        self._play_button.setEnabled(enabled)
        self._record_button.setEnabled(enabled)
        self._stop_button.setEnabled(not enabled)
        # Siehe `gui/setup_view.py::SetupView.set_start_enabled` - gleiche
        # Ausnahme: waehrend eines eigenen aktiven Zyklus bleibt der
        # Scharf-Button immer klickbar (damit "entschärfen" jederzeit geht).
        if not self._trigger_arm_button.isChecked():
            self._trigger_arm_button.setEnabled(enabled)

    def open_channel_display_dialog(
        self, channels: list[Channel] | None = None
    ) -> dict[tuple[str, str], dict] | None:
        """Öffnet den Dialog für Kurvenfarbe/Hintergrund/Y-Bereich/
        Autoskalierung pro Kanal.

        Aufgerufen vom Menüpunkt Optionen -> "Kanal-Darstellung
        festlegen..." (siehe `gui/main_window.py::_build_menu`).

        Args:
            channels: Kanäle, die im Dialog angeboten werden (ihre
                aktuellen `plot_*`-Felder sind die Vorbelegung, siehe
                `data/models.py::Channel`). `None` (Default) verwendet die
                aktuell live angezeigten Kanäle (`self._channels`, nur
                während einer laufenden Messung gefüllt).
                `gui/main_window.py` übergibt stattdessen die Kanäle aus
                der Setup-Konfiguration, damit sich die Darstellung schon
                VOR dem Messstart einstellen lässt.

        Returns:
            Die im Dialog gesetzten Werte pro Kanal (siehe
            `ChannelDisplayDialog.results()`), oder `None` bei Abbruch/
            fehlenden Kanälen. `gui/main_window.py` reicht das Ergebnis an
            `SetupView.apply_channel_display_settings()` weiter, damit die
            Werte beim Speichern der Konfiguration erhalten bleiben - die
            Live View selbst kennt nur ihre eigenen `self._channels`
            (siehe `_apply_display_settings_to_live_channels`).
        """
        channels = channels if channels is not None else self._channels
        if not channels:
            QMessageBox.information(
                self, t("channel_display_dialog_title"), t("channel_display_no_channels")
            )
            return None
        dialog = ChannelDisplayDialog(
            channels, curve_color(), plot_background_color(), plot_foreground_color(), self
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        settings = dialog.results()
        self._apply_display_settings_to_live_channels(settings)
        return settings

    def _apply_display_settings_to_live_channels(
        self, settings: dict[tuple[str, str], dict]
    ) -> None:
        """Überträgt vom Dialog gesetzte Werte auf die AKTUELL live
        angezeigten Kanäle (`self._channels`).

        Relevant, falls der Dialog mit einer anderen Kanalliste (z. B. aus
        dem Setup, siehe `open_channel_display_dialog`) geöffnet wurde,
        während gerade eine Messung läuft: die laufende Anzeige soll sich
        sofort aktualisieren, nicht erst beim nächsten Messstart.
        """
        if not self._channels:
            return
        changed = False
        visibility_changed = False
        time_window_changed = False
        integer_digits_changed = False
        show_value_changed = False
        for channel in self._channels:
            values = settings.get(_channel_display_key(channel))
            if values is None:
                continue
            channel.plot_color = values.get("plot_color")
            channel.plot_background = values.get("plot_background")
            channel.plot_grid_color = values.get("plot_grid_color")
            channel.plot_y_min = values.get("plot_y_min")
            channel.plot_y_max = values.get("plot_y_max")
            channel.plot_autoscale = values.get("plot_autoscale", True)
            new_time_window = max(
                0.1, float(values.get("plot_time_window_seconds", 5.0))
            )
            if new_time_window != channel.plot_time_window_seconds:
                time_window_changed = True
            channel.plot_time_window_seconds = new_time_window
            # Braucht einen Rebuild (siehe unten), keine reine
            # `_apply_channel_appearance()`-Aktualisierung: die Zahlenspalte
            # im Hauptraster hat im `GraphicsLayoutWidget` eine FESTE Breite
            # (`setColumnFixedWidth`, siehe `_rebuild_plots()`) - nur
            # `.setVisible()` auf der Box selbst laesst den Plot die Spalte
            # NICHT zurueckgewinnen, die Spaltenbreite muss dafuer neu
            # berechnet werden.
            new_show_value = values.get("plot_show_value", True)
            if new_show_value != channel.plot_show_value:
                show_value_changed = True
            channel.plot_show_value = new_show_value
            new_integer_digits = max(
                1, int(values.get("plot_value_integer_digits", 3))
            )
            new_decimal_digits = max(
                0, int(values.get("plot_value_decimal_digits", 3))
            )
            if (
                new_integer_digits != channel.plot_value_integer_digits
                or new_decimal_digits != channel.plot_value_decimal_digits
            ):
                integer_digits_changed = True
            channel.plot_value_integer_digits = new_integer_digits
            channel.plot_value_decimal_digits = new_decimal_digits
            new_visible = values.get("plot_visible", True)
            new_popout = values.get("plot_popout", False)
            if new_visible != channel.plot_visible or new_popout != channel.plot_popout:
                visibility_changed = True
            channel.plot_visible = new_visible
            channel.plot_popout = new_popout
            changed = True
        if visibility_changed or time_window_changed or integer_digits_changed or show_value_changed:
            if time_window_changed:
                # Puffer ist auf das breiteste Zeitfenster ueber alle
                # Kanaele dimensioniert (siehe `_ensure_display_buffer`) -
                # bei einer waehrend der Messung vergroesserten Zeitspanne
                # muss er neu allokiert werden, sonst wuerde `cap` in
                # `_write_to_display_buffer` weiterhin auf die alte,
                # kleinere Kapazitaet gedeckelt.
                self._ensure_display_buffer(len(self._channels))
            # Welche Kanäle überhaupt einen Subplot bekommen, hat sich
            # geändert - Farb-/Bereichs-Anwendung fürs Hauptraster ist Teil
            # von `_rebuild_plots()` und muss daher nicht separat erfolgen.
            # Offene eigene Fenster (siehe `ChannelPopoutWindow`) werden von
            # `_rebuild_plots()` NUR bei einem tatsächlichen
            # Sichtbarkeits-/Popout-Wechsel angefasst - `integer_digits_changed`
            # allein (Fenster bleibt offen) braeuchte sonst KEINE
            # Aktualisierung der Feldbreite, daher hier explizit nachziehen.
            self._rebuild_plots()
            self._apply_channel_appearance()
        elif changed:
            self._apply_channel_appearance()
            self._apply_y_range_mode()

    def _find_channel_by_key(self, key: tuple[str, str]) -> Channel | None:
        """Findet einen Kanal über `_channel_display_key()` - für
        Popout-bezogene Nachschlagevorgänge (siehe dort)."""
        return next((c for c in self._channels if _channel_display_key(c) == key), None)

    def _open_popout_window(self, channel: Channel) -> None:
        """Öffnet ein eigenständiges Fenster mit dem Live-Plot eines
        einzelnen Kanals (siehe `ChannelPopoutWindow`), oder aktiviert ein
        dafür bereits offenes Fenster, statt ein zweites zu öffnen."""
        key = _channel_display_key(channel)
        existing = self._popout_windows.get(key)
        if existing is not None:
            existing.raise_()
            existing.activateWindow()
            return

        window = ChannelPopoutWindow(channel, self)
        channel_index = self._channels.index(channel)
        x_min = self._channel_cycle_starts.get(channel_index, 0.0)
        window.plot_item.setXRange(
            x_min,
            x_min + channel.plot_time_window_seconds,
            padding=0,
        )
        self._apply_channel_curve_style(window.plot_item, window.curve, channel)
        self._apply_channel_y_range(window.plot_item, channel, None, self._popout_y_auto_active)
        # WICHTIG: Die Closure haelt `self` (LiveView) NUR als `weakref`,
        # nicht direkt - sonst entsteht ein echter Referenzzyklus
        # (LiveView -> self._popout_windows[hw] -> window -> Qt/sip-
        # Verbindungsregister -> diese Closure -> self). Ein solcher
        # Zyklus wird nur vom zyklischen GC aufgeloest, nicht durch
        # normales Refcounting - und weil dieser dabei die beteiligten
        # Objekte ueber `tp_clear` "leerraeumt", kann `destroyed` mitten
        # in diesem Aufraeumvorgang feuern und `self` als bereits
        # geleerte Closure-Zelle vorfinden
        # (`NameError: cannot access free variable 'self'`) - reproduzierbar
        # ueber `profile_live_tick.py` (mehrere LiveView-Instanzen kurz
        # hintereinander anlegen/verwerfen). Mit `weakref` entsteht gar
        # kein Zyklus, Refcounting allein reicht zum Aufraeumen.
        view_ref = weakref.ref(self)

        def _on_window_destroyed(_obj=None, key=key, view_ref=view_ref) -> None:
            view = view_ref()
            if view is not None:
                view._on_popout_window_closed(key)

        window.destroyed.connect(_on_window_destroyed)

        # Zuletzt bekannte Position/Groesse wiederverwenden (siehe
        # `Channel.plot_popout_x` usw.), sofern vorhanden UND noch auf
        # einem aktuell angeschlossenen Bildschirm liegt (z. B. NICHT auf
        # einem inzwischen abgesteckten zweiten Monitor) - sonst wie
        # bisher kaskadiert relativ zum Hauptfenster platzieren.
        has_saved_geometry = (
            channel.plot_popout_x is not None
            and channel.plot_popout_y is not None
            and channel.plot_popout_width is not None
            and channel.plot_popout_height is not None
        )
        if has_saved_geometry and is_position_on_screen(
            channel.plot_popout_x + channel.plot_popout_width // 2,
            channel.plot_popout_y + channel.plot_popout_height // 2,
        ):
            window.setGeometry(
                channel.plot_popout_x,
                channel.plot_popout_y,
                channel.plot_popout_width,
                channel.plot_popout_height,
            )
        else:
            # Kaskadierte Position statt Qt's Default-Platzierung: werden
            # mehrere Kanäle auf einmal per Dialog auf "Eigenes Fenster"
            # gesetzt (ein OK-Klick löst mehrere `_open_popout_window()`-
            # Aufrufe direkt hintereinander aus, siehe Aufrufer), platziert
            # Qt ohne das hier neue Fenster sonst exakt übereinander -
            # sichtbar wird dann nur das zuletzt geöffnete, die anderen
            # liegen unsichtbar dahinter und "erscheinen" erst beim
            # Schließen des jeweils obersten. Versatz relativ zur Position
            # des Hauptfensters, damit die Fenster in dessen Naehe
            # auftauchen (nicht z. B. auf einem anderen Bildschirm).
            main_window = self.window()
            base = main_window.pos() if main_window is not None else QPoint(80, 80)
            cascade_offset = 32 * len(self._popout_windows)
            window.move(base.x() + 60 + cascade_offset, base.y() + 60 + cascade_offset)
        self._popout_windows[key] = window
        window.show()

    def get_open_popout_geometries(self) -> dict[tuple[str, str], tuple[int, int, int, int]]:
        """Liefert Position/Groesse (x, y, width, height) aller aktuell
        offenen eigenen Fenster - fuer `gui/main_window.py`, um sie beim
        Schliessen/expliziten Speichern der App in die Setup-Kanaltabelle
        zu uebernehmen (siehe `Channel.plot_popout_x` usw.). Eigentlich
        redundant zu den kontinuierlich synchronisierten `Channel`-Feldern
        (siehe `ChannelPopoutWindow.moveEvent`/`resizeEvent`), liest aber
        bewusst direkt vom Fenster statt vom Kanal-Objekt - unabhaengig
        davon, ob dieser Kanal gerade ueberhaupt Teil der live angezeigten
        `self._channels` ist."""
        return {
            key: (window.x(), window.y(), window.width(), window.height())
            for key, window in self._popout_windows.items()
        }

    def _on_popout_window_closed(self, key: tuple[str, str]) -> None:
        """Räumt die Nachverfolgung eines geschlossenen eigenen Fensters
        auf (`self._popout_windows`). Wurde das Fenster vom Nutzer direkt
        geschlossen (z. B. über das X, statt über die Checkbox im
        Dialog), soll der Kanal nicht spurlos verschwinden, sondern
        wieder im Hauptraster erscheinen - daher `plot_popout` hier
        ebenfalls zurücksetzen und neu aufbauen.

        `destroyed` ist eine QUEUED Verbindung und kann daher auch noch
        feuern, NACHDEM die Live View selbst (z. B. beim Beenden der
        Anwendung mit offenem eigenem Fenster) bereits zerstört wird -
        `sip.isdeleted` verhindert in diesem Fall einen Zugriff auf ein
        bereits abgebautes `self._plot_widget` in `_rebuild_plots()`.
        """
        self._popout_windows.pop(key, None)
        self._popout_y_auto_active.pop(key, None)
        if sip.isdeleted(self):
            return
        channel = self._find_channel_by_key(key)
        if channel is not None and channel.plot_popout:
            channel.plot_popout = False
            self._rebuild_plots()

    # ------------------------------------------------------------------ #
    # Interna
    # ------------------------------------------------------------------ #

    def _make_value_box(
        self,
        row: int,
        col: int,
        text: str,
        point_size: int,
        bold: bool,
        box_width_px: float,
        align: str = "center",
        margin_px: float = 0.0,
    ) -> tuple[pg.ViewBox, pg.TextItem]:
        """Erzeugt eine achsen-/interaktionslose `ViewBox` mit `TextItem`
        fuer eine Zelle der Messwertanzeige (siehe `_rebuild_plots`) -
        bewusst KEIN `LabelItem` (siehe Kommentar dort).

        `align="right"`/`"left"` mit `margin_px`: positioniert den Text
        nicht zentriert, sondern buendig an einer Kante mit `margin_px`
        Abstand dazu (in Pixel, umgerechnet auf die `ViewBox`-eigenen
        Koordinaten ueber `box_width_px`) - genutzt fuer Zahl (rechts,
        `margin_px=0`) und Einheit (links, `margin_px`=Leerzeichenbreite),
        damit der Zwischenraum zwischen beiden IMMER exakt einer
        Leerzeichenbreite entspricht (siehe `_rebuild_plots`), statt vom
        (unterschiedlich breiten) Text selbst abzuhaengen.
        """
        box = self._plot_widget.addViewBox(row=row, col=col, lockAspect=False)
        box.setMouseEnabled(x=False, y=False)
        box.setMenuEnabled(False)
        box.setRange(xRange=(-1, 1), yRange=(-1, 1), padding=0)
        margin_units = (2.0 * margin_px / box_width_px) if box_width_px else 0.0
        if align == "right":
            anchor = (1.0, 0.5)
            x = 1.0 - margin_units
        elif align == "left":
            anchor = (0.0, 0.5)
            x = -1.0 + margin_units
        else:
            anchor = (0.5, 0.5)
            x = 0.0
        text_item = pg.TextItem(text, color=plot_foreground_color(), anchor=anchor)
        font = QFont()
        font.setPointSize(point_size)
        font.setBold(bold)
        text_item.setFont(font)
        box.addItem(text_item)
        text_item.setPos(x, 0.0)
        return box, text_item

    def _rebuild_plots(self) -> None:
        """Erzeugt für jeden im Hauptraster sichtbaren Kanal einen eigenen
        Subplot (mit unabhängiger X-Achse, siehe `Channel.plot_time_window_seconds`).

        Ein Kanal erscheint hier NICHT, wenn er entweder komplett
        deaktiviert ist (`Channel.plot_visible=False`) ODER stattdessen in
        einem eigenen Fenster angezeigt wird (`Channel.plot_popout=True`,
        siehe `ChannelPopoutWindow`/`_open_popout_window`) - so landet
        jeder sichtbare Kanal an GENAU einer Stelle, nie doppelt.

        `self._curve_channel_indices[i]` hält fest, auf welchen Index in
        `self._channels` sich `self._curves[i]`/`self._plot_items[i]`
        bezieht - ausgeblendete/ausgelagerte Kanäle werden übersprungen,
        Kurven-Position und Kanal-Index in `self._channels` sind daher
        NICHT mehr zwangsläufig identisch (siehe `_on_timer_tick`).
        """
        self._plot_widget.clear()
        self._plot_items = []
        self._curves = []
        self._curve_channel_indices = []
        self._value_boxes = []
        self._value_labels = []
        self._value_unit_boxes = []
        self._value_unit_labels = []
        self._channel_buffer_positions = {index: 0 for index in range(len(self._channels))}
        self._channel_cycle_starts = {index: 0.0 for index in range(len(self._channels))}
        self._channel_x_range_applied = {index: None for index in range(len(self._channels))}
        self._channel_y_auto_active = {}

        # Breite der Zahlenspalte richtet sich nach dem Kanal mit den
        # MEISTEN konfigurierten Vorkommastellen (siehe
        # `Channel.plot_value_integer_digits`/`_number_field_width_px`), da
        # die Spaltenbreite im `GraphicsLayoutWidget` fuer alle Zeilen
        # gemeinsam gilt. Muss VOR der Schleife feststehen, da sie fuer die
        # rechtsbuendige Positionierung jeder einzelnen Zahl gebraucht wird
        # (siehe `_make_value_box`).
        number_font = QFont()
        number_font.setPointSize(_VALUE_NUMBER_POINT_SIZE)
        number_font.setBold(True)
        number_field_width = _number_field_width_px(
            number_font,
            max((c.plot_value_integer_digits for c in self._channels), default=3),
            max((c.plot_value_decimal_digits for c in self._channels), default=3),
        )
        value_unit_gap = _space_width_px(number_font)

        row = 0
        for index, channel in enumerate(self._channels):
            if not channel.plot_visible or channel.plot_popout:
                continue
            background = _channel_background_color(channel)

            # Grosse Messwertanzeige LINKS neben dem Subplot (siehe
            # `Channel.plot_show_value`/`_on_timer_tick`) - Zahl (Spalte 0,
            # rechtsbuendig) und Einheit (Spalte 1, linksbuendig mit
            # `value_unit_gap` Abstand) in ZWEI getrennten, fest breiten
            # `ViewBox`en (siehe `setColumnFixedWidth` unten UND
            # `_make_value_box`) - der Zwischenraum zwischen beiden
            # entspricht so IMMER exakt einer Leerzeichenbreite, unabhaengig
            # vom (fest formatierten, siehe `_format_channel_value`)
            # Textinhalt. BEWUSST `ViewBox`+`TextItem` statt eines simplen
            # `LabelItem`: `LabelItem.setText()` setzt intern seine eigene
            # `minimumWidth` auf die Breite des GERADE gerenderten Texts
            # (`updateMin()`) - das hat die feste Spaltenbreite bei jedem
            # neuen Zahlenwert wieder aufgebrochen und den Plot sichtbar
            # mitwandern lassen. Eine `ViewBox` hat dagegen keine
            # inhaltsabhaengige Mindestbreite.
            #
            # Hintergrundfarbe der Messwertanzeige-Boxen ist IMMER die
            # Fenster-Hintergrundfarbe (`plot_container_background_color()`),
            # NICHT die individuelle Kanalfarbe (`background` oben, nur fuer
            # die Plot-ViewBox selbst) - die eigene Farbe soll ausschliesslich
            # innerhalb der eigentlichen Plotflaeche gelten, alles
            # drumherum (inkl. dieser Anzeige) folgt dem Fenster-Hintergrund.
            #
            # Boxen werden NUR angelegt, wenn `plot_show_value` gesetzt ist
            # (sonst `None`) - der Plot bekommt in diesem Fall stattdessen
            # `colspan=3` und beansprucht die frei werdende Spaltenbreite
            # selbst. Ein reines `.setVisible(False)` auf einer trotzdem
            # angelegten Box wuerde deren FESTE Grid-Spaltenbreite
            # (`setColumnFixedWidth` unten) nicht zurueckgeben - der Plot
            # bliebe auf Spalte 2 eingeengt, mit sichtbarem Leerraum links
            # davon. Als Folge davon braucht ein Wechsel von
            # `plot_show_value` jetzt einen vollen `_rebuild_plots()` statt
            # nur `_apply_channel_appearance()` (siehe
            # `_apply_display_settings_to_live_channels`) - die Einheit
            # wird hier EINMALIG gesetzt und danach nie mehr pro Tick
            # aktualisiert.
            if channel.plot_show_value:
                value_box, value_text = self._make_value_box(
                    row,
                    0,
                    "--",
                    point_size=_VALUE_NUMBER_POINT_SIZE,
                    bold=True,
                    box_width_px=number_field_width,
                    align="right",
                )
                value_box.setBackgroundColor(plot_container_background_color())

                unit_box, unit_text = self._make_value_box(
                    row,
                    1,
                    channel.unit,
                    point_size=_VALUE_UNIT_POINT_SIZE,
                    bold=False,
                    box_width_px=_VALUE_UNIT_WIDTH,
                    align="left",
                    margin_px=value_unit_gap,
                )
                unit_box.setBackgroundColor(plot_container_background_color())
                plot_col, plot_colspan = 2, 1
            else:
                value_box = value_text = unit_box = unit_text = None
                plot_col, plot_colspan = 0, 3

            unit_suffix = f" [{channel.unit}]" if channel.unit else ""
            plot_item = self._plot_widget.addPlot(
                row=row,
                col=plot_col,
                colspan=plot_colspan,
                title=f"{channel.display_name}{unit_suffix}",
            )
            plot_item.showGrid(x=True, y=True, alpha=0.3)
            # `units=` NICHT genutzt (siehe `ChannelPopoutWindow.__init__`)
            # - Zeiteinheit ueberall einheitlich in eckigen Klammern.
            plot_item.setLabel("bottom", f"{t('axis_time')} [s]", **_axis_label_style())
            plot_item.setLabel("left", _channel_axis_label(channel), **_axis_label_style())
            style_plot_item(plot_item)
            # KEIN `setXLink` zwischen den Subplots: jeder Kanal hat sein
            # eigenes, unabhaengig konfigurierbares Zeitfenster
            # (`Channel.plot_time_window_seconds`) - eine verlinkte X-Achse
            # wuerde den zuletzt gesetzten Bereich auf alle anderen Subplots
            # erzwingen und die Einstellung pro Kanal wirkungslos machen.
            curve = plot_item.plot(pen=pg.mkPen(color=curve_color(), width=1.5))
            curve.setDownsampling(auto=True, method="mean")
            curve.setClipToView(True)
            plot_item.getViewBox().setBackgroundColor(background)

            # Sweep-Anzeige (Oszilloskop-Art, siehe Klassendoc weiter oben):
            # das Zeitfenster steht fest bei [0, Fensterlaenge] - es scrollt
            # NICHT mit, die Kurve selbst laeuft innerhalb dieses festen
            # Fensters von links nach rechts durch.
            plot_item.enableAutoRange(x=False)
            plot_item.setXRange(0.0, channel.plot_time_window_seconds, padding=0)

            self._plot_items.append(plot_item)
            self._curves.append(curve)
            self._curve_channel_indices.append(index)
            self._value_boxes.append(value_box)
            self._value_labels.append(value_text)
            self._value_unit_boxes.append(unit_box)
            self._value_unit_labels.append(unit_text)
            row += 1

        if self._plot_items:
            # Zahl-/Einheitsspalte fest breit - siehe Kommentar oben
            # (`number_field_width` bereits vor der Schleife berechnet, da
            # dort fuer die rechtsbuendige Positionierung gebraucht). Der
            # Plot bekommt den gesamten restlichen Platz.
            self._plot_widget.ci.layout.setColumnFixedWidth(0, number_field_width)
            self._plot_widget.ci.layout.setColumnFixedWidth(1, _VALUE_UNIT_WIDTH)
            self._plot_widget.ci.layout.setColumnStretchFactor(2, 1)

        # Eigene Fenster (siehe `ChannelPopoutWindow`) für Kanäle
        # schliessen, die es nach dieser Kanalkonfiguration nicht mehr
        # gibt, die inzwischen komplett deaktiviert wurden
        # (`plot_visible=False`) ODER deren "Eigenes Fenster"-Haken im
        # Dialog wieder entfernt wurde (`plot_popout=False`) - Letzteres
        # ist seit "erst mit OK aktiv werden" der EINZIGE Weg, ein Fenster
        # wieder zu schliessen, wenn der Nutzer es nicht direkt selbst
        # zumacht (siehe `_on_popout_window_closed`). Verhindert außerdem
        # verwaiste Fenster mit eingefrorenen Altdaten. Wird über
        # `window.destroyed` automatisch aus `self._popout_windows`
        # entfernt (siehe `_on_popout_window_closed`).
        for key in list(self._popout_windows.keys()):
            channel = self._find_channel_by_key(key)
            if channel is None or not channel.plot_visible or not channel.plot_popout:
                self._popout_windows[key].close()

        # Kanäle, die als "eigenes Fenster" konfiguriert sind
        # (`plot_popout=True`, z. B. aus einer geladenen Konfiguration
        # oder nach einem Messstart), aber noch kein offenes Fenster
        # haben, automatisch öffnen - sonst würde ein solcher Kanal sonst
        # spurlos verschwinden (weder Hauptraster noch Fenster sichtbar).
        for channel in self._channels:
            if (
                channel.plot_visible
                and channel.plot_popout
                and _channel_display_key(channel) not in self._popout_windows
            ):
                self._open_popout_window(channel)

        self._apply_channel_appearance()
        self._apply_y_range_mode()

    @staticmethod
    def _apply_channel_curve_style(plot_item, curve, channel: Channel) -> None:
        """Wendet Kurvenfarbe, Hintergrundfarbe und Gitterlinienfarbe EINES
        Kanals auf sein Plot/Kurven-Paar an - Theme-Default, falls keine
        eigene Farbe konfiguriert ist. Gemeinsam genutzt von
        Hauptraster-Subplots (`_apply_channel_appearance`) und eigenen
        Fenstern (`_open_popout_window`)."""
        color = channel.plot_color or curve_color()
        curve.setPen(pg.mkPen(color=color, width=1.5))
        plot_item.getViewBox().setBackgroundColor(_channel_background_color(channel))
        grid_color = _channel_grid_color(channel)
        for axis_name in ("left", "bottom", "right", "top"):
            axis = plot_item.getAxis(axis_name)
            if axis is not None:
                axis.setTickPen(grid_color)

    def _apply_channel_appearance(self) -> None:
        """Wendet Kurvenfarbe und Hintergrundfarbe pro Kanal an (siehe
        `open_channel_display_dialog`), für Hauptraster-Subplots UND offene
        eigene Fenster.

        `plot_show_value` selbst wird HIER NICHT mehr behandelt - ein
        Wechsel braucht einen vollen `_rebuild_plots()` (siehe
        `_apply_display_settings_to_live_channels`/`_rebuild_plots`), die
        Boxen existieren für Hauptraster-Kanäle ohne Messwertanzeige gar
        nicht mehr (`None`, siehe dort).
        """
        for pos, (plot_item, curve) in enumerate(zip(self._plot_items, self._curves)):
            channel = self._channels[self._curve_channel_indices[pos]]
            self._apply_channel_curve_style(plot_item, curve, channel)
            # Fenster-Hintergrundfarbe, nicht die individuelle Kanalfarbe -
            # siehe `_rebuild_plots` fuer die Begruendung.
            if self._value_boxes[pos] is not None:
                self._value_boxes[pos].setBackgroundColor(plot_container_background_color())
            if self._value_unit_boxes[pos] is not None:
                self._value_unit_boxes[pos].setBackgroundColor(plot_container_background_color())
        for key, window in self._popout_windows.items():
            channel = self._find_channel_by_key(key)
            if channel is not None:
                self._apply_channel_curve_style(window.plot_item, window.curve, channel)
                window._apply_number_width()
                window._style_value_labels()
                window._value_container.setVisible(channel.plot_show_value)

    def _apply_y_range_mode(self) -> None:
        """Wendet den Y-Bereich (fest, Autoscale oder Hybrid) auf alle
        Subplots UND offenen eigenen Fenster an - ohne aktuelle Messwerte
        (siehe `_apply_channel_y_range`), z. B. direkt nach
        `_rebuild_plots()` oder nach Ändern der Einstellungen im Dialog,
        bevor der nächste Tick neue Daten liefert.
        """
        for pos, plot_item in enumerate(self._plot_items):
            channel = self._channels[self._curve_channel_indices[pos]]
            self._apply_channel_y_range(plot_item, channel, None)
        for key, window in self._popout_windows.items():
            channel = self._find_channel_by_key(key)
            if channel is not None:
                self._apply_channel_y_range(
                    window.plot_item, channel, None, self._popout_y_auto_active
                )

    def _apply_channel_y_range(
        self,
        plot_item,
        channel: Channel,
        data: np.ndarray | None,
        auto_active_cache: dict[tuple[str, str], bool] | None = None,
    ) -> None:
        """Setzt die Y-Achse eines einzelnen Subplots gemäß der pro Kanal
        konfigurierten Autoskalierung (siehe `ChannelDisplayDialog`).

        Kein reines An/Aus: Ist Autoskalierung für den Kanal aktiviert
        (Default), wird der konfigurierte feste Bereich verwendet, SOLANGE
        `data` (die aktuell angezeigten Messwerte, `None` = noch keine)
        innerhalb davon liegt - über-/unterschreitet auch nur ein Wert
        diesen Bereich, übernimmt PyQtGraphs Autoscale für den Rest des
        aktuellen Durchlaufs. Ist Autoskalierung deaktiviert, bleibt der
        feste Bereich immer aktiv, unabhängig von `data`.

        `auto_active_cache` (Default `self._channel_y_auto_active`)
        verhindert unnötige `setYRange`/`enableAutoRange`-Aufrufe, wenn
        sich der effektive Modus gegenüber dem letzten Aufruf nicht
        geändert hat. Hauptraster-Subplot und eigenes Fenster (siehe
        `ChannelPopoutWindow`) desselben Kanals nutzen bewusst
        UNTERSCHIEDLICHE Caches (`self._popout_y_auto_active`) - sie
        haben getrennte `plot_item`-Instanzen und dürfen sich beim
        Umschalten auf Autoscale nicht gegenseitig überspringen.
        """
        if auto_active_cache is None:
            auto_active_cache = self._channel_y_auto_active
        key = _channel_display_key(channel)
        default_min = channel.min_range if channel.min_range is not None else -10.0
        default_max = channel.max_range if channel.max_range is not None else 10.0
        y_min = channel.plot_y_min if channel.plot_y_min is not None else default_min
        y_max = channel.plot_y_max if channel.plot_y_max is not None else default_max
        autoscale = channel.plot_autoscale

        if autoscale:
            use_auto = bool(
                data is not None
                and data.size > 0
                and (float(np.min(data)) < y_min or float(np.max(data)) > y_max)
            )
        else:
            use_auto = False

        if auto_active_cache.get(key) == use_auto:
            return
        auto_active_cache[key] = use_auto

        if use_auto:
            plot_item.enableAutoRange(y=True)
        else:
            plot_item.enableAutoRange(y=False)
            plot_item.setYRange(y_min, y_max, padding=0)

    def _on_storage_timer_tick(self) -> None:
        """Aktualisiert die Speicherpuffer-Anzeige des Storage Writers.

        Bezugsgröße ("Maximum") ist die konfigurierte Ring-Buffer-Kapazität
        (`RingBuffer.capacity`, siehe `setup_view._calculate_dynamic_buffer_size`)
        - nicht der freie Festplattenplatz. Angezeigt wird, wie viele bereits
        vom DAQ-Thread geschriebene Samples der Storage Writer noch NICHT auf
        die Festplatte übertragen hat (`StorageWriter.pending_samples`).
        Kommt die Festplatte nicht hinterher (z. B. weil sie zu langsam oder
        voll ist), wächst dieser Rückstand; erreicht er die Kapazität, werden
        ungeschriebene Samples im Ring Buffer überschrieben - ein
        unwiederbringlicher Datenverlust (Overrun, siehe `core/ringbuffer.py`).
        Das ist damit ein direkterer Risikoindikator als der reine freie
        Festplattenplatz.
        """
        if self._storage_writer is None:
            return

        ring_buffer = self._controller.get_ring_buffer()
        if ring_buffer is None:
            return

        try:
            pending = self._storage_writer.pending_samples
            file_bytes = self._storage_writer.output_path.stat().st_size
        except (KeyError, OSError):
            logger.debug("Speicherpuffer-Status konnte nicht ermittelt werden", exc_info=True)
            return

        capacity = ring_buffer.capacity
        percent = (pending / capacity * 100.0) if capacity > 0 else 0.0

        self._storage_progress.setValue(int(round(min(100.0, percent))))
        detail_text = t(
            "storage_detail",
            file_size=_format_bytes(file_bytes),
            pending=f"{pending:,}",
            capacity=f"{capacity:,}",
            percent=f"{percent:.1f}",
        )
        if get_language() == "de":
            # Deutsches Zahlenformat: Tausenderpunkt statt -komma
            # (nur die :,-formatierten Ganzzahlen betroffen, nicht die
            # bereits mit Punkt formatierten Kommazahlen).
            detail_text = detail_text.replace(",", ".")
        self._storage_detail_label.setText(detail_text)
        if percent >= _STORAGE_CRITICAL_PERCENT:
            color = "#dc3545"  # rot
        elif percent >= _STORAGE_WARN_PERCENT:
            color = "#fd7e14"  # orange
        else:
            color = "#28a745"  # gruen
        self._storage_progress.setStyleSheet(f"QProgressBar::chunk {{ background-color: {color}; }}")

    def _on_timer_tick(self) -> None:
        if self._reader_id is None:
            return

        session = self._controller.current_session
        if session is not None and session.start_time is not None:
            self._duration_label.setText(
                t("duration_value", value=f"{session.duration_seconds:.1f} s")
            )
            # Konfiguriertes Aufnahme-Limit (siehe
            # `data/models.py::MeasurementConfig.is_recording_limit_reached`)
            # - geprueft anhand der tatsaechlich erfassten Samplezahl, NICHT
            # der Wanduhrzeit: Samples werden vom Hardware-Sample-Clock des
            # DAQ-Moduls getaktet, das macht den Grenzwert unabhaengig von
            # GUI-/Thread-Verzoegerungen zuverlaessig. Stoppt ueber denselben
            # Pfad wie der manuelle "Messung stoppen"-Button
            # (`self._stop_button.clicked.connect(self.stop_requested.emit)`),
            # damit Metadaten/Storage-Writer identisch abgeschlossen werden.
            #
            # WICHTIG bei getriggerten Messungen: waehrend der Scharf-Phase
            # (`self._armed`) noch KEINE Pruefung - es wird ja noch nichts
            # aufgezeichnet. Danach wird gegen `total_samples_acquired -
            # _recording_baseline_samples` geprueft statt gegen den rohen
            # Zaehler, da dieser bereits ab Erfassungsstart (= Scharf-
            # Zeitpunkt) laeuft, nicht erst ab dem tatsaechlichen Trigger
            # (siehe `mark_recording_started`). Bei manuellem Start bleibt
            # die Baseline 0, also unveraendertes Verhalten.
            if (
                not self._armed
                and not session.config.recording_unlimited
                and session.config.is_recording_limit_reached(
                    self._controller.total_samples_acquired
                    - self._recording_baseline_samples
                )
            ):
                self.stop_requested.emit()
                return
        self._sample_rate_label.setText(
            t("sample_rate_value", value=f"{self._sample_rate_hz:.1f} Hz")
        )

        max_display_samples = int(self._sample_rate_hz * self._display_window_seconds)
        raw = self._controller.read_live_data(self._reader_id, max_samples=max_display_samples)
        if raw.shape[1] == 0:
            return

        scaled = apply_scaling(raw, self._channels)
        self._check_threshold_trigger(scaled)
        self._check_stop_threshold_trigger(scaled)
        self._write_to_display_buffer(scaled)

        channel_views = {
            index: self._get_channel_display_view(index)
            for index in range(len(self._channels))
        }
        if not any(values.size for _times, values in channel_views.values()):
            return

        for pos, curve in enumerate(self._curves):
            channel_index = self._curve_channel_indices[pos]
            times, values = channel_views[channel_index]
            curve.setData(times, values)
            if values.size and self._value_labels[pos] is not None:
                channel = self._channels[channel_index]
                self._value_labels[pos].setText(
                    _format_channel_value(
                        values[-1], channel.plot_value_integer_digits, channel.plot_value_decimal_digits
                    )
                )

        # Hybrid-Autoskalierung pro Kanal (fester Bereich, bis Messwerte
        # ihn über-/unterschreiten - siehe `_apply_channel_y_range`) mit
        # den JETZT tatsächlich angezeigten Werten neu bewerten.
        for pos, plot_item in enumerate(self._plot_items):
            channel_index = self._curve_channel_indices[pos]
            self._apply_channel_y_range(
                plot_item, self._channels[channel_index], channel_views[channel_index][1]
            )

        # Eigene Fenster (siehe `ChannelPopoutWindow`) unabhängig vom
        # Hauptraster mit denselben Werten aktualisieren - `channel_views`
        # ist immer nach `self._channels` indiziert (siehe `apply_scaling`),
        # unabhängig von `plot_visible`, daher hier per Index statt Position.
        if self._popout_windows:
            for index, channel in enumerate(self._channels):
                window = self._popout_windows.get(_channel_display_key(channel))
                if window is None:
                    continue
                times, values = channel_views[index]
                window.curve.setData(times, values)
                if values.size:
                    window.value_label.setText(
                        _format_channel_value(
                            values[-1], channel.plot_value_integer_digits, channel.plot_value_decimal_digits
                        )
                    )
                self._apply_channel_y_range(
                    window.plot_item, channel, values, self._popout_y_auto_active
                )

        # X-Bereich selbst bleibt fensterbreit fest (Sweep scrollt nicht) -
        # nur bei einem tatsächlichen Zyklus-Wechsel (neuer Durchlauf
        # begonnen) auf die neue absolute Zeitspanne verschieben, damit die
        # Achsenbeschriftung die echte Messzeit zeigt (siehe
        # `_channel_cycle_starts`). Bewusst nicht bei jedem Tick gesetzt -
        # pro Kanal, da die Fensterlänge (und damit der Zyklus-Rhythmus)
        # pro Kanal unterschiedlich sein kann.
        for pos, plot_item in enumerate(self._plot_items):
            channel_index = self._curve_channel_indices[pos]
            x_min = self._channel_cycle_starts.get(channel_index, 0.0)
            if self._channel_x_range_applied.get(channel_index) == x_min:
                continue
            self._channel_x_range_applied[channel_index] = x_min
            x_max = x_min + self._channels[channel_index].plot_time_window_seconds
            plot_item.setXRange(x_min, x_max, padding=0)
        for key, window in self._popout_windows.items():
            channel = self._find_channel_by_key(key)
            if channel is None:
                continue
            channel_index = self._channels.index(channel)
            x_min = self._channel_cycle_starts.get(channel_index, 0.0)
            if self._channel_x_range_applied.get(channel_index) == x_min:
                continue
            self._channel_x_range_applied[channel_index] = x_min
            window.plot_item.setXRange(
                x_min, x_min + channel.plot_time_window_seconds, padding=0
            )

    def _ensure_display_buffer(self, num_channels: int) -> None:
        """Initialisiert oder passt den internen Sweep-Anzeigepuffer an."""
        max_window = max(
            [channel.plot_time_window_seconds for channel in self._channels],
            default=self._display_window_seconds,
        )
        capacity = max(1, int(self._sample_rate_hz * max_window))
        if self._display_buffer is None or self._display_buffer.shape != (num_channels, capacity):
            self._display_capacity_samples = capacity
            self._display_buffer = np.zeros((num_channels, capacity), dtype=np.float64)
            self._buffer_write_pos = 0

    def _write_to_display_buffer(self, scaled_block: np.ndarray) -> None:
        """Schreibt neue Samples in den Sweep-Puffer (siehe Klassendoc oben).

        Füllt den aktuellen Durchlauf ab der Schreibposition auf. Reicht
        der neue Block über das Fensterende hinaus, beginnt der
        überschüssige Rest einen NEUEN Durchlauf ab Index 0 - die alte
        Kurve verschwindet dabei komplett, statt (wie bei einem
        klassischen Ringpuffer) langsam am linken Rand herauszuscrollen.
        Eine Schleife statt Rekursion, falls ein einzelner Block (nach
        einer GUI-Verzögerung) sogar mehr als ein volles Fenster enthält.
        """
        if self._display_buffer is None:
            return
        for channel_index in range(scaled_block.shape[0]):
            cap = max(
                1,
                min(
                    self._display_capacity_samples,
                    int(
                        self._sample_rate_hz
                        * self._channels[channel_index].plot_time_window_seconds
                    ),
                ),
            )
            pos = self._channel_buffer_positions.get(channel_index, 0)
            start = 0
            while start < scaled_block.shape[1]:
                if pos >= cap:
                    pos = 0
                    self._channel_cycle_starts[channel_index] = (
                        self._channel_cycle_starts.get(channel_index, 0.0)
                        + cap / self._sample_rate_hz
                    )
                take = min(cap - pos, scaled_block.shape[1] - start)
                self._display_buffer[channel_index, pos:pos + take] = scaled_block[
                    channel_index, start:start + take
                ]
                pos += take
                start += take
            self._channel_buffer_positions[channel_index] = pos
        self._buffer_write_pos = max(self._channel_buffer_positions.values(), default=0)

    def _get_channel_display_view(self, channel_index: int) -> tuple[np.ndarray, np.ndarray]:
        """Gibt den aktuellen Durchlauf für einen einzelnen Kanal zurück.

        Die Zeitwerte sind um `_channel_cycle_starts[channel_index]`
        verschoben, zeigen also die tatsächliche Messzeit (z. B. "40-45s"
        im 9. Durchlauf eines 5s-Fensters) statt immer bei 0 zu beginnen -
        der Sweep selbst (Kurve läuft im festen, pro Kanal konfigurierbaren
        Fenster durch, setzt zurück) bleibt davon unverändert.

        Rückgabe wächst mit der Sweep-Position (statt einer konstanten,
        NaN-gepolsterten Fensterlänge - das wurde ausprobiert, machte die
        Darstellung aber schlechter statt besser: jede Kurve hätte dann
        JEDEN Tick auf volle Fensterlänge verarbeitet werden müssen, auch
        wenn erst wenige Punkte echte Daten sind).

        Zeigt IMMER den vollen aktuell eingetroffenen Stand
        (`_channel_buffer_positions`) - bewusst OHNE künstliches
        Nachzieh-Tempo: ein frueherer Versuch, neu eingetroffene ~25ms-
        Bloecke (siehe `gui/setup_view.py::_calculate_samples_per_read`)
        ueber mehrere Ticks zu "verschmieren", hat zwar das sichtbare
        Blockweise-Wachstum der Kurve geglaettet, dabei aber spuerbare
        zusaetzliche Latenz eingefuehrt - bei einem direkten
        Reiz-Reaktions-Test (Klopftest auf einen Beschleunigungssensor)
        war das inakzeptabel: Latenz ist fuer ein Live-Messinstrument
        wichtiger als Anzeige-Glaette.
        """
        if self._display_buffer is None:
            return np.array([]), np.empty((0,))
        position = self._channel_buffer_positions.get(channel_index, 0)
        if position == 0:
            return np.array([]), np.empty((0,))
        values = self._display_buffer[channel_index, :position]
        times = self._channel_cycle_starts.get(channel_index, 0.0) + (
            np.arange(position) / self._sample_rate_hz
        )
        return times, values
