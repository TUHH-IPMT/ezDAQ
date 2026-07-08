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

import logging
from pathlib import Path

import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from data.loader import LoadedMeasurement, LoaderError, infer_metadata_path, load_measurement_file

logger = logging.getLogger(__name__)

_SUPPORTED_SUFFIXES = {".parquet", ".csv"}


class AnalysisView(QWidget):
    """Ansicht zum Laden und Untersuchen abgeschlossener Messungen."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

        self._loaded_measurement: LoadedMeasurement | None = None
        self._curves: list = []
        self._loaded_measurements: list[tuple[Path, LoadedMeasurement]] = []

        layout = QVBoxLayout(self)

        # --- Datei laden ---
        file_row = QHBoxLayout()
        self._drop_label = QLabel(
            "Messdateien (.parquet/.csv) hierher ziehen oder über den Button auswählen"
        )
        self._drop_label.setStyleSheet(
            "QLabel { border: 2px dashed gray; padding: 12px; }"
        )
        self._browse_button = QPushButton("Datei auswählen...")
        self._browse_button.clicked.connect(self._on_browse_clicked)
        file_row.addWidget(self._drop_label, stretch=1)
        file_row.addWidget(self._browse_button)
        layout.addLayout(file_row)

        # --- Dateien/Module/Kanäle (Tree) + Plot + Detail-Panel ---
        content_row = QHBoxLayout()

        # Linke Spalte: Tree mit Dateien -> Module -> Kanäle
        left_col = QVBoxLayout()
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Name"])
        self._tree.itemChanged.connect(self._on_tree_item_changed)
        # Kontextmenü für Top-Level-Dateien (rechtsklick)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self._tree.setMaximumWidth(420)
        left_col.addWidget(QLabel("Geladene Dateien / Kanäle"))
        left_col.addWidget(self._tree)

        content_row.addLayout(left_col)

        # Mitte: Plot
        self._plot_widget = pg.PlotWidget()
        self._plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self._plot_widget.setLabel("bottom", "Zeit", units="s")
        self._plot_widget.addLegend()
        content_row.addWidget(self._plot_widget, stretch=1)

        layout.addLayout(content_row, stretch=1)

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
            "Messdatei auswählen",
            "",
            "Messdaten (*.parquet *.csv)",
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
        remove_action = menu.addAction("Datei aus Analyse löschen")
        remove_action.triggered.connect(lambda: self._remove_file_item(top))
        menu.exec(self._tree.viewport().mapToGlobal(point))

    def _remove_file_item(self, top: QTreeWidgetItem) -> None:
        file_path_str = top.data(0, Qt.ItemDataRole.UserRole)
        # Entferne Eintrag aus geladenen Messungen
        self._loaded_measurements = [pair for pair in self._loaded_measurements if str(pair[0]) != str(file_path_str) and pair[0].name != str(file_path_str)]
        # Entferne Top-Level-Item aus Tree
        idx = self._tree.indexOfTopLevelItem(top)
        if idx != -1:
            self._tree.takeTopLevelItem(idx)
        self._drop_label.setText(f"Geladen: {len(self._loaded_measurements)} Datei(en)")
        self._update_plot()

    def _load_file(self, path: Path) -> None:
        if path.suffix.lower() not in _SUPPORTED_SUFFIXES:
            QMessageBox.warning(
                self,
                "Nicht unterstütztes Format",
                f"Das Format '{path.suffix}' wird nicht unterstützt "
                f"(erwartet: .parquet oder .csv).",
            )
            return

        try:
            metadata_path = infer_metadata_path(path)
            measurement = load_measurement_file(path, metadata_path)
        except LoaderError as exc:
            QMessageBox.critical(self, "Fehler beim Laden", str(exc))
            return

        # Verhindere doppelte Einträge
        if any(p == path for p, _ in self._loaded_measurements):
            QMessageBox.information(self, "Bereits geladen", f"Datei {path.name} ist bereits geladen.")
            return

        self._loaded_measurements.append((path, measurement))
        # Erzeuge Tree-Einträge: Datei -> Module -> Kanäle
        file_item = QTreeWidgetItem(self._tree, [path.name])  # top-level
        file_item.setFlags(file_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        file_item.setCheckState(0, Qt.CheckState.Checked)
        file_item.setData(0, Qt.ItemDataRole.UserRole, str(path))

        # Füge direkte Kanal-Einträge unter der Datei hinzu (falls Metadaten vorhanden)
        if measurement.channels:
            for ch in measurement.channels:
                ch_item = QTreeWidgetItem(file_item, [ch.display_name or ch.hardware_channel])
                ch_item.setFlags(ch_item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
                ch_item.setCheckState(0, Qt.CheckState.Checked if ch.enabled else Qt.CheckState.Unchecked)
                ch_item.setData(0, Qt.ItemDataRole.UserRole, ch)
        else:
            # Fallback: benutze DataFrame-Spalten als Kanäle (keine Metadaten)
            inferred = measurement.channel_names
            for name in inferred:
                if name == "time_s":
                    continue
                ch_item = QTreeWidgetItem(file_item, [name])
                ch_item.setFlags(ch_item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
                ch_item.setCheckState(0, Qt.CheckState.Checked)
                # Create a lightweight Channel placeholder
                from data.models import Channel

                placeholder = Channel(hardware_channel=name, display_name=name)
                ch_item.setData(0, Qt.ItemDataRole.UserRole, placeholder)

        self._tree.expandItem(file_item)
        self._drop_label.setText(f"Geladen: {len(self._loaded_measurements)} Datei(en)")
        self._update_plot()

    def _on_tree_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        # When a parent is checked/unchecked, propagate to children
        state = item.checkState(0)
        for i in range(item.childCount()):
            child = item.child(i)
            child.setCheckState(0, state)
        self._update_plot()

    def _update_plot(self) -> None:
        self._plot_widget.clear()
        self._curves = []
        if not self._loaded_measurements:
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

                color = pg.intColor(file_idx * 8 + ch_i, hues=max(self._tree.topLevelItemCount() * 8, 1))
                curve = self._plot_widget.plot(
                    time_s,
                    data[chan_name].to_numpy(),
                    pen=pg.mkPen(color=color, width=1.2),
                    name=f"{file_item.text(0)} - {display_label}",
                )
                self._curves.append(curve)
