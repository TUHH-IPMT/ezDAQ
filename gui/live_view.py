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

Architektur-Hinweis (Performance):
    Für die Live-Anzeige wird ein rollierendes Zeitfenster (Default: 5 s)
    aus einzelnen, bereits gelesenen Datenblöcken zusammengesetzt
    (`collections.deque` mit `maxlen`, ältere Blöcke fallen automatisch
    heraus). Das ist für "normale" Laborabtastraten (bis einige kHz über
    mehrere Kanäle) ausreichend performant. Bei sehr hohen Abtastraten
    (z. B. 100 kHz über viele Kanäle) über lange Anzeigefenster würde ein
    Downsampling der Anzeigedaten (z. B. Min/Max-Dezimierung pro Pixel)
    die Zeichenlast weiter reduzieren - das ist als spätere Optimierung
    vorgesehen und hier bewusst noch nicht implementiert (Version 1).
"""

from __future__ import annotations

import logging

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.controller import MeasurementController
from core.measurement import apply_scaling
from data.exporter import StorageWriter
from data.models import Channel

logger = logging.getLogger(__name__)

_DEFAULT_DISPLAY_WINDOW_SECONDS = 10.0
_UI_UPDATE_INTERVAL_MS = 33  # ~30 Hz; fluessigere Darstellung bei moderater Last
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


class LiveView(QWidget):
    """Zeigt Messdaten einer laufenden Messung in Echtzeit an.

    Signals:
        stop_requested: Der Nutzer hat auf "Messung stoppen" geklickt.
            `gui/main_window.py` ist dafür zuständig, die Messung über
            den `MeasurementController` tatsächlich zu stoppen.
    """

    stop_requested = pyqtSignal()

    def __init__(self, controller: MeasurementController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller

        self._reader_id: int | None = None
        self._channels: list[Channel] = []
        self._sample_rate_hz: float = 1.0
        self._total_samples_displayed = 0
        self._display_window_seconds = _DEFAULT_DISPLAY_WINDOW_SECONDS

        # Anzeige-Ringpuffer für die letzten N Samples.
        self._display_buffer: np.ndarray | None = None
        self._display_capacity_samples: int = 0
        self._buffer_write_pos: int = 0
        self._buffer_filled: int = 0

        self._plot_widget = pg.GraphicsLayoutWidget()
        self._plot_items: list = []
        self._curves: list = []

        # Stat-Panel Labels (werden in _rebuild_plots initialisiert)
        self._stat_labels: dict[str, QLabel] = {}

        # StorageWriter der laufenden Messung (None bei "Nur Live anzeigen").
        self._storage_writer: StorageWriter | None = None

        self._timer = QTimer(self)
        self._timer.setInterval(_UI_UPDATE_INTERVAL_MS)
        self._timer.timeout.connect(self._on_timer_tick)

        self._storage_timer = QTimer(self)
        self._storage_timer.setInterval(_STORAGE_UPDATE_INTERVAL_MS)
        self._storage_timer.timeout.connect(self._on_storage_timer_tick)

        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        info_row = QHBoxLayout()
        self._duration_label = QLabel("Dauer: -")
        self._sample_rate_label = QLabel("Abtastrate: -")
        self._stop_button = QPushButton("Messung stoppen")
        self._stop_button.setStyleSheet(
            "QPushButton { background-color: #dc3545; color: white; border: none; padding: 6px 16px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #c82333; }"
            "QPushButton:pressed { background-color: #bd2130; }"
        )
        self._stop_button.clicked.connect(self.stop_requested.emit)
        info_row.addWidget(self._duration_label)
        info_row.addWidget(self._sample_rate_label)
        info_row.addStretch(1)
        info_row.addWidget(self._stop_button)
        layout.addLayout(info_row)

        layout.addWidget(self._plot_widget, stretch=1)

        # Stat-Panel für Min/Max/RMS
        self._stat_group = QGroupBox("Statistiken (aktuelle 10 s)")
        self._stat_layout = QGridLayout(self._stat_group)
        layout.addWidget(self._stat_group)

        # Pufferauslastung des Storage Writers (Schreib-Rückstand ggü. DAQ-Thread).
        self._storage_group = QGroupBox("Speicherpuffer (Schreib-Rückstand)")
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

    def start_display(
        self,
        channels: list[Channel],
        sample_rate_hz: float,
        storage_writer: StorageWriter | None = None,
    ) -> None:
        """Beginnt die Live-Anzeige für eine neu gestartete Messung.

        Registriert einen eigenen, unabhängigen Ring-Buffer-Reader (siehe
        `MeasurementController.register_reader`) - die Live View darf
        Samples verlieren/überspringen, ohne den Storage Writer zu
        beeinträchtigen (siehe `core/ringbuffer.py`).

        Args:
            storage_writer: Der `StorageWriter` der laufenden Messung, falls
                gespeichert wird. `None` bei "Nur Live anzeigen" - dann
                bleibt die Speicherpuffer-Anzeige ausgeblendet.
        """
        self._channels = channels
        self._sample_rate_hz = sample_rate_hz
        self._total_samples_displayed = 0
        self._reader_id = self._controller.register_reader()

        self._rebuild_plots()
        self._ensure_display_buffer(len(channels))
        self._timer.start()

        self._storage_writer = storage_writer
        self._storage_group.setVisible(storage_writer is not None)
        if storage_writer is not None:
            self._on_storage_timer_tick()
            self._storage_timer.start()

        logger.info(
            "Live View gestartet für %d Kanäle bei %.1f Hz", len(channels), sample_rate_hz
        )

    def stop_display(self) -> None:
        """Beendet die Live-Anzeige (nach Messungsende)."""
        self._timer.stop()
        self._storage_timer.stop()
        if self._reader_id is not None:
            self._controller.unregister_reader(self._reader_id)
            self._reader_id = None
        logger.info("Live View gestoppt")

    # ------------------------------------------------------------------ #
    # Interna
    # ------------------------------------------------------------------ #

    def _rebuild_plots(self) -> None:
        """Erzeugt für jeden Kanal einen eigenen, X-Achsen-verknüpften Subplot."""
        self._plot_widget.clear()
        self._plot_items = []
        self._curves = []

        previous_plot_item = None
        for channel in self._channels:
            unit_suffix = f" [{channel.unit}]" if channel.unit else ""
            plot_item = self._plot_widget.addPlot(title=f"{channel.display_name}{unit_suffix}")
            plot_item.showGrid(x=True, y=True, alpha=0.3)
            plot_item.setLabel("bottom", "Zeit", units="s")
            if previous_plot_item is not None:
                plot_item.setXLink(previous_plot_item)
            curve = plot_item.plot(pen=pg.mkPen(width=1.5))
            curve.setDownsampling(auto=True, method="mean")
            curve.setClipToView(True)

            self._plot_widget.nextRow()
            self._plot_items.append(plot_item)
            self._curves.append(curve)
            previous_plot_item = plot_item

        # Baue auch das Stat-Panel neu
        # Leere das alte Layout
        while self._stat_layout.count():
            item = self._stat_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._stat_labels = {}

        # Kopfzeile
        self._stat_layout.addWidget(QLabel("<b>Kanal</b>"), 0, 0)
        self._stat_layout.addWidget(QLabel("<b>Min</b>"), 0, 1)
        self._stat_layout.addWidget(QLabel("<b>Max</b>"), 0, 2)
        self._stat_layout.addWidget(QLabel("<b>RMS</b>"), 0, 3)

        # Reihe pro Kanal
        for row, channel in enumerate(self._channels, start=1):
            ch_label = QLabel(f"{channel.display_name}")
            min_label = QLabel("-")
            max_label = QLabel("-")
            rms_label = QLabel("-")

            self._stat_layout.addWidget(ch_label, row, 0)
            self._stat_layout.addWidget(min_label, row, 1)
            self._stat_layout.addWidget(max_label, row, 2)
            self._stat_layout.addWidget(rms_label, row, 3)

            self._stat_labels[f"{channel.hardware_channel}_min"] = min_label
            self._stat_labels[f"{channel.hardware_channel}_max"] = max_label
            self._stat_labels[f"{channel.hardware_channel}_rms"] = rms_label

    def _maxlen_for_window(self) -> int:
        """Grobe Schätzung, wie viele Tick-Einträge für das Anzeigefenster
        benötigt werden (mind. 2, um Division durch 0 zu vermeiden)."""
        ticks_per_second = 1000.0 / _UI_UPDATE_INTERVAL_MS
        return max(2, int(self._display_window_seconds * ticks_per_second) + 1)

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
        self._storage_detail_label.setText(
            f"Datei: {_format_bytes(file_bytes)} — Rückstand: "
            f"{pending:,} / {capacity:,} Samples ({percent:.1f} %)".replace(",", ".")
        )
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
            self._duration_label.setText(f"Dauer: {session.duration_seconds:.1f} s")
        self._sample_rate_label.setText(f"Abtastrate: {self._sample_rate_hz:.1f} Hz")

        max_display_samples = int(self._sample_rate_hz * self._display_window_seconds)
        raw = self._controller.read_live_data(self._reader_id, max_samples=max_display_samples)
        if raw.shape[1] == 0:
            return

        scaled = apply_scaling(raw, self._channels)
        n = scaled.shape[1]
        self._write_to_display_buffer(scaled)
        self._total_samples_displayed += n

        times, all_values = self._get_display_view()
        if all_values.size == 0:
            return

        for i, curve in enumerate(self._curves):
            curve.setData(times, all_values[i])

        # Setze X-Achsenbereich auf das letzte Fenster (scrolling view)
        try:
            if times.size > 0:
                x_max = float(times[-1])
                x_min = x_max - float(self._display_window_seconds)
                if self._plot_items:
                    self._plot_items[0].setXRange(x_min, x_max, padding=0)
        except Exception:
            logger.exception("Fehler beim Setzen des X-Bereichs im Live View")

        # Aktualisiere Statistiken
        self._update_statistics(all_values)

    def _update_statistics(self, all_values: np.ndarray) -> None:
        """Berechnet und aktualisiert Min/Max/RMS für jeden Kanal."""
        if all_values.size == 0:
            return
        for i, channel in enumerate(self._channels):
            try:
                ch_data = all_values[i]
                ch_min = float(np.min(ch_data))
                ch_max = float(np.max(ch_data))
                ch_rms = float(np.sqrt(np.mean(ch_data ** 2)))

                min_key = f"{channel.hardware_channel}_min"
                max_key = f"{channel.hardware_channel}_max"
                rms_key = f"{channel.hardware_channel}_rms"

                if min_key in self._stat_labels:
                    self._stat_labels[min_key].setText(f"{ch_min:.3f}")
                if max_key in self._stat_labels:
                    self._stat_labels[max_key].setText(f"{ch_max:.3f}")
                if rms_key in self._stat_labels:
                    self._stat_labels[rms_key].setText(f"{ch_rms:.3f}")
            except Exception:
                logger.exception(f"Fehler beim Berechnen der Stats für Kanal {i}")

    def _ensure_display_buffer(self, num_channels: int) -> None:
        """Initialisiert oder passt den internen Anzeige-Puffer an."""
        capacity = max(1, int(self._sample_rate_hz * self._display_window_seconds))
        if self._display_buffer is None or self._display_buffer.shape != (num_channels, capacity):
            self._display_capacity_samples = capacity
            self._display_buffer = np.zeros((num_channels, capacity), dtype=np.float64)
            self._buffer_write_pos = 0
            self._buffer_filled = 0

    def _write_to_display_buffer(self, scaled_block: np.ndarray) -> None:
        if self._display_buffer is None:
            return
        num_channels, n = scaled_block.shape
        cap = self._display_capacity_samples
        pos = self._buffer_write_pos

        if n >= cap:
            self._display_buffer[:, :] = scaled_block[:, -cap:]
            self._buffer_write_pos = 0
            self._buffer_filled = cap
            return

        end = pos + n
        if end <= cap:
            self._display_buffer[:, pos:end] = scaled_block
        else:
            first_part = cap - pos
            self._display_buffer[:, pos:] = scaled_block[:, :first_part]
            self._display_buffer[:, : n - first_part] = scaled_block[:, first_part:]

        self._buffer_write_pos = (pos + n) % cap
        self._buffer_filled = min(cap, self._buffer_filled + n)

    def _get_display_view(self) -> tuple[np.ndarray, np.ndarray]:
        if self._display_buffer is None or self._buffer_filled == 0:
            return np.array([]), np.empty((0, 0))

        cap = self._display_capacity_samples
        m = self._buffer_filled
        pos = self._buffer_write_pos
        oldest = (pos - m) % cap

        if oldest + m <= cap:
            view = self._display_buffer[:, oldest:oldest + m]
        else:
            first_part = cap - oldest
            view = np.concatenate(
                (self._display_buffer[:, oldest:], self._display_buffer[:, : m - first_part]),
                axis=1,
            )

        start_index = max(0, self._total_samples_displayed - m)
        times = (start_index + np.arange(m)) / self._sample_rate_hz
        return times, view
