"""
gui/analysis_view.py

Analyse-Ansicht (Version 1).

Funktionen (siehe Vorgabe):
    * Drag & Drop von Messdateien (.parquet, .csv)
    * Metadaten laden (falls vorhanden)
    * Kanäle auswählen
    * Plot anzeigen, Zoom/Pan (nativ durch PyQtGraph)

Noch NICHT implementiert (siehe Vorgabe): FFT, Filter, RMS, Statistik,
automatische Reports. Die Architektur ist jedoch darauf vorbereitet -
siehe `analysis/basic_analysis.py` für die vorgesehenen Erweiterungspunkte.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from PyQt6.QtGui import QFont

import pyqtgraph as pg
from PyQt6.QtCore import QEvent, pyqtSignal, Qt
from PyQt6.QtGui import QDrag, QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from data.loader import LoadedMeasurement, LoaderError, infer_metadata_path, load_measurement_file
from gui.i18n import connect_language_changed, t
from gui.theme import connect_theme_changed, style_plot_container, style_plot_item

logger = logging.getLogger(__name__)

_SUPPORTED_SUFFIXES = {".parquet", ".csv"}
_DRAG_PREFIX = "daq.channel/"
_ROLE_FILE_PATH = int(Qt.ItemDataRole.UserRole)
_ROLE_CHANNEL_NAME = int(Qt.ItemDataRole.UserRole) + 1
_LAYOUT_SPECS = {
    "single": [(0, 0, 0, 1, 1)],
    "split": [(0, 0, 0, 1, 1), (1, 1, 0, 1, 1)],
    "three": [(0, 0, 0, 1, 1), (1, 1, 0, 1, 1), (2, 2, 0, 1, 1)],
    "four": [(0, 0, 0, 1, 1), (1, 1, 0, 1, 1), (2, 2, 0, 1, 1), (3, 3, 0, 1, 1)],
    "four_square": [(0, 0, 0, 1, 1), (1, 0, 1, 1, 1), (2, 1, 0, 1, 1), (3, 1, 1, 1, 1)],
}


class ChannelTreeWidget(QTreeWidget):
    """Tree mit explizitem Drag-Payload für Kanal-Zuordnung auf Plot-Ziele."""

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


class AnalysisView(QWidget):
    """Ansicht zum Laden und Untersuchen abgeschlossener Messungen."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

        self._loaded_measurement: LoadedMeasurement | None = None
        self._curves: list = []
        self._loaded_measurements: list[tuple[Path, LoadedMeasurement]] = []
        self._channel_assignments: dict[tuple[str, str], int] = {}

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
            plot_widget.setLabel("bottom", t("axis_time"), units="s")
            plot_widget.addLegend()
            style_plot_container(plot_widget)
            style_plot_item(plot_widget.getPlotItem())
            plot_widget.channel_dropped.connect(self._on_channel_dropped_to_plot)
        content_row.addWidget(self._plot_area, stretch=1)

        # Rechts: kategorisierter Seitenbereich (Dateien/Kanäle)
        right_panel = QWidget()
        right_panel.setMinimumWidth(360)
        right_panel.setMaximumWidth(460)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        files_header_row = QHBoxLayout()
        files_header_row.setContentsMargins(0, 0, 0, 0)
        files_header_row.setSpacing(8)
        self._files_category_label = QLabel(t("analysis_category_files"))
        self._browse_button = QPushButton(t("browse_file_button"))
        self._browse_button.clicked.connect(self._on_browse_clicked)
        files_header_row.addWidget(self._files_category_label)
        files_header_row.addStretch(1)
        files_header_row.addWidget(self._browse_button)
        right_layout.addLayout(files_header_row)

        self._files_drop_hint = QLabel(t("drag_drop_files"))
        self._files_drop_hint.setWordWrap(True)
        self._files_drop_hint.setStyleSheet("QLabel { color: palette(foreground); }")
        right_layout.addWidget(self._files_drop_hint)

        self._tree = ChannelTreeWidget()
        self._tree.setHeaderLabels([t("tree_header_name")])
        self._tree.itemChanged.connect(self._on_tree_item_changed)
        self._tree.setDragEnabled(True)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # Kontextmenü für Top-Level-Dateien (rechtsklick)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self._files_label = QLabel(t("loaded_files_channels"))
        right_layout.addWidget(self._files_label)
        right_layout.addWidget(self._tree, stretch=1)
        content_row.addWidget(right_panel)

        layout.addLayout(content_row, stretch=1)

        # Kategorieköpfe visuell wie im Setup-Tab (ohne farbliche Overrides).
        category_font = QFont(self.font())
        if category_font.pointSize() > 0:
            category_font.setPointSize(category_font.pointSize() + 1)
        category_font.setBold(True)
        self._layout_category_label.setFont(category_font)
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

        # Eigenes Stylesheet (`color: palette(foreground)`) auf einem
        # QLabel wird von Qt nach einem Palettenwechsel nicht automatisch
        # neu ausgewertet - ohne explizites Repolish bleibt der Text in der
        # Farbe des Themes haengen, mit dem das Label urspruenglich erzeugt
        # wurde (siehe gleiches Problem/Fix bei den Nav-Kacheln in
        # gui/main_window.py::_retheme_nav_icons).
        self._files_drop_hint.style().unpolish(self._files_drop_hint)
        self._files_drop_hint.style().polish(self._files_drop_hint)
        self._files_drop_hint.update()

    def retranslate_ui(self) -> None:
        """Aktualisiert alle statischen Texte nach einem Sprachwechsel."""
        self._browse_button.setText(t("browse_file_button"))
        self._layout_category_label.setText(t("analysis_category_layout"))
        self._files_category_label.setText(t("analysis_category_files"))
        self._files_drop_hint.setText(t("drag_drop_files"))
        self._files_label.setText(t("loaded_files_channels"))
        self._layout_label.setText(t("analysis_layout"))
        self._populate_layout_combo()
        self._tree.setHeaderLabels([t("tree_header_name")])
        for plot_widget in self._plot_widgets:
            plot_widget.setLabel("bottom", t("axis_time"), units="s")
        self._update_files_label_count()

    def _update_files_label_count(self) -> None:
        """Aktualisiert die Datei/Kanal-Überschrift inkl. Anzahl geladener Dateien."""
        if self._loaded_measurements:
            self._files_label.setText(
                f"{t('loaded_files_channels')} - {t('files_loaded_count', count=len(self._loaded_measurements))}"
            )
        else:
            self._files_label.setText(t("loaded_files_channels"))

    # ------------------------------------------------------------------ #
    # Drag & Drop
    # ------------------------------------------------------------------ #

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        urls = event.mimeData().urls()
        if not urls:
            return
        path = Path(urls[0].toLocalFile())
        self._load_file(path)

    # ------------------------------------------------------------------ #
    # Interna
    # ------------------------------------------------------------------ #

    def _on_browse_clicked(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            t("load_measurement_dialog_title"),
            "",
            t("measurement_files_filter"),
        )
        if filename:
            self._load_file(Path(filename))

    def _on_tree_context_menu(self, point) -> None:
        """Zeigt ein Kontextmenü zum Entfernen der Datei unter dem angeklickten Tree-Item."""
        item = self._tree.itemAt(point)
        if item is None:
            return
        # finde Top-Level-Item (Datei)
        top = item
        while top.parent() is not None:
            top = top.parent()

        menu = QMenu(self)
        remove_action = menu.addAction(t("remove_file_action"))
        remove_action.triggered.connect(lambda: self._remove_file_item(top))
        menu.exec(self._tree.viewport().mapToGlobal(point))

    def _remove_file_item(self, top: QTreeWidgetItem) -> None:
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

        try:
            metadata_path = infer_metadata_path(path)
            measurement = load_measurement_file(path, metadata_path)
        except LoaderError as exc:
            QMessageBox.critical(self, t("load_error_title"), str(exc))
            return

        # Verhindere doppelte Einträge
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
                # Create a lightweight Channel placeholder
                from data.models import Channel

                placeholder = Channel(hardware_channel=name, display_name=name)
                ch_item.setData(0, Qt.ItemDataRole.UserRole, placeholder)
                ch_item.setData(0, _ROLE_CHANNEL_NAME, name)

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
        if not self._loaded_measurements:
            return
        active_indices = self._active_plot_indices()
        if not active_indices:
            return

        # Iterate over tree: top-level items are files
        for file_idx in range(self._tree.topLevelItemCount()):
            file_item = self._tree.topLevelItem(file_idx)
            file_path_str = file_item.data(0, Qt.ItemDataRole.UserRole)
            if not file_path_str:
                continue
            # Find measurement in loaded list
            matched = [m for p, m in self._loaded_measurements if str(p) == file_path_str or p.name == file_path_str]
            if not matched:
                continue
            meas = matched[0]
            data = meas.data
            if "time_s" not in data.columns:
                continue
            time_s = data["time_s"].to_numpy()

            # If file unchecked, skip
            if file_item.checkState(0) != Qt.CheckState.Checked:
                continue

            # Iterate channel items directly under file_item
            for ch_i in range(file_item.childCount()):
                ch_item = file_item.child(ch_i)
                if ch_item.checkState(0) != Qt.CheckState.Checked:
                    continue
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
                plot_widget = self._plot_widgets[min(plot_index, len(self._plot_widgets) - 1)]

                color = pg.intColor(file_idx * 8 + ch_i, hues=max(self._tree.topLevelItemCount() * 8, 1))
                curve = plot_widget.plot(
                    time_s,
                    data[chan_name].to_numpy(),
                    pen=pg.mkPen(color=color, width=1.2),
                    name=f"{file_item.text(0)} - {display_label}",
                )
                self._curves.append(curve)

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
