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
    (siehe `_write_to_display_buffer`/`_get_display_view`). Das
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
    wichtiger als Anzeige-Glaette - `_get_display_view()` zeigt daher
    IMMER sofort den vollen aktuell eingetroffenen Stand.

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
from PyQt6.QtCore import QSize, QTimer, pyqtSignal, Qt
from PyQt6.QtGui import QColor, QIcon
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
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
    connect_theme_changed,
    curve_color,
    draw_play_icon,
    draw_stop_icon,
    plot_background_color,
    style_plot_container,
    style_plot_item,
)
from gui.widgets.spinbox import PrecisionDoubleSpinBox

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
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("channel_display_dialog_title"))

        self._colors: dict[str, str] = {}
        self._backgrounds: dict[str, str] = {}
        self._rows: dict[str, dict[str, QWidget]] = {}

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        for channel in channels:
            hw = channel.hardware_channel
            hw_default_min = channel.min_range if channel.min_range is not None else -10.0
            hw_default_max = channel.max_range if channel.max_range is not None else 10.0
            current_min = channel.plot_y_min if channel.plot_y_min is not None else hw_default_min
            current_max = channel.plot_y_max if channel.plot_y_max is not None else hw_default_max
            self._colors[hw] = channel.plot_color or default_color
            self._backgrounds[hw] = channel.plot_background or default_background

            row = QHBoxLayout()

            color_button = QPushButton()
            color_button.setFixedSize(24, 24)
            color_button.setToolTip(t("plot_color"))
            self._update_swatch(color_button, self._colors[hw])
            color_button.clicked.connect(
                lambda _checked, h=hw, b=color_button: self._pick_color(h, b, is_background=False)
            )
            row.addWidget(QLabel(f"{t('plot_color')}:"))
            row.addWidget(color_button)

            bg_button = QPushButton()
            bg_button.setFixedSize(24, 24)
            bg_button.setToolTip(t("plot_background"))
            self._update_swatch(bg_button, self._backgrounds[hw])
            bg_button.clicked.connect(
                lambda _checked, h=hw, b=bg_button: self._pick_color(h, b, is_background=True)
            )
            row.addWidget(QLabel(f"{t('plot_background')}:"))
            row.addWidget(bg_button)

            min_spin = PrecisionDoubleSpinBox()
            min_spin.setRange(-1e9, 1e9)
            min_spin.setValue(current_min)
            max_spin = PrecisionDoubleSpinBox()
            max_spin.setRange(-1e9, 1e9)
            max_spin.setValue(current_max)
            row.addWidget(QLabel(f"{t('min')}:"))
            row.addWidget(min_spin)
            row.addWidget(QLabel(f"{t('max')}:"))
            row.addWidget(max_spin)

            autoscale_check = QCheckBox(t("autoscale_checkbox"))
            autoscale_check.setToolTip(t("autoscale_checkbox_tooltip"))
            autoscale_check.setChecked(channel.plot_autoscale)
            row.addWidget(autoscale_check)

            # Betrifft NUR, ob der Kanal als Subplot im Hauptraster
            # erscheint (siehe `LiveView._rebuild_plots`) - Erfassung/
            # Speicherung laufen unabhängig davon unverändert weiter.
            visible_check = QCheckBox(t("plot_visible_checkbox"))
            visible_check.setToolTip(t("plot_visible_checkbox_tooltip"))
            visible_check.setChecked(channel.plot_visible)
            row.addWidget(visible_check)

            # Wirkt (wie "Aktiv" oben) erst nach OK über `results()` -
            # siehe Klassendoc oben.
            popout_check = QCheckBox(t("popout_button"))
            popout_check.setToolTip(t("popout_button_tooltip"))
            popout_check.setChecked(channel.plot_popout)
            row.addWidget(popout_check)

            form.addRow(channel.display_name, row)
            self._rows[hw] = {
                "min": min_spin,
                "max": max_spin,
                "autoscale": autoscale_check,
                "visible": visible_check,
                "popout": popout_check,
            }

        button_box = QDialogButtonBox()
        ok_button = button_box.addButton(t("ok"), QDialogButtonBox.ButtonRole.AcceptRole)
        cancel_button = button_box.addButton(t("cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        ok_button.clicked.connect(self.accept)
        cancel_button.clicked.connect(self.reject)
        layout.addWidget(button_box)

    def _pick_color(self, hw_channel: str, button: QPushButton, is_background: bool) -> None:
        store = self._backgrounds if is_background else self._colors
        initial = QColor(store.get(hw_channel, "#ffffff"))
        color = QColorDialog.getColor(initial, self)
        if not color.isValid():
            return
        store[hw_channel] = color.name()
        self._update_swatch(button, color.name())

    @staticmethod
    def _update_swatch(button: QPushButton, hex_color: str) -> None:
        button.setStyleSheet(f"background-color: {hex_color}; border: 1px solid #888888;")

    def results(self) -> dict[str, dict]:
        """Gibt die eingestellten Werte pro Kanal zurück (nur bei OK gültig).

        Format je Kanal passend zu `Channel.plot_*`/
        `ChannelTableWidget.apply_display_settings` /
        `LiveView._apply_display_settings_to_live_channels`.
        """
        return {
            hw_channel: {
                "plot_color": self._colors[hw_channel],
                "plot_background": self._backgrounds[hw_channel],
                "plot_y_min": row["min"].value(),
                "plot_y_max": row["max"].value(),
                "plot_autoscale": row["autoscale"].isChecked(),
                "plot_visible": row["visible"].isChecked(),
                "plot_popout": row["popout"].isChecked(),
            }
            for hw_channel, row in self._rows.items()
        }


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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        self.plot_widget = pg.PlotWidget()
        self.plot_item = self.plot_widget.getPlotItem()
        self.plot_item.setTitle(title)
        self.plot_item.showGrid(x=True, y=True, alpha=0.3)
        self.plot_item.setLabel("bottom", t("axis_time"), units="s")
        style_plot_container(self.plot_widget)
        style_plot_item(self.plot_item)

        self.curve = self.plot_item.plot(pen=pg.mkPen(color=curve_color(), width=1.5))
        self.curve.setDownsampling(auto=True, method="mean")
        self.curve.setClipToView(True)
        self.plot_item.enableAutoRange(x=False)

        layout.addWidget(self.plot_widget)

        connect_theme_changed(self._retheme)

    def _retheme(self) -> None:
        style_plot_container(self.plot_widget)
        style_plot_item(self.plot_item)


class LiveView(QWidget):
    """Zeigt Messdaten einer laufenden Messung in Echtzeit an.

    Signals:
        start_requested: Der Nutzer hat auf "Messung starten" geklickt.
            `gui/main_window.py` startet die Messung dann mit der aktuell
            konfigurierten Setup-Konfiguration.
        stop_requested: Der Nutzer hat auf "Messung stoppen" geklickt.
            `gui/main_window.py` ist dafür zuständig, die Messung über
            den `MeasurementController` tatsächlich zu stoppen.
        trigger_fired: Ein scharf geschalteter Schwellwert-Trigger (siehe
            `enter_armed_state`) hat ausgelöst - `gui/main_window.py`
            erzeugt daraufhin den StorageWriter (ggf. rückwirkend, siehe
            `_on_trigger_fired`).
    """

    start_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    trigger_fired = pyqtSignal()

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
        # Kurve verschwindet komplett). `_buffer_write_pos` ist zugleich
        # die Anzahl gültiger Samples im aktuellen Durchlauf (siehe
        # `_write_to_display_buffer`/`_get_display_view`).
        self._display_buffer: np.ndarray | None = None
        self._display_capacity_samples: int = 0
        self._buffer_write_pos: int = 0
        # Absolute Messzeit (Sekunden seit Messstart), bei der der AKTUELLE
        # Durchlauf begonnen hat - die Achsenbeschriftung soll die echte
        # Messzeit zeigen (z. B. "40-45s" statt immer "0-5s"), auch wenn
        # der Sweep selbst weiterhin bei jedem Durchlauf zurücksetzt.
        # Erhöht sich um `_display_window_seconds`, sobald ein neuer
        # Durchlauf beginnt (siehe `_write_to_display_buffer`).
        self._cycle_start_seconds: float = 0.0
        # Zuletzt auf die Plots angewendeter `_cycle_start_seconds`-Wert
        # (siehe `_on_timer_tick`) - der X-Bereich wird nur bei einem
        # tatsächlichen Zyklus-Wechsel neu gesetzt, nicht bei jedem Tick.
        self._x_range_cycle_start: float | None = None

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
        self._channel_y_auto_active: dict[str, bool] = {}

        self._plot_widget = pg.GraphicsLayoutWidget()
        self._plot_items: list = []
        self._curves: list = []
        # `self._curves[i]`/`self._plot_items[i]` gehören zum Kanal
        # `self._channels[self._curve_channel_indices[i]]` - NICHT mehr
        # zwangsläufig `self._channels[i]`, seit unsichtbare Kanäle
        # (`Channel.plot_visible=False`) keinen Subplot mehr bekommen
        # (siehe `_rebuild_plots`).
        self._curve_channel_indices: list[int] = []

        # Eigene Fenster einzelner Kanäle (siehe `ChannelPopoutWindow`,
        # `_on_popout_requested`), nach hardware_channel - unabhängig von
        # `plot_visible`: ein Kanal kann im Hauptraster ausgeblendet UND
        # trotzdem in einem eigenen Fenster sichtbar sein. Eigener
        # Autoscale-Zustands-Cache (siehe `_apply_channel_y_range`), damit
        # sich Popout und Hauptraster-Subplot eines Kanals nicht
        # gegenseitig die Skalierung "wegcachen".
        self._popout_windows: dict[str, ChannelPopoutWindow] = {}
        self._popout_y_auto_active: dict[str, bool] = {}

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

        self._start_button = QPushButton()
        self._set_start_button_text()
        self._start_button.setIconSize(QSize(18, 18))
        self._start_button.setStyleSheet(
            "QPushButton { background-color: #1f7a36; color: #f9fafb; border: none; padding: 6px 16px; border-radius: 4px; font-weight: 700; font-size: 11pt; }"
            "QPushButton:hover { background-color: #1a662e; }"
            "QPushButton:pressed { background-color: #145125; }"
        )
        self._start_button.clicked.connect(self.start_requested.emit)

        self._stop_button = QPushButton()
        self._set_stop_button_text()
        self._stop_button.setIconSize(QSize(18, 18))
        self._stop_button.setStyleSheet(
            "QPushButton { background-color: #dc3545; color: #f9fafb; border: none; padding: 6px 16px; border-radius: 4px; font-weight: 700; font-size: 11pt; }"
            "QPushButton:hover { background-color: #c82333; }"
            "QPushButton:pressed { background-color: #bd2130; }"
        )
        self._stop_button.clicked.connect(self.stop_requested.emit)

        self._retheme_action_button_icons()

        info_row.addWidget(self._duration_label, 0, Qt.AlignmentFlag.AlignVCenter)
        info_row.addWidget(self._sample_rate_label, 0, Qt.AlignmentFlag.AlignVCenter)
        info_row.addStretch(1)
        info_row.addWidget(self._start_button, 0, Qt.AlignmentFlag.AlignVCenter)
        info_row.addWidget(self._stop_button, 0, Qt.AlignmentFlag.AlignVCenter)
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
        self._cycle_start_seconds = 0.0
        self._x_range_cycle_start = None
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
        self._set_start_button_text()
        self._set_stop_button_text()
        self._storage_group.setTitle(t("storage_buffer_group"))

        for plot_item in self._plot_items:
            plot_item.setLabel("bottom", t("axis_time"), units="s")

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
        for plot_item in self._plot_items:
            style_plot_item(plot_item)
        # Kurvenfarbe/Hintergrund NICHT pauschal auf den Theme-Default
        # zurücksetzen - individuell konfigurierte Kanalfarben (siehe
        # `open_channel_display_dialog`) sollen einen Theme-Wechsel
        # überstehen; `_apply_channel_appearance()` wendet für Kanäle OHNE
        # eigene Farbe ohnehin den (jetzt neuen) Theme-Default an.
        self._apply_channel_appearance()

    def _retheme_action_button_icons(self) -> None:
        self._start_button.setIcon(QIcon(draw_play_icon(20, y_offset=0.6)))
        self._stop_button.setIcon(QIcon(draw_stop_icon(20, y_offset=0.6)))

    def _set_start_button_text(self) -> None:
        self._start_button.setText(f"  {t('start_measurement')}")

    def _set_stop_button_text(self) -> None:
        self._stop_button.setText(f"  {t('stop_measurement')}")

    def set_start_enabled(self, enabled: bool) -> None:
        self._start_button.setEnabled(enabled)

    def open_channel_display_dialog(
        self, channels: list[Channel] | None = None
    ) -> dict[str, dict] | None:
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
        dialog = ChannelDisplayDialog(channels, curve_color(), plot_background_color(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        settings = dialog.results()
        self._apply_display_settings_to_live_channels(settings)
        return settings

    def _apply_display_settings_to_live_channels(self, settings: dict[str, dict]) -> None:
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
        for channel in self._channels:
            values = settings.get(channel.hardware_channel)
            if values is None:
                continue
            channel.plot_color = values.get("plot_color")
            channel.plot_background = values.get("plot_background")
            channel.plot_y_min = values.get("plot_y_min")
            channel.plot_y_max = values.get("plot_y_max")
            channel.plot_autoscale = values.get("plot_autoscale", True)
            new_visible = values.get("plot_visible", True)
            new_popout = values.get("plot_popout", False)
            if new_visible != channel.plot_visible or new_popout != channel.plot_popout:
                visibility_changed = True
            channel.plot_visible = new_visible
            channel.plot_popout = new_popout
            changed = True
        if visibility_changed:
            # Welche Kanäle überhaupt einen Subplot bekommen, hat sich
            # geändert - Farb-/Bereichs-Anwendung ist Teil von
            # `_rebuild_plots()` und muss daher nicht separat erfolgen.
            self._rebuild_plots()
        elif changed:
            self._apply_channel_appearance()
            self._apply_y_range_mode()

    def _find_channel(self, hardware_channel: str) -> Channel | None:
        return next(
            (c for c in self._channels if c.hardware_channel == hardware_channel), None
        )

    def _open_popout_window(self, channel: Channel) -> None:
        """Öffnet ein eigenständiges Fenster mit dem Live-Plot eines
        einzelnen Kanals (siehe `ChannelPopoutWindow`), oder aktiviert ein
        dafür bereits offenes Fenster, statt ein zweites zu öffnen."""
        existing = self._popout_windows.get(channel.hardware_channel)
        if existing is not None:
            existing.raise_()
            existing.activateWindow()
            return

        window = ChannelPopoutWindow(channel, self)
        window.plot_item.setXRange(
            self._cycle_start_seconds,
            self._cycle_start_seconds + self._display_window_seconds,
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

        def _on_window_destroyed(_obj=None, hw=channel.hardware_channel, view_ref=view_ref) -> None:
            view = view_ref()
            if view is not None:
                view._on_popout_window_closed(hw)

        window.destroyed.connect(_on_window_destroyed)
        self._popout_windows[channel.hardware_channel] = window
        window.show()

    def _on_popout_window_closed(self, hardware_channel: str) -> None:
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
        self._popout_windows.pop(hardware_channel, None)
        if sip.isdeleted(self):
            return
        channel = self._find_channel(hardware_channel)
        if channel is not None and channel.plot_popout:
            channel.plot_popout = False
            self._rebuild_plots()

    # ------------------------------------------------------------------ #
    # Interna
    # ------------------------------------------------------------------ #

    def _rebuild_plots(self) -> None:
        """Erzeugt für jeden im Hauptraster sichtbaren Kanal einen eigenen,
        X-Achsen-verknüpften Subplot.

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
        self._channel_y_auto_active = {}

        previous_plot_item = None
        for index, channel in enumerate(self._channels):
            if not channel.plot_visible or channel.plot_popout:
                continue
            unit_suffix = f" [{channel.unit}]" if channel.unit else ""
            plot_item = self._plot_widget.addPlot(title=f"{channel.display_name}{unit_suffix}")
            plot_item.showGrid(x=True, y=True, alpha=0.3)
            plot_item.setLabel("bottom", t("axis_time"), units="s")
            style_plot_item(plot_item)
            if previous_plot_item is not None:
                plot_item.setXLink(previous_plot_item)
            curve = plot_item.plot(pen=pg.mkPen(color=curve_color(), width=1.5))
            curve.setDownsampling(auto=True, method="mean")
            curve.setClipToView(True)

            # Sweep-Anzeige (Oszilloskop-Art, siehe Klassendoc weiter oben):
            # das Zeitfenster steht fest bei [0, Fensterlaenge] - es scrollt
            # NICHT mit, die Kurve selbst laeuft innerhalb dieses festen
            # Fensters von links nach rechts durch.
            plot_item.enableAutoRange(x=False)
            plot_item.setXRange(0.0, self._display_window_seconds, padding=0)

            self._plot_widget.nextRow()
            self._plot_items.append(plot_item)
            self._curves.append(curve)
            self._curve_channel_indices.append(index)
            previous_plot_item = plot_item

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
        for hw in list(self._popout_windows.keys()):
            channel = self._find_channel(hw)
            if channel is None or not channel.plot_visible or not channel.plot_popout:
                self._popout_windows[hw].close()

        # Kanäle, die als "eigenes Fenster" konfiguriert sind
        # (`plot_popout=True`, z. B. aus einer geladenen Konfiguration
        # oder nach einem Messstart), aber noch kein offenes Fenster
        # haben, automatisch öffnen - sonst würde ein solcher Kanal sonst
        # spurlos verschwinden (weder Hauptraster noch Fenster sichtbar).
        for channel in self._channels:
            if (
                channel.plot_visible
                and channel.plot_popout
                and channel.hardware_channel not in self._popout_windows
            ):
                self._open_popout_window(channel)

        self._apply_channel_appearance()
        self._apply_y_range_mode()

    @staticmethod
    def _apply_channel_curve_style(plot_item, curve, channel: Channel) -> None:
        """Wendet Kurvenfarbe und Hintergrundfarbe EINES Kanals auf sein
        Plot/Kurven-Paar an - Theme-Default, falls keine eigene Farbe
        konfiguriert ist. Gemeinsam genutzt von Hauptraster-Subplots
        (`_apply_channel_appearance`) und eigenen Fenstern
        (`_open_popout_window`)."""
        color = channel.plot_color or curve_color()
        background = channel.plot_background or plot_background_color()
        curve.setPen(pg.mkPen(color=color, width=1.5))
        plot_item.getViewBox().setBackgroundColor(background)

    def _apply_channel_appearance(self) -> None:
        """Wendet Kurvenfarbe und Hintergrundfarbe pro Kanal an (siehe
        `open_channel_display_dialog`), für Hauptraster-Subplots UND
        offene eigene Fenster."""
        for pos, (plot_item, curve) in enumerate(zip(self._plot_items, self._curves)):
            channel = self._channels[self._curve_channel_indices[pos]]
            self._apply_channel_curve_style(plot_item, curve, channel)
        for hw, window in self._popout_windows.items():
            channel = self._find_channel(hw)
            if channel is not None:
                self._apply_channel_curve_style(window.plot_item, window.curve, channel)

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
        for hw, window in self._popout_windows.items():
            channel = self._find_channel(hw)
            if channel is not None:
                self._apply_channel_y_range(
                    window.plot_item, channel, None, self._popout_y_auto_active
                )

    def _apply_channel_y_range(
        self,
        plot_item,
        channel: Channel,
        data: np.ndarray | None,
        auto_active_cache: dict[str, bool] | None = None,
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
        hw = channel.hardware_channel
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

        if auto_active_cache.get(hw) == use_auto:
            return
        auto_active_cache[hw] = use_auto

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
            if not self._armed and session.config.is_recording_limit_reached(
                self._controller.total_samples_acquired - self._recording_baseline_samples
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

        times, all_values = self._get_display_view()
        if all_values.size == 0:
            return

        for pos, curve in enumerate(self._curves):
            curve.setData(times, all_values[self._curve_channel_indices[pos]])

        # Hybrid-Autoskalierung pro Kanal (fester Bereich, bis Messwerte
        # ihn über-/unterschreiten - siehe `_apply_channel_y_range`) mit
        # den JETZT tatsächlich angezeigten Werten neu bewerten.
        for pos, plot_item in enumerate(self._plot_items):
            channel_index = self._curve_channel_indices[pos]
            self._apply_channel_y_range(plot_item, self._channels[channel_index], all_values[channel_index])

        # Eigene Fenster (siehe `ChannelPopoutWindow`) unabhängig vom
        # Hauptraster mit denselben Werten aktualisieren - `all_values`
        # ist immer nach `self._channels` indiziert (siehe `apply_scaling`),
        # unabhängig von `plot_visible`, daher hier per Index statt Position.
        if self._popout_windows:
            for index, channel in enumerate(self._channels):
                window = self._popout_windows.get(channel.hardware_channel)
                if window is None:
                    continue
                window.curve.setData(times, all_values[index])
                self._apply_channel_y_range(
                    window.plot_item, channel, all_values[index], self._popout_y_auto_active
                )

        # X-Bereich selbst bleibt fensterbreit fest (Sweep scrollt nicht) -
        # nur bei einem tatsächlichen Zyklus-Wechsel (neuer Durchlauf
        # begonnen) auf die neue absolute Zeitspanne verschieben, damit die
        # Achsenbeschriftung die echte Messzeit zeigt (siehe
        # `_cycle_start_seconds`). Bewusst nicht bei jedem Tick gesetzt.
        if self._cycle_start_seconds != self._x_range_cycle_start:
            self._x_range_cycle_start = self._cycle_start_seconds
            x_min = self._cycle_start_seconds
            x_max = self._cycle_start_seconds + self._display_window_seconds
            for plot_item in self._plot_items:
                plot_item.setXRange(x_min, x_max, padding=0)
            for window in self._popout_windows.values():
                window.plot_item.setXRange(x_min, x_max, padding=0)

    def _ensure_display_buffer(self, num_channels: int) -> None:
        """Initialisiert oder passt den internen Sweep-Anzeigepuffer an."""
        capacity = max(1, int(self._sample_rate_hz * self._display_window_seconds))
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
        cap = self._display_capacity_samples
        remaining = scaled_block

        while remaining.shape[1] > 0:
            pos = self._buffer_write_pos
            if pos >= cap:
                # Voriger Durchlauf war exakt voll (siehe unten) - der
                # volle letzte Frame wurde bereits einmal angezeigt
                # (`_get_display_view` zeigt `_buffer_write_pos == cap`
                # korrekt als volles Fenster); jetzt beginnt der naechste
                # Durchlauf bei 0. Absolute Startzeit des neuen Durchlaufs
                # fuer die Achsenbeschriftung mitfuehren (siehe
                # `_cycle_start_seconds`).
                pos = 0
                self._cycle_start_seconds += cap / self._sample_rate_hz
            space = cap - pos
            take = min(space, remaining.shape[1])
            self._display_buffer[:, pos:pos + take] = remaining[:, :take]
            pos += take
            remaining = remaining[:, take:]
            self._buffer_write_pos = pos

    def _get_display_view(self) -> tuple[np.ndarray, np.ndarray]:
        """Gibt den aktuellen Durchlauf zurück.

        Die Zeitwerte sind um `_cycle_start_seconds` verschoben, zeigen
        also die tatsächliche Messzeit (z. B. "40-45s" im 9. Durchlauf
        eines 5s-Fensters) statt immer bei 0 zu beginnen - der Sweep
        selbst (Kurve läuft im festen Fenster durch, setzt zurück) bleibt
        davon unverändert.

        Rückgabe wächst mit der Sweep-Position (statt einer konstanten,
        NaN-gepolsterten Fensterlänge - das wurde ausprobiert, machte die
        Darstellung aber schlechter statt besser: jede Kurve hätte dann
        JEDEN Tick auf volle Fensterlänge verarbeitet werden müssen, auch
        wenn erst wenige Punkte echte Daten sind).

        Zeigt IMMER den vollen aktuell eingetroffenen Stand
        (`_buffer_write_pos`) - bewusst OHNE künstliches Nachzieh-Tempo:
        ein frueherer Versuch, neu eingetroffene ~25ms-Bloecke (siehe
        `gui/setup_view.py::_calculate_samples_per_read`) ueber mehrere
        Ticks zu "verschmieren", hat zwar das sichtbare Blockweise-
        Wachstum der Kurve geglaettet, dabei aber spuerbare zusaetzliche
        Latenz eingefuehrt - bei einem direkten Reiz-Reaktions-Test (Klopf-
        test auf einen Beschleunigungssensor) war das inakzeptabel: Latenz
        ist fuer ein Live-Messinstrument wichtiger als Anzeige-Glaette.
        """
        m = self._buffer_write_pos
        if self._display_buffer is None or m == 0:
            return np.array([]), np.empty((0, 0))

        view = self._display_buffer[:, :m]
        times = self._cycle_start_seconds + np.arange(m) / self._sample_rate_hz
        return times, view
