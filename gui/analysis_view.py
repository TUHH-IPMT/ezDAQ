"""
gui/analysis_view.py

Analyse-Ansicht.

Funktionen (siehe Vorgabe):
    * Drag & Drop von Messdateien (.parquet, .csv)
    * Metadaten laden (falls vorhanden)
    * Kanäle auswählen
    * Plot anzeigen, Zoom/Pan (nativ durch PyQtGraph)
    * Analysefunktionen (FFT, Tief-/Hochpass, Glättung, siehe
      `analysis/basic_analysis.py`) - Ergebnisse werden als neuer Kanal
      unter der Quelldatei im Dateibrowser abgelegt und können per
      Rechtsklick als CSV/Parquet gespeichert werden.

Noch NICHT implementiert (siehe Vorgabe): RMS, Statistik, automatische
Reports. Die Architektur ist jedoch darauf vorbereitet - siehe
`analysis/basic_analysis.py` für die vorgesehenen Erweiterungspunkte.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PyQt6.QtCore import QEvent, QSize, pyqtSignal, Qt
from PyQt6.QtGui import QDrag, QDragEnterEvent, QDropEvent, QFont, QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from analysis.basic_analysis import apply_filter, apply_smoothing, compute_fft, native_samples
from data.loader import LoadedMeasurement, infer_metadata_path, load_measurement_file
from data.models import Channel
from gui.i18n import connect_language_changed, t
from gui.dialogs import confirm_delete
from gui.theme import (
    axis_tick_point_size,
    connect_theme_changed,
    draw_fft_icon,
    draw_highpass_icon,
    draw_lowpass_icon,
    draw_smoothing_icon,
    plot_background_color,
    repolish,
    style_plot_container,
    style_plot_item,
)
from gui.widgets.spinbox import NoWheelSpinBox, PrecisionDoubleSpinBox
from gui.workers import BackgroundWorker

logger = logging.getLogger(__name__)

_SUPPORTED_SUFFIXES = {".parquet", ".csv"}
_DRAG_PREFIX = "daq.channel/"
_ROLE_FILE_PATH = int(Qt.ItemDataRole.UserRole)
_ROLE_CHANNEL_NAME = int(Qt.ItemDataRole.UserRole) + 1
_ROLE_MEASUREMENT = int(Qt.ItemDataRole.UserRole) + 2
_ROLE_IS_RESULT = int(Qt.ItemDataRole.UserRole) + 3
_FREQUENCY_X_COLUMN = "frequency_hz"

# (kind, icon_fn, label_i18n_key, tooltip_i18n_key) - gemeinsam für den
# Button-Aufbau in AnalysisView.__init__ und das Neuzeichnen der Icons in
# retheme_plots() nach einem Theme-Wechsel.
_FUNCTION_SPECS = [
    ("fft", draw_fft_icon, "analysis_fft_button", "analysis_fft_tooltip"),
    ("lowpass", draw_lowpass_icon, "analysis_lowpass_button", "analysis_lowpass_tooltip"),
    ("highpass", draw_highpass_icon, "analysis_highpass_button", "analysis_highpass_tooltip"),
    ("smoothing", draw_smoothing_icon, "analysis_smoothing_button", "analysis_smoothing_tooltip"),
]
_FUNCTION_SPECS_BY_KIND = {spec[0]: spec for spec in _FUNCTION_SPECS}

# Gruppierung der Analysefunktions-Buttons im Toolkasten: Spektralanalyse
# (FFT) getrennt von Filtern (Tief-/Hochpass UND Glättung).
_FUNCTION_CATEGORIES = [
    ("analysis_category_spectral", ["fft"]),
    ("analysis_category_filter", ["lowpass", "highpass", "smoothing"]),
]
_LAYOUT_SPECS = {
    "single": [(0, 0, 0, 1, 1)],
    "split": [(0, 0, 0, 1, 1), (1, 1, 0, 1, 1)],
    "three": [(0, 0, 0, 1, 1), (1, 1, 0, 1, 1), (2, 2, 0, 1, 1)],
    "four": [(0, 0, 0, 1, 1), (1, 1, 0, 1, 1), (2, 2, 0, 1, 1), (3, 3, 0, 1, 1)],
    "four_square": [(0, 0, 0, 1, 1), (1, 0, 1, 1, 1), (2, 1, 0, 1, 1), (3, 1, 1, 1, 1)],
}


class ChannelTreeWidget(QTreeWidget):
    """Tree mit explizitem Drag-Payload für Kanal-Zuordnung auf Plot-Ziele.

    Übernimmt zusätzlich das Datei-Drag&Drop (Laden per Ziehen aus dem
    Datei-Explorer) und dessen Leerzustand-Hinweistext - bewusst hier und
    NICHT auf der gesamten `AnalysisView`, damit die Drop-Zone optisch UND
    funktional exakt auf diesen Baum begrenzt bleibt.
    """

    delete_key_pressed = pyqtSignal()
    file_dropped = pyqtSignal(object)  # Path

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setAcceptDrops(True)
        # Drag&Drop-Events landen bei Qt-ItemViews am Viewport, nicht am
        # aeusseren Widget (siehe gleiches Muster bei AssignablePlotWidget
        # weiter unten) - ohne diesen Aufruf kommen extern gezogene Dateien
        # nie bei dragEnterEvent()/dropEvent() an.
        self.viewport().setAcceptDrops(True)

        # Leerzustand-Hinweis als Overlay auf dem Viewport (QTreeWidget
        # kennt anders als z. B. QLineEdit kein natives `placeholderText`)
        # - sichtbar nur solange keine Datei geladen ist, automatisch über
        # die Modell-Signale synchron gehalten statt an jeder einzelnen
        # Stelle in AnalysisView manuell aktualisiert werden zu müssen.
        self._empty_hint_label = QLabel(self.viewport())
        self._empty_hint_label.setWordWrap(True)
        self._empty_hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_hint_label.setStyleSheet("QLabel { color: palette(foreground); }")
        self._empty_hint_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.model().rowsInserted.connect(self._update_empty_hint_visibility)
        self.model().rowsRemoved.connect(self._update_empty_hint_visibility)
        self.model().modelReset.connect(self._update_empty_hint_visibility)
        self._update_empty_hint_visibility()

    def set_empty_hint_text(self, text: str) -> None:
        self._empty_hint_label.setText(text)

    def set_empty_hint_visible(self, visible: bool) -> None:
        self._empty_hint_label.setVisible(visible)

    def retheme_empty_hint(self) -> None:
        """Erzwingt ein Repolish des Hinweistexts nach einem Theme-Wechsel
        (eigenes Stylesheet wird von Qt sonst nicht automatisch neu
        ausgewertet, siehe gleiches Problem bei den Nav-Kacheln in
        `gui/main_window.py::_retheme_nav_icons`)."""
        repolish(self._empty_hint_label)

    def _update_empty_hint_visibility(self, *_args) -> None:
        self._empty_hint_label.setVisible(self.topLevelItemCount() == 0)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        self._empty_hint_label.setGeometry(self.viewport().rect())

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 (Qt API)
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # Ohne diesen Override greift QAbstractItemView's Standard-
        # dragMoveEvent, das externe URL-Drops ablehnt (Modell kennt sie
        # nicht) - sichtbar am Verbotsschild-Cursor waehrend des Ziehens,
        # obwohl dragEnterEvent() oben bereits akzeptiert hat.
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 (Qt API)
        urls = event.mimeData().urls()
        if urls:
            self.file_dropped.emit(Path(urls[0].toLocalFile()))
            return
        super().dropEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.key() == Qt.Key.Key_Delete:
            self.delete_key_pressed.emit()
            return
        super().keyPressEvent(event)

    def startDrag(self, supportedActions) -> None:  # noqa: N802 (Qt API)
        item = self.currentItem()
        if item is None or item.parent() is None:
            return

        file_item = item.parent()
        file_path = file_item.data(0, _ROLE_FILE_PATH)
        channel_name = item.data(0, _ROLE_CHANNEL_NAME)
        if not file_path or not channel_name:
            return

        payload = {
            "file_path": str(file_path),
            "channel_name": str(channel_name),
            "display_name": item.text(0),
        }

        from PyQt6.QtCore import QMimeData

        mime = QMimeData()
        mime.setText(_DRAG_PREFIX + json.dumps(payload))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


class AssignablePlotWidget(pg.PlotWidget):
    """Plot-Ziel, das Kanalzuweisungen per Drag&Drop annimmt."""

    channel_dropped = pyqtSignal(int, str, str)

    def __init__(self, plot_index: int, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._plot_index = plot_index
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.viewport().installEventFilter(self)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        text = event.mimeData().text() if event.mimeData().hasText() else ""
        if text.startswith(_DRAG_PREFIX):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event) -> None:
        text = event.mimeData().text() if event.mimeData().hasText() else ""
        if text.startswith(_DRAG_PREFIX):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        text = event.mimeData().text() if event.mimeData().hasText() else ""
        if not text.startswith(_DRAG_PREFIX):
            event.ignore()
            return
        try:
            payload = json.loads(text[len(_DRAG_PREFIX):])
            file_path = str(payload["file_path"])
            channel_name = str(payload["channel_name"])
        except Exception:
            event.ignore()
            return
        self.channel_dropped.emit(self._plot_index, file_path, channel_name)
        event.acceptProposedAction()

    def eventFilter(self, watched, event) -> bool:
        if watched is self.viewport() and event.type() in {
            QEvent.Type.DragEnter,
            QEvent.Type.DragMove,
            QEvent.Type.Drop,
        }:
            text = event.mimeData().text() if event.mimeData().hasText() else ""
            if not text.startswith(_DRAG_PREFIX):
                event.ignore()
                return True

            if event.type() in {QEvent.Type.DragEnter, QEvent.Type.DragMove}:
                event.acceptProposedAction()
                return True

            # Drop
            try:
                payload = json.loads(text[len(_DRAG_PREFIX):])
                file_path = str(payload["file_path"])
                channel_name = str(payload["channel_name"])
            except Exception:
                event.ignore()
                return True

            self.channel_dropped.emit(self._plot_index, file_path, channel_name)
            event.acceptProposedAction()
            return True

        return super().eventFilter(watched, event)


class _AnalysisFunctionDialog(QDialog):
    """Dialog zur Kanal- und Parameterauswahl für eine Analysefunktion.

    Die Kanalauswahl erfolgt über eine nach Datei gruppierte Baumansicht
    (wie der Dateibrowser rechts in der Analyse-Ansicht, siehe
    `ChannelTreeWidget`) statt einer flachen Dropdown-Liste - bei mehreren
    geladenen Dateien mit jeweils mehreren Kanälen ist das übersichtlicher
    und zeigt auf einen Blick, zu welcher Datei ein Kanal gehört.

    Wird beim Klick auf einen der Analysefunktions-Buttons geöffnet
    (siehe `AnalysisView._on_analysis_function_clicked`).
    """

    def __init__(
        self,
        kind: str,
        channel_groups: list[tuple[str, str, list[tuple[str, str]]]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._kind = kind
        self.setWindowTitle(t(f"analysis_function_dialog_title_{kind}"))

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(t("analysis_select_channel")))

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.itemSelectionChanged.connect(self._update_ok_enabled)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        for file_label, file_path_str, channels in channel_groups:
            file_item = QTreeWidgetItem([file_label])
            file_item.setFlags(file_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            for channel_label, channel_name in channels:
                channel_item = QTreeWidgetItem([channel_label])
                channel_item.setData(0, Qt.ItemDataRole.UserRole, (file_path_str, channel_name))
                file_item.addChild(channel_item)
            self._tree.addTopLevelItem(file_item)
        self._tree.expandAll()
        layout.addWidget(self._tree, stretch=1)

        form = QFormLayout()
        self._cutoff_spin: QDoubleSpinBox | None = None
        self._window_spin: QSpinBox | None = None
        if kind in ("lowpass", "highpass"):
            self._cutoff_spin = PrecisionDoubleSpinBox()
            self._cutoff_spin.setRange(0.01, 1_000_000.0)
            self._cutoff_spin.setValue(10.0)
            self._cutoff_spin.setSuffix(" Hz")
            form.addRow(t("analysis_cutoff_frequency"), self._cutoff_spin)
        elif kind == "smoothing":
            self._window_spin = NoWheelSpinBox()
            self._window_spin.setRange(2, 1_000_000)
            self._window_spin.setValue(10)
            form.addRow(t("analysis_window_size"), self._window_spin)
        if form.rowCount() > 0:
            layout.addLayout(form)

        self._button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_button = self._button_box.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_button.setText(t("analysis_run_button"))
        self._button_box.button(QDialogButtonBox.StandardButton.Cancel).setText(t("cancel"))
        self._ok_button.setEnabled(False)
        self._button_box.accepted.connect(self.accept)
        self._button_box.rejected.connect(self.reject)
        layout.addWidget(self._button_box)

        self.resize(360, 320)

    def _update_ok_enabled(self) -> None:
        items = self._tree.selectedItems()
        has_leaf = bool(items) and items[0].data(0, Qt.ItemDataRole.UserRole) is not None
        self._ok_button.setEnabled(has_leaf)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if item.data(0, Qt.ItemDataRole.UserRole) is not None:
            self._tree.setCurrentItem(item)
            self.accept()

    def selected_channel(self) -> tuple[str, str] | None:
        items = self._tree.selectedItems()
        if not items:
            return None
        return items[0].data(0, Qt.ItemDataRole.UserRole)

    def cutoff_hz(self) -> float:
        return self._cutoff_spin.value() if self._cutoff_spin is not None else 0.0

    def window_size(self) -> int:
        return self._window_spin.value() if self._window_spin is not None else 0


class AnalysisView(QWidget):
    """Ansicht zum Laden und Untersuchen abgeschlossener Messungen."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._loaded_measurement: LoadedMeasurement | None = None
        self._curves: list = []
        self._loaded_measurements: list[tuple[Path, LoadedMeasurement]] = []
        self._channel_assignments: dict[tuple[str, str], int] = {}
        self._plot_x_columns: dict[int, str] = {}
        self._function_buttons: dict[str, QToolButton] = {}
        self._function_category_labels: dict[str, QLabel] = {}
        self._busy_count = 0
        self._loading_paths: set[Path] = set()
        # Referenzen auf laufende Hintergrund-Worker (siehe gui/workers.py)
        # - müssen bis zum Abschluss am Leben gehalten werden, sonst würde
        # Python das QThread-Objekt vorzeitig einsammeln.
        self._background_workers: list[BackgroundWorker] = []

        layout = QVBoxLayout(self)

        # --- Plotbereich + rechter Seitenbereich ---
        content_row = QHBoxLayout()

        # Links: Kategorien/Bedienung (Layout)
        left_panel = QWidget()
        left_panel.setMinimumWidth(240)
        left_panel.setMaximumWidth(320)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)

        self._layout_category_label = QLabel(t("analysis_category_layout"))
        left_layout.addWidget(self._layout_category_label)

        controls_row = QHBoxLayout()
        controls_row.setContentsMargins(0, 0, 0, 0)
        self._layout_label = QLabel(t("analysis_layout"))
        self._layout_combo = QComboBox()
        self._layout_combo.currentIndexChanged.connect(self._on_layout_changed)
        controls_row.addWidget(self._layout_label)
        controls_row.addWidget(self._layout_combo, stretch=1)
        left_layout.addLayout(controls_row)

        for category_key, kinds in _FUNCTION_CATEGORIES:
            category_label = QLabel(t(category_key))
            left_layout.addWidget(category_label)
            self._function_category_labels[category_key] = category_label

            functions_grid = QGridLayout()
            functions_grid.setSpacing(6)
            functions_grid.setContentsMargins(0, 0, 0, 0)
            for idx, kind in enumerate(kinds):
                _kind, icon_fn, label_key, tooltip_key = _FUNCTION_SPECS_BY_KIND[kind]
                button = QToolButton()
                button.setIcon(QIcon(icon_fn(32)))
                button.setIconSize(QSize(32, 32))
                button.setFixedSize(56, 56)
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
                button.setToolTip(f"{t(label_key)} — {t(tooltip_key)}")
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.clicked.connect(lambda checked=False, k=kind: self._on_analysis_function_clicked(k))
                functions_grid.addWidget(button, idx // 2, idx % 2)
                self._function_buttons[kind] = button
            functions_row = QHBoxLayout()
            functions_row.setContentsMargins(0, 0, 0, 0)
            functions_row.addLayout(functions_grid)
            functions_row.addStretch(1)
            left_layout.addLayout(functions_row)

        left_layout.addStretch(1)

        content_row.addWidget(left_panel)

        # Mitte/links: Plotfläche (umschaltbare Raster-Layouts)
        self._plot_area = QWidget()
        self._plot_grid = QGridLayout(self._plot_area)
        self._plot_grid.setContentsMargins(0, 0, 0, 0)
        self._plot_grid.setHorizontalSpacing(8)
        self._plot_grid.setVerticalSpacing(8)
        self._plot_widgets = [
            AssignablePlotWidget(0),
            AssignablePlotWidget(1),
            AssignablePlotWidget(2),
            AssignablePlotWidget(3),
        ]
        for plot_widget in self._plot_widgets:
            plot_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            plot_widget.showGrid(x=True, y=True, alpha=0.3)
            # `units=` NICHT genutzt: PyQtGraph rendert das intern immer in
            # runden Klammern - fest "[s]" im Text selbst statt dessen,
            # damit die Zeiteinheit ueberall konsistent in eckigen Klammern
            # steht (siehe gui/live_view.py). Schriftgroesse explizit wie
            # die Achsentick-Beschriftung (siehe
            # gui/theme.py::axis_tick_point_size) - MUSS vor
            # `style_plot_item()` gesetzt werden, siehe
            # gui/live_view.py::_axis_label_style fuer die Begruendung.
            # `_refresh_plot_axis_labels()`/`retranslate_ui()` setzen den
            # Text spaeter ohne Kwargs neu, das behaelt dieses Style bei.
            plot_widget.setLabel(
                "bottom", f"{t('axis_time')} [s]", **{"font-size": f"{axis_tick_point_size()}pt"}
            )
            plot_widget.addLegend()
            style_plot_container(plot_widget)
            style_plot_item(plot_widget.getPlotItem())
            # OHNE diesen expliziten Aufruf bleibt die ViewBox-Hintergrund-
            # farbe auf PyQtGraph's Default (transparent) - unter reinem
            # Software-Rendering unsichtbar (zeigt die Container-Farbe
            # durch), aber mit `useOpenGL=True` (siehe gui/live_view.py,
            # global für den Prozess aktiv) faellt eine transparente
            # ViewBox auf die OpenGL-Standardfarbe statt auf die
            # Szenenfarbe zurueck - sichtbar als Farbbruch zwischen
            # Plotflaeche und Achsenbeschriftungs-Rand. Live View setzt das
            # deshalb ueberall explizit (siehe `_channel_background_color`).
            plot_widget.getPlotItem().getViewBox().setBackgroundColor(plot_background_color())
            plot_widget.channel_dropped.connect(self._on_channel_dropped_to_plot)
        content_row.addWidget(self._plot_area, stretch=1)

        # Rechts: kategorisierter Seitenbereich (Dateien/Kanäle)
        right_panel = QWidget()
        right_panel.setMinimumWidth(360)
        right_panel.setMaximumWidth(460)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        self._files_category_label = QLabel(t("analysis_category_files"))
        right_layout.addWidget(self._files_category_label)

        self._tree = ChannelTreeWidget()
        self._tree.setHeaderLabels([t("tree_header_name")])
        self._tree.set_empty_hint_text(t("drag_drop_files"))
        self._tree.itemChanged.connect(self._on_tree_item_changed)
        self._tree.setDragEnabled(True)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # Kontextmenü für Top-Level-Dateien (rechtsklick)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self._tree.delete_key_pressed.connect(self._on_delete_key_pressed)
        # Datei per Ziehen aus dem Datei-Explorer laden - Drop-Zone bewusst
        # auf den Baum begrenzt (siehe ChannelTreeWidget-Klassendoc), nicht
        # auf die gesamte Ansicht.
        self._tree.file_dropped.connect(self._load_file)
        self._files_label = QLabel(t("loaded_files_channels"))
        right_layout.addWidget(self._files_label)

        # Such-/Filterfeld: blendet Datei-/Kanalzeilen aus, die weder
        # selbst noch (bei einer Datei) über ein passendes Kind zum
        # Suchtext passen - reine Sichtbarkeitsfilterung, verändert nichts
        # an den geladenen Daten. Wird nach jeder Strukturänderung des
        # Baums automatisch neu angewendet (siehe Model-Signal-Verbindungen
        # unten), damit z. B. neu geladene Dateien einen aktiven Filter
        # nicht umgehen.
        self._tree_search_edit = QLineEdit()
        self._tree_search_edit.setPlaceholderText(t("search_files_placeholder"))
        self._tree_search_edit.setClearButtonEnabled(True)
        self._tree_search_edit.textChanged.connect(self._apply_tree_filter)
        right_layout.addWidget(self._tree_search_edit)
        self._tree.model().rowsInserted.connect(self._apply_tree_filter)
        self._tree.model().rowsRemoved.connect(self._apply_tree_filter)

        right_layout.addWidget(self._tree, stretch=1)
        content_row.addWidget(right_panel)

        layout.addLayout(content_row, stretch=1)

        # Kategorieköpfe visuell wie im Setup-Tab (ohne farbliche Overrides).
        category_font = QFont(self.font())
        if category_font.pointSize() > 0:
            category_font.setPointSize(category_font.pointSize() + 1)
        category_font.setBold(True)
        self._layout_category_label.setFont(category_font)
        for category_label in self._function_category_labels.values():
            category_label.setFont(category_font)
        self._files_category_label.setFont(category_font)

        self._populate_layout_combo()
        self._apply_plot_layout()
        connect_language_changed(self.retranslate_ui)
        connect_theme_changed(self.retheme_plots)

    def retheme_plots(self) -> None:
        """Färbt Plot-Hintergrund/-Achsen nach einem Theme-Wechsel um.

        PyQtGraph-Widgets folgen der `QApplication`-Palette nicht
        automatisch (siehe `gui/theme.py`).
        """
        for plot_widget in self._plot_widgets:
            style_plot_container(plot_widget)
            style_plot_item(plot_widget.getPlotItem())
            plot_widget.getPlotItem().getViewBox().setBackgroundColor(plot_background_color())

        # Analysefunktions-Icons folgen der WindowText-Farbe (siehe
        # gui/theme.py::nav_icon_color) und müssen daher nach einem
        # Theme-Wechsel neu gezeichnet werden.
        for kind, icon_fn, _label_key, _tooltip_key in _FUNCTION_SPECS:
            button = self._function_buttons.get(kind)
            if button is not None:
                button.setIcon(QIcon(icon_fn(32)))

        self._tree.retheme_empty_hint()

    def retranslate_ui(self) -> None:
        """Aktualisiert alle statischen Texte nach einem Sprachwechsel."""
        self._layout_category_label.setText(t("analysis_category_layout"))
        for category_key, category_label in self._function_category_labels.items():
            category_label.setText(t(category_key))
        self._files_category_label.setText(t("analysis_category_files"))
        self._tree.set_empty_hint_text(t("drag_drop_files"))
        self._tree_search_edit.setPlaceholderText(t("search_files_placeholder"))
        self._files_label.setText(t("loaded_files_channels"))
        self._layout_label.setText(t("analysis_layout"))
        self._populate_layout_combo()
        self._tree.setHeaderLabels([t("tree_header_name")])
        for kind, _icon_fn, label_key, tooltip_key in _FUNCTION_SPECS:
            button = self._function_buttons.get(kind)
            if button is not None:
                button.setToolTip(f"{t(label_key)} — {t(tooltip_key)}")
        self._refresh_plot_axis_labels()
        self._update_files_label_count()
        self._apply_tree_filter()

    def _refresh_plot_axis_labels(self) -> None:
        """Setzt die x-Achsen-Beschriftung je Plot passend zu den dort
        aktuell dargestellten Kanälen (Zeit- oder Frequenzachse, siehe
        `_update_plot`)."""
        for plot_index, plot_widget in enumerate(self._plot_widgets):
            if self._plot_x_columns.get(plot_index) == _FREQUENCY_X_COLUMN:
                plot_widget.setLabel("bottom", t("axis_frequency"), units="Hz")
            else:
                # `units=` NICHT genutzt - Zeiteinheit ueberall konsistent
                # in eckigen Klammern (siehe gui/live_view.py).
                plot_widget.setLabel("bottom", f"{t('axis_time')} [s]")

    def _update_files_label_count(self) -> None:
        """Aktualisiert die Datei/Kanal-Überschrift inkl. Anzahl geladener Dateien."""
        if self._loaded_measurements:
            self._files_label.setText(
                f"{t('loaded_files_channels')} - {t('files_loaded_count', count=len(self._loaded_measurements))}"
            )
        else:
            self._files_label.setText(t("loaded_files_channels"))

    def _apply_tree_filter(self, *_args) -> None:
        """Blendet Datei-/Kanalzeilen aus, die nicht zum Suchtext passen
        (Groß-/Kleinschreibung ignoriert).

        Eine Datei bleibt sichtbar, wenn IHR Name passt (dann auch alle
        ihre Kinder, ungefiltert) ODER mindestens ein Kanal-/
        Ergebnis-Kind passt (dann nur die passenden Kinder). `*_args`
        nimmt die von `QLineEdit.textChanged`
        (str) UND den Model-Signalen `rowsInserted`/`rowsRemoved`
        (QModelIndex, int, int) übergebenen, hier ungenutzten Argumente
        gleichermaßen entgegen - der Suchtext wird immer frisch aus
        `self._tree_search_edit` gelesen.
        """
        query = self._tree_search_edit.text().strip().lower()
        for i in range(self._tree.topLevelItemCount()):
            file_item = self._tree.topLevelItem(i)
            file_matches = not query or query in file_item.text(0).lower()
            any_child_visible = False
            for j in range(file_item.childCount()):
                child = file_item.child(j)
                child_matches = not query or query in child.text(0).lower()
                show_child = file_matches or child_matches
                child.setHidden(not show_child)
                if show_child:
                    any_child_visible = True
            show_file = file_matches or any_child_visible
            file_item.setHidden(not show_file)
            if query and show_file:
                file_item.setExpanded(True)

        visible_files = any(
            not self._tree.topLevelItem(index).isHidden()
            for index in range(self._tree.topLevelItemCount())
        )
        if query and self._tree.topLevelItemCount() > 0 and not visible_files:
            self._tree.set_empty_hint_text(t("search_no_results"))
            self._tree.set_empty_hint_visible(True)
        else:
            self._tree.set_empty_hint_text(t("drag_drop_files"))
            self._tree.set_empty_hint_visible(self._tree.topLevelItemCount() == 0)

    # ------------------------------------------------------------------ #
    # Öffentliche API (von gui/main_window.py aufgerufen)
    # ------------------------------------------------------------------ #

    def prompt_and_load_file(self) -> bool:
        """Öffnet einen Datei-Dialog zum Laden einer Messung und startet
        bei Auswahl den (Hintergrund-)Ladevorgang, siehe `_load_file`.

        Wird vom "Messung laden..."-Menüeintrag in `gui/main_window.py`
        aufgerufen (ehemals ein Button direkt in dieser Ansicht - jetzt
        aus jeder Ansicht heraus erreichbar). Gibt True zurück, wenn eine
        Datei ausgewählt wurde (main_window navigiert dann zum
        Analyse-Tab), False bei Abbruch des Dialogs.
        """
        filename, _ = QFileDialog.getOpenFileName(
            self,
            t("load_measurement_dialog_title"),
            "",
            t("measurement_files_filter"),
        )
        if not filename:
            return False
        self._load_file(Path(filename))
        return True

    # ------------------------------------------------------------------ #
    # Interna
    # ------------------------------------------------------------------ #

    def _begin_busy(self) -> None:
        """Sperrt die Bedienelemente, deren Aktionen im Hintergrund laufen
        (Datei laden, Analysefunktionen, siehe `gui/workers.py`), und
        zeigt einen Wartecursor - verhindert, dass mehrere solche
        Operationen gleichzeitig gestartet werden, während der Nutzer
        über den Fortschritt im Bilde bleibt.
        """
        self._busy_count += 1
        if self._busy_count == 1:
            for button in self._function_buttons.values():
                button.setEnabled(False)
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

    def _end_busy(self) -> None:
        self._busy_count = max(0, self._busy_count - 1)
        if self._busy_count == 0:
            for button in self._function_buttons.values():
                button.setEnabled(True)
            QApplication.restoreOverrideCursor()

    def _forget_background_worker(self, worker: BackgroundWorker) -> None:
        """Entfernt eine abgeschlossene `BackgroundWorker`-Referenz, damit
        `_background_workers` bei langer Programmlaufzeit nicht unbegrenzt
        wächst."""
        if worker in self._background_workers:
            self._background_workers.remove(worker)
        worker.deleteLater()

    def _on_tree_context_menu(self, point) -> None:
        """Zeigt ein Kontextmenü zum Entfernen der Datei bzw. eines einzelnen
        Kanals - für Analyseergebnis-Kanäle zusätzlich zum Speichern des
        Ergebnisses."""
        item = self._tree.itemAt(point)
        if item is None:
            return

        menu = QMenu(self)
        if item.parent() is None:
            # Top-Level-Item (Datei/Messung)
            remove_action = menu.addAction(t("remove_file_action"))
            remove_action.triggered.connect(lambda: self._remove_file_item(item))
        else:
            is_result = bool(item.data(0, _ROLE_IS_RESULT))
            if is_result:
                save_csv_action = menu.addAction(t("save_as_csv_action"))
                save_csv_action.triggered.connect(lambda: self._save_result_channel(item, "csv"))
                save_parquet_action = menu.addAction(t("save_as_parquet_action"))
                save_parquet_action.triggered.connect(lambda: self._save_result_channel(item, "parquet"))
                menu.addSeparator()
            remove_action = menu.addAction(
                t("remove_result_action") if is_result else t("remove_channel_action")
            )
            remove_action.triggered.connect(lambda: self._remove_channel_item(item))
        menu.exec(self._tree.viewport().mapToGlobal(point))

    def _on_delete_key_pressed(self) -> None:
        """Löscht den aktuell ausgewählten Kanal bzw. die ausgewählte Messung
        aus dem Dateibrowser (Entfernen-Taste, siehe `ChannelTreeWidget`)."""
        item = self._tree.currentItem()
        if item is None:
            return
        if item.parent() is None:
            self._remove_file_item(item)
        else:
            self._remove_channel_item(item)

    def _confirm_delete(self, body: str) -> bool:
        """Fragt vor einer Lösch-Aktion (Datei/Kanal, per Kontextmenü oder
        Entfernen-Taste) explizit nach Bestätigung."""
        return confirm_delete(self, body)

    def _remove_channel_item(self, item: QTreeWidgetItem) -> None:
        """Entfernt einen einzelnen Kanal (regulär oder Analyseergebnis) aus
        dem Baum, ohne die gesamte Quelldatei zu entfernen."""
        if not self._confirm_delete(t("confirm_remove_channel_body", name=item.text(0))):
            return
        file_item = item.parent()
        if file_item is None:
            return
        file_path_str = str(file_item.data(0, Qt.ItemDataRole.UserRole) or "")
        channel_name = str(item.data(0, _ROLE_CHANNEL_NAME) or "")
        self._channel_assignments.pop((file_path_str, channel_name), None)
        file_item.removeChild(item)
        self._update_plot()

    def _save_result_channel(self, item: QTreeWidgetItem, fmt: str) -> None:
        """Speichert einen Analyseergebnis-Kanal als eigenständige CSV-/Parquet-Datei."""
        measurement = item.data(0, _ROLE_MEASUREMENT)
        if measurement is None:
            return
        channel_name = str(item.data(0, _ROLE_CHANNEL_NAME) or item.text(0))

        if fmt == "csv":
            file_filter = t("save_result_csv_filter")
            suffix = ".csv"
        else:
            file_filter = t("save_result_parquet_filter")
            suffix = ".parquet"

        filename, _ = QFileDialog.getSaveFileName(
            self, t("save_result_dialog_title"), f"{channel_name}{suffix}", file_filter
        )
        if not filename:
            return
        path = Path(filename)
        if path.suffix.lower() != suffix:
            path = path.with_suffix(suffix)

        try:
            if fmt == "csv":
                measurement.data.to_csv(path, index=False)
            else:
                measurement.data.to_parquet(path)
        except Exception as exc:
            QMessageBox.critical(
                self, t("save_result_error_title"), t("save_result_error_body", error=str(exc))
            )
            return

        QMessageBox.information(
            self, t("save_result_dialog_title"), t("save_result_success", filename=path.name)
        )

    def _remove_file_item(self, top: QTreeWidgetItem) -> None:
        if not self._confirm_delete(t("confirm_remove_file_body", name=top.text(0))):
            return
        file_path_str = top.data(0, Qt.ItemDataRole.UserRole)
        # Entferne Eintrag aus geladenen Messungen
        self._loaded_measurements = [pair for pair in self._loaded_measurements if str(pair[0]) != str(file_path_str) and pair[0].name != str(file_path_str)]
        # Entferne auch ggf. bestehende Plot-Zuordnungen dieser Datei.
        self._channel_assignments = {
            key: value
            for key, value in self._channel_assignments.items()
            if key[0] != str(file_path_str)
        }
        # Entferne Top-Level-Item aus Tree
        idx = self._tree.indexOfTopLevelItem(top)
        if idx != -1:
            self._tree.takeTopLevelItem(idx)
        self._update_files_label_count()
        self._update_plot()

    def _load_file(self, path: Path) -> None:
        if path.suffix.lower() not in _SUPPORTED_SUFFIXES:
            QMessageBox.warning(
                self,
                t("unsupported_format_title"),
                t("unsupported_format_body", suffix=path.suffix),
            )
            return

        # Verhindere doppelte Einträge (billige Prüfung, bewusst noch vor
        # dem eigentlichen - potenziell langsamen - Laden im Hintergrund).
        if any(p == path for p, _ in self._loaded_measurements):
            QMessageBox.information(
                self, t("already_loaded_title"), t("already_loaded_body", filename=path.name)
            )
            return
        if path in self._loading_paths:
            QMessageBox.information(
                self, t("already_loaded_title"), t("already_loaded_body", filename=path.name)
            )
            return

        # Laden selbst (pd.read_parquet/pd.read_csv) läuft im Hintergrund,
        # da das bei großen Messdateien den GUI-Thread spürbar blockieren
        # würde (siehe gui/workers.py::BackgroundWorker).
        metadata_path = infer_metadata_path(path)
        self._loading_paths.add(path)
        self._begin_busy()
        worker = BackgroundWorker(load_measurement_file, path, metadata_path)
        worker.succeeded.connect(lambda measurement: self._on_file_loaded(path, measurement))
        worker.failed.connect(lambda message: self._on_file_load_failed(path, message))
        worker.finished.connect(lambda: self._forget_background_worker(worker))
        self._background_workers.append(worker)
        worker.start()

    def _on_file_load_failed(self, path: Path, message: str) -> None:
        self._loading_paths.discard(path)
        self._end_busy()
        QMessageBox.critical(self, t("load_error_title"), message)

    def _on_file_loaded(self, path: Path, measurement: LoadedMeasurement) -> None:
        self._loading_paths.discard(path)
        self._end_busy()

        # Zwischen Start des Hintergrund-Ladens und hier könnte dieselbe
        # Datei bereits über einen zweiten, parallel gestarteten Ladevorgang
        # hinzugefügt worden sein - erneut prüfen statt blind einzufügen.
        if any(p == path for p, _ in self._loaded_measurements):
            QMessageBox.information(
                self, t("already_loaded_title"), t("already_loaded_body", filename=path.name)
            )
            return

        self._loaded_measurements.append((path, measurement))
        # Erzeuge Tree-Einträge: Datei -> Module -> Kanäle
        file_item = QTreeWidgetItem(self._tree, [path.name])  # top-level
        file_item.setFlags(file_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        file_item.setCheckState(0, Qt.CheckState.Unchecked)
        file_item.setData(0, Qt.ItemDataRole.UserRole, str(path))

        # Füge direkte Kanal-Einträge unter der Datei hinzu (falls Metadaten vorhanden)
        if measurement.channels:
            for ch in measurement.channels:
                ch_item = QTreeWidgetItem(file_item, [ch.display_name or ch.hardware_channel])
                ch_item.setFlags(ch_item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
                ch_item.setCheckState(0, Qt.CheckState.Unchecked)
                ch_item.setData(0, Qt.ItemDataRole.UserRole, ch)
                ch_item.setData(0, _ROLE_MEASUREMENT, measurement)
                column_name = self._resolve_column_name(measurement, ch_item)
                if column_name is not None:
                    ch_item.setData(0, _ROLE_CHANNEL_NAME, column_name)
        else:
            # Fallback: benutze DataFrame-Spalten als Kanäle (keine Metadaten)
            inferred = measurement.channel_names
            for name in inferred:
                if name == "time_s":
                    continue
                ch_item = QTreeWidgetItem(file_item, [name])
                ch_item.setFlags(ch_item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
                ch_item.setCheckState(0, Qt.CheckState.Unchecked)
                # Leichtgewichtiger Channel-Platzhalter (keine Metadaten vorhanden)
                placeholder = Channel(hardware_channel=name, display_name=name)
                ch_item.setData(0, Qt.ItemDataRole.UserRole, placeholder)
                ch_item.setData(0, _ROLE_CHANNEL_NAME, name)
                ch_item.setData(0, _ROLE_MEASUREMENT, measurement)

        self._tree.expandItem(file_item)
        self._update_files_label_count()
        self._update_plot()

    def _on_tree_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        # When a parent is checked/unchecked, propagate to children
        state = item.checkState(0)
        for i in range(item.childCount()):
            child = item.child(i)
            child.setCheckState(0, state)
        self._update_plot()

    def _update_plot(self) -> None:
        for plot_widget in self._plot_widgets:
            plot_widget.clear()
        self._curves = []
        self._plot_x_columns = {}
        if not self._loaded_measurements:
            self._refresh_plot_axis_labels()
            return
        active_indices = self._active_plot_indices()
        if not active_indices:
            self._refresh_plot_axis_labels()
            return

        # Iterate over tree: top-level items are files
        for file_idx in range(self._tree.topLevelItemCount()):
            file_item = self._tree.topLevelItem(file_idx)
            file_path_str = file_item.data(0, Qt.ItemDataRole.UserRole)
            if not file_path_str:
                continue
            # Find measurement in loaded list (Fallback für Kanäle ohne
            # eigene _ROLE_MEASUREMENT-Zuweisung)
            matched = [m for p, m in self._loaded_measurements if str(p) == file_path_str or p.name == file_path_str]
            if not matched:
                continue
            default_measurement = matched[0]

            # If file unchecked, skip
            if file_item.checkState(0) != Qt.CheckState.Checked:
                continue

            # Iterate channel items directly under file_item
            for ch_i in range(file_item.childCount()):
                ch_item = file_item.child(ch_i)
                if ch_item.checkState(0) != Qt.CheckState.Checked:
                    continue

                # Jeder Kanal trägt einen Verweis auf sein eigenes
                # LoadedMeasurement (siehe _load_file/_add_result_channel) -
                # bei Analyseergebnissen (z. B. FFT) weicht dieses vom
                # Messungs-DataFrame der Quelldatei ab (andere x-Achse).
                meas = ch_item.data(0, _ROLE_MEASUREMENT) or default_measurement
                data = meas.data
                x_column = meas.x_column if meas.x_column in data.columns else None
                if x_column is None:
                    continue
                x_values = data[x_column].to_numpy()

                data_obj = ch_item.data(0, Qt.ItemDataRole.UserRole)
                display_label = ch_item.text(0)
                chan_name = None
                # Prefer the sanitized display_name (what StorageWriter used as column)
                if hasattr(data_obj, "display_name") and data_obj.display_name:
                    candidate = data_obj.display_name.strip()
                    if candidate in data.columns:
                        chan_name = candidate
                        display_label = candidate
                if chan_name is None and hasattr(data_obj, "hardware_channel"):
                    chan_name = getattr(data_obj, "hardware_channel", None)
                if chan_name is None:
                    chan_name = ch_item.text(0)

                if chan_name not in data.columns:
                    continue

                assignment_key = (str(file_path_str), str(chan_name))
                # Nur bereits zugewiesene Kanaele plotten (Drag&Drop).
                # Der Haken blendet danach nur ein/aus.
                if assignment_key not in self._channel_assignments:
                    continue
                plot_index = self._channel_assignments.get(assignment_key, 0)
                if plot_index not in active_indices:
                    plot_index = active_indices[0]
                plot_index = min(plot_index, len(self._plot_widgets) - 1)
                plot_widget = self._plot_widgets[plot_index]
                self._plot_x_columns.setdefault(plot_index, x_column)

                color = pg.intColor(file_idx * 8 + ch_i, hues=max(self._tree.topLevelItemCount() * 8, 1))
                curve = plot_widget.plot(
                    x_values,
                    data[chan_name].to_numpy(),
                    pen=pg.mkPen(color=color, width=1.2),
                    name=f"{file_item.text(0)} - {display_label}",
                )
                # Min/Max-Dezimierung pro Pixel (siehe gui/live_view.py:44-53) -
                # haelt Zoom/Pan bei sehr grossen Datensaetzen fluessig, ohne
                # kurze Spitzen/Ausreisser zu verlieren (method="peak" statt
                # "mean").
                curve.setDownsampling(auto=True, method="peak")
                curve.setClipToView(True)
                self._curves.append(curve)

        self._refresh_plot_axis_labels()

    # ------------------------------------------------------------------ #
    # Analysefunktionen (FFT, Tief-/Hochpass, Glättung)
    # ------------------------------------------------------------------ #

    def _on_analysis_function_clicked(self, kind: str) -> None:
        """Öffnet den Kanal-/Parameter-Dialog für eine Analysefunktion und
        legt das Ergebnis als neuen Kanal unter der Quelldatei ab."""
        channel_options = self._collect_channel_options()
        if not channel_options:
            QMessageBox.information(
                self, t("analysis_no_channels_title"), t("analysis_no_channels_available")
            )
            return

        dialog = _AnalysisFunctionDialog(kind, channel_options, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selection = dialog.selected_channel()
        if selection is None:
            return
        file_path_str, channel_name = selection

        file_item, ch_item = self._find_channel_item(file_path_str, channel_name)
        if file_item is None or ch_item is None:
            return
        measurement = ch_item.data(0, _ROLE_MEASUREMENT)
        if measurement is None:
            return

        # Die eigentliche Berechnung (compute_fft/apply_filter/
        # apply_smoothing) läuft im Hintergrund (siehe
        # gui/workers.py::BackgroundWorker) - bei langen Messungen (z. B.
        # 100 kHz über mehrere Minuten) kann das sonst den GUI-Thread für
        # spürbare Zeit blockieren. Nur die schnellen Vorbereitungsschritte
        # (Abtastrate ermitteln, x-Achse extrahieren) bleiben synchron.
        if kind == "fft":
            sample_rate_hz = self._resolve_sample_rate(measurement)
            if sample_rate_hz is None:
                QMessageBox.warning(
                    self, t("analysis_error_title"), t("analysis_no_sample_rate_body")
                )
                return
            analysis_data, sample_rate_hz = self._prepare_channel_for_rate_aware_analysis(
                measurement, channel_name, sample_rate_hz
            )
            self._run_analysis_in_background(
                compute_fft,
                (analysis_data, channel_name, sample_rate_hz),
                {},
                lambda result: self._finish_result(
                    file_item, channel_name, t("analysis_fft_result_suffix"),
                    _FREQUENCY_X_COLUMN, result[0], result[1],
                ),
            )
        elif kind in ("lowpass", "highpass"):
            sample_rate_hz = self._resolve_sample_rate(measurement)
            if sample_rate_hz is None:
                QMessageBox.warning(
                    self, t("analysis_error_title"), t("analysis_no_sample_rate_body")
                )
                return
            analysis_data, sample_rate_hz = self._prepare_channel_for_rate_aware_analysis(
                measurement, channel_name, sample_rate_hz
            )
            suffix_key = (
                "analysis_lowpass_result_suffix" if kind == "lowpass"
                else "analysis_highpass_result_suffix"
            )
            x_column = measurement.x_column
            x_values = analysis_data[x_column].to_numpy()
            self._run_analysis_in_background(
                apply_filter,
                (analysis_data, channel_name, sample_rate_hz, dialog.cutoff_hz()),
                {"kind": kind},
                lambda filtered: self._finish_result(
                    file_item, channel_name, t(suffix_key), x_column, x_values, filtered,
                ),
            )
        elif kind == "smoothing":
            x_column = measurement.x_column
            x_values = measurement.data[x_column].to_numpy()
            self._run_analysis_in_background(
                apply_smoothing,
                (measurement.data, channel_name, dialog.window_size()),
                {},
                lambda smoothed: self._finish_result(
                    file_item, channel_name, t("analysis_smoothing_result_suffix"),
                    x_column, x_values, smoothed,
                ),
            )

    def _run_analysis_in_background(
        self, fn, args: tuple, kwargs: dict, on_success
    ) -> None:
        """Führt eine Analysefunktion im Hintergrund aus und ruft bei
        Erfolg `on_success(ergebnis)` im GUI-Thread auf (siehe
        `_finish_result`-Aufrufe in `_on_analysis_function_clicked`)."""
        self._begin_busy()
        worker = BackgroundWorker(fn, *args, **kwargs)
        worker.succeeded.connect(lambda result: self._on_analysis_succeeded(on_success, result))
        worker.failed.connect(self._on_analysis_failed)
        worker.finished.connect(lambda: self._forget_background_worker(worker))
        self._background_workers.append(worker)
        worker.start()

    def _on_analysis_succeeded(self, on_success, result) -> None:
        self._end_busy()
        try:
            on_success(result)
        except Exception as exc:
            logger.warning("Analyseergebnis konnte nicht übernommen werden: %s", exc)
            QMessageBox.critical(
                self, t("analysis_error_title"), t("analysis_error_body", error=str(exc))
            )

    def _on_analysis_failed(self, message: str) -> None:
        self._end_busy()
        logger.warning("Analysefunktion fehlgeschlagen: %s", message)
        QMessageBox.critical(
            self, t("analysis_error_title"), t("analysis_error_body", error=message)
        )

    def _collect_channel_options(self) -> list[tuple[str, str, list[tuple[str, str]]]]:
        """Alle aktuell im Baum vorhandenen Dateien mit ihren Kanälen
        (regulär und Analyseergebnisse), gruppiert nach Datei - als
        `(Dateiname, Dateipfad, [(Kanal-Anzeigename, Kanalname), ...])`
        für die Baumauswahl im Analysefunktions-Dialog (siehe
        `_AnalysisFunctionDialog`). Dateien ohne auswählbare Kanäle werden
        ausgelassen, statt als leere Gruppe angezeigt zu werden."""
        groups: list[tuple[str, str, list[tuple[str, str]]]] = []
        for file_idx in range(self._tree.topLevelItemCount()):
            file_item = self._tree.topLevelItem(file_idx)
            file_path_str = str(file_item.data(0, Qt.ItemDataRole.UserRole) or "")
            if not file_path_str:
                continue
            channels: list[tuple[str, str]] = []
            for ch_i in range(file_item.childCount()):
                ch_item = file_item.child(ch_i)
                channel_name = ch_item.data(0, _ROLE_CHANNEL_NAME)
                if not channel_name:
                    continue
                channels.append((ch_item.text(0), str(channel_name)))
            if channels:
                groups.append((file_item.text(0), file_path_str, channels))
        return groups

    def _find_channel_item(
        self, file_path_str: str, channel_name: str
    ) -> tuple[QTreeWidgetItem | None, QTreeWidgetItem | None]:
        for file_idx in range(self._tree.topLevelItemCount()):
            file_item = self._tree.topLevelItem(file_idx)
            if str(file_item.data(0, Qt.ItemDataRole.UserRole) or "") != file_path_str:
                continue
            for ch_i in range(file_item.childCount()):
                ch_item = file_item.child(ch_i)
                if str(ch_item.data(0, _ROLE_CHANNEL_NAME) or "") == channel_name:
                    return file_item, ch_item
        return None, None

    @staticmethod
    def _resolve_sample_rate(measurement: LoadedMeasurement) -> float | None:
        """Ermittelt die Abtastrate eines Kanals: bevorzugt aus den
        Metadaten (`sample_rate_hz`), sonst geschätzt aus dem Median-Abstand
        der x-Achsen-Werte (z. B. bei Dateien ohne Metadaten-JSON)."""
        sample_rate = measurement.metadata.get("sample_rate_hz") if measurement.metadata else None
        if sample_rate:
            try:
                return float(sample_rate)
            except (TypeError, ValueError):
                pass

        x_column = measurement.x_column
        if x_column in measurement.data.columns and len(measurement.data) > 1:
            x_values = measurement.data[x_column].to_numpy(dtype=float)
            diffs = np.diff(x_values)
            diffs = diffs[diffs > 0]
            if len(diffs) > 0:
                return float(1.0 / np.median(diffs))
        return None

    @staticmethod
    def _resolve_native_sample_rate(measurement: LoadedMeasurement, channel_name: str) -> float | None:
        """Native Abtastrate eines einzelnen Kanals aus den Metadaten
        (Schlüssel `native_sample_rate_hz`, siehe
        `data/metadata.py::build_measurement_metadata`), falls vorhanden.

        Kann von der Datei-Tick-Rate (`_resolve_sample_rate`) abweichen,
        wenn der Kanal per `core/rate_merge.py::RateMerger` forward-
        gefüllt wurde (aktuell nur beim NI9210 möglich). Sucht bewusst im
        ROHEN Metadaten-Dictionary (`measurement.metadata["channels"]`),
        NICHT über `measurement.channels`/`Channel.from_dict` - letzteres
        kennt `native_sample_rate_hz` nicht als eigenes Dataclass-Feld und
        würde es beim Rekonstruieren stillschweigend verlieren.
        """
        if not measurement.metadata:
            return None
        for channel_meta in measurement.metadata.get("channels", []):
            display_name = str(channel_meta.get("display_name", "")).strip()
            hardware_channel = str(channel_meta.get("hardware_channel", "")).strip()
            if channel_name not in (display_name, hardware_channel):
                continue
            native_rate = channel_meta.get("native_sample_rate_hz")
            if native_rate is None:
                return None
            try:
                return float(native_rate)
            except (TypeError, ValueError):
                return None
        return None

    def _prepare_channel_for_rate_aware_analysis(
        self, measurement: LoadedMeasurement, channel_name: str, tick_rate_hz: float
    ) -> tuple[pd.DataFrame, float]:
        """Liefert die für FFT/Filter zu verwendenden Daten + Abtastrate.

        Bei einem forward-gefüllten Kanal (native Rate erkennbar unter
        der Datei-Tick-Rate, siehe `_resolve_native_sample_rate`) werden
        zuerst die Wiederholungswerte entfernt
        (`analysis.basic_analysis.native_samples`) und mit der echten
        nativen Rate gerechnet - sonst würde eine Zero-Order-Hold-
        Treppenstufe (siehe `core/rate_merge.py`) ein falsches, sinc-
        förmiges Spektrum-Artefakt vortäuschen. Bei allen anderen Kanälen
        (der Regelfall, native Rate == Tick-Rate) exakt unverändertes
        Verhalten - `measurement.data`/`tick_rate_hz` werden unangetastet
        zurückgegeben.

        `* 0.99`-Toleranz deckt Rundung beim Speichern/Runden der Raten
        ab, ohne eine minimal abweichende, aber eigentlich identische
        Rate fälschlich als "forward-gefüllt" zu behandeln.
        """
        native_rate_hz = self._resolve_native_sample_rate(measurement, channel_name)
        if native_rate_hz is not None and native_rate_hz < tick_rate_hz * 0.99:
            return native_samples(measurement.data, channel_name), native_rate_hz
        return measurement.data, tick_rate_hz

    @staticmethod
    def _make_unique_result_name(file_item: QTreeWidgetItem, channel_name: str, suffix: str) -> str:
        base = f"{channel_name}_{suffix}"
        existing = {
            str(file_item.child(ch_i).data(0, _ROLE_CHANNEL_NAME) or "")
            for ch_i in range(file_item.childCount())
        }
        candidate = base
        counter = 2
        while candidate in existing:
            candidate = f"{base}_{counter}"
            counter += 1
        return candidate

    def _finish_result(
        self,
        file_item: QTreeWidgetItem,
        channel_name: str,
        suffix: str,
        x_column: str,
        x_values: np.ndarray,
        values: np.ndarray,
    ) -> None:
        result_name = self._make_unique_result_name(file_item, channel_name, suffix)
        df = pd.DataFrame({x_column: x_values, result_name: values})
        result_measurement = LoadedMeasurement(
            data=df,
            channels=[Channel(hardware_channel=result_name, display_name=result_name)],
            metadata={},
            source_path=Path(f"{file_item.text(0)} / {result_name}"),
            x_column=x_column,
        )
        self._add_result_channel(file_item, result_measurement, result_name)

    def _add_result_channel(
        self, file_item: QTreeWidgetItem, result_measurement: LoadedMeasurement, result_name: str
    ) -> None:
        ch_item = QTreeWidgetItem(file_item, [result_name])
        ch_item.setFlags(ch_item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
        ch_item.setData(0, Qt.ItemDataRole.UserRole, result_measurement.channels[0])
        ch_item.setData(0, _ROLE_CHANNEL_NAME, result_name)
        ch_item.setData(0, _ROLE_MEASUREMENT, result_measurement)
        ch_item.setData(0, _ROLE_IS_RESULT, True)

        file_path_str = str(file_item.data(0, Qt.ItemDataRole.UserRole) or "")
        active_indices = self._active_plot_indices()
        if active_indices and file_path_str:
            self._channel_assignments.setdefault((file_path_str, result_name), active_indices[0])

        self._tree.blockSignals(True)
        ch_item.setCheckState(0, Qt.CheckState.Checked)
        file_item.setCheckState(0, Qt.CheckState.Checked)
        self._tree.blockSignals(False)
        self._tree.expandItem(file_item)

        self._update_plot()

    def _populate_layout_combo(self) -> None:
        current = self._layout_combo.currentData()
        self._layout_combo.blockSignals(True)
        self._layout_combo.clear()
        self._layout_combo.addItem(t("analysis_layout_single"), "single")
        self._layout_combo.addItem(t("analysis_layout_split"), "split")
        self._layout_combo.addItem(t("analysis_layout_three"), "three")
        self._layout_combo.addItem(t("analysis_layout_four"), "four")
        self._layout_combo.addItem(t("analysis_layout_four_square"), "four_square")
        index = self._layout_combo.findData(current)
        self._layout_combo.setCurrentIndex(index if index >= 0 else 0)
        self._layout_combo.blockSignals(False)

    def _on_layout_changed(self) -> None:
        self._apply_plot_layout()
        self._update_plot()

    def _on_channel_dropped_to_plot(self, plot_index: int, file_path: str, channel_name: str) -> None:
        active_indices = self._active_plot_indices()
        if not active_indices:
            return
        assigned_index = plot_index if plot_index in active_indices else active_indices[0]
        self._channel_assignments[(file_path, channel_name)] = assigned_index

        # Nach Zuordnung sichtbar schalten: Datei + Kanal auf "checked" setzen.
        for file_idx in range(self._tree.topLevelItemCount()):
            file_item = self._tree.topLevelItem(file_idx)
            if str(file_item.data(0, _ROLE_FILE_PATH)) != file_path:
                continue

            self._tree.blockSignals(True)
            file_item.setCheckState(0, Qt.CheckState.Checked)
            for ch_i in range(file_item.childCount()):
                ch_item = file_item.child(ch_i)
                if str(ch_item.data(0, _ROLE_CHANNEL_NAME)) == channel_name:
                    ch_item.setCheckState(0, Qt.CheckState.Checked)
                    break
            self._tree.blockSignals(False)
            break

        self._update_plot()

    def _active_plot_indices(self) -> list[int]:
        layout_id = self._layout_combo.currentData() or "single"
        return [spec[0] for spec in _LAYOUT_SPECS.get(layout_id, _LAYOUT_SPECS["single"])]

    def _apply_plot_layout(self) -> None:
        while self._plot_grid.count():
            item = self._plot_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(self._plot_area)

        for plot_widget in self._plot_widgets:
            plot_widget.setVisible(False)

        layout_id = self._layout_combo.currentData() or "single"
        specs = _LAYOUT_SPECS.get(layout_id, _LAYOUT_SPECS["single"])

        for plot_index, row, col, row_span, col_span in specs:
            plot_widget = self._plot_widgets[plot_index]
            plot_widget.setVisible(True)
            self._plot_grid.addWidget(plot_widget, row, col, row_span, col_span)

        # Nur tatsächlich genutzte Zeilen/Spalten stretchen, damit Single/
        # gestapelte Layouts die volle Breite/Höhe ausfüllen.
        used_rows: set[int] = set()
        used_cols: set[int] = set()
        for _plot_index, row, col, row_span, col_span in specs:
            used_rows.update(range(row, row + row_span))
            used_cols.update(range(col, col + col_span))
        for idx in range(4):
            self._plot_grid.setRowStretch(idx, 1 if idx in used_rows else 0)
            self._plot_grid.setColumnStretch(idx, 1 if idx in used_cols else 0)

    @staticmethod
    def _resolve_column_name(measurement: LoadedMeasurement, ch_item: QTreeWidgetItem) -> str | None:
        data_obj = ch_item.data(0, Qt.ItemDataRole.UserRole)
        if hasattr(data_obj, "display_name") and data_obj.display_name:
            candidate = str(data_obj.display_name).strip()
            if candidate in measurement.data.columns:
                return candidate
        if hasattr(data_obj, "hardware_channel"):
            candidate = str(getattr(data_obj, "hardware_channel", "")).strip()
            if candidate in measurement.data.columns:
                return candidate
        candidate = ch_item.text(0)
        if candidate in measurement.data.columns:
            return candidate
        return None
