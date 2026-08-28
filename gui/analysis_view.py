"""
gui/analysis_view.py

Analysis view.

Features (per spec):
    * Drag & drop of measurement files (.parquet, .csv)
    * Load metadata (if available)
    * Select channels
    * Show plot, zoom/pan (native via PyQtGraph)
    * Analysis functions (FFT, low-/high-pass, smoothing, see
      `analysis/basic_analysis.py`) - results are stored as a new channel
      under the source file in the file browser and can be saved as
      CSV/Parquet via right-click.

NOT YET implemented (per spec): RMS, statistics, automatic
reports. The architecture is however prepared for this - see
`analysis/basic_analysis.py` for the planned extension points.
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
    draw_crosshair_icon,
    draw_fft_icon,
    draw_highpass_icon,
    draw_lowpass_icon,
    draw_smoothing_icon,
    plot_background_color,
    plot_foreground_color,
    repolish,
    style_plot_container,
    style_plot_item,
)
from gui.widgets.crosshair import SnappingCrosshair
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

# (kind, icon_fn, label_i18n_key, tooltip_i18n_key) - shared between the
# button setup in AnalysisView.__init__ and the icon redraw in
# retheme_plots() after a theme change.
_FUNCTION_SPECS = [
    ("fft", draw_fft_icon, "analysis_fft_button", "analysis_fft_tooltip"),
    ("lowpass", draw_lowpass_icon, "analysis_lowpass_button", "analysis_lowpass_tooltip"),
    ("highpass", draw_highpass_icon, "analysis_highpass_button", "analysis_highpass_tooltip"),
    ("smoothing", draw_smoothing_icon, "analysis_smoothing_button", "analysis_smoothing_tooltip"),
]
_FUNCTION_SPECS_BY_KIND = {spec[0]: spec for spec in _FUNCTION_SPECS}

# Grouping of the analysis function buttons in the toolbox: spectral
# analysis (FFT) separate from filters (low-/high-pass AND smoothing).
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
    """Tree with an explicit drag payload for channel assignment to plot targets.

    Also handles file drag&drop (loading by dragging from the file
    explorer) and its empty-state hint text - deliberately here and
    NOT on the whole `AnalysisView`, so the drop zone stays exactly
    confined to this tree both visually AND functionally.
    """

    delete_key_pressed = pyqtSignal()
    file_dropped = pyqtSignal(object)  # Path

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setAcceptDrops(True)
        # Drag&drop events land on the viewport for Qt item views, not on
        # the outer widget (see the same pattern in AssignablePlotWidget
        # further below) - without this call, externally dragged files
        # never reach dragEnterEvent()/dropEvent().
        self.viewport().setAcceptDrops(True)

        # Empty-state hint as an overlay on the viewport (unlike e.g.
        # QLineEdit, QTreeWidget has no native `placeholderText`)
        # - visible only as long as no file is loaded, kept in sync
        # automatically via the model signals instead of having to be
        # updated manually at every single spot in AnalysisView.
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
        """Forces a repolish of the hint text after a theme change
        (Qt otherwise doesn't automatically re-evaluate a custom
        stylesheet, see the same problem with the nav tiles in
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
        # Without this override, QAbstractItemView's default
        # dragMoveEvent takes over, which rejects external URL drops
        # (model doesn't know them) - visible as the "not allowed" cursor
        # while dragging, even though dragEnterEvent() above already
        # accepted it.
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
    """Plot target that accepts channel assignments via drag&drop."""

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
    """Dialog for channel and parameter selection for an analysis function.

    Channel selection happens via a tree view grouped by file (like the
    file browser on the right of the analysis view, see
    `ChannelTreeWidget`) instead of a flat dropdown list - with several
    loaded files each having several channels, this is clearer and shows
    at a glance which file a channel belongs to.

    Opened when one of the analysis function buttons is clicked
    (see `AnalysisView._on_analysis_function_clicked`).
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
    """View for loading and examining completed measurements."""

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
        # References to running background workers (see gui/workers.py)
        # - must be kept alive until completion, otherwise Python would
        # garbage-collect the QThread object prematurely.
        self._background_workers: list[BackgroundWorker] = []

        layout = QVBoxLayout(self)

        # --- Plot area + right-hand side panel ---
        content_row = QHBoxLayout()

        # Left: categories/controls (layout)
        left_panel = QWidget()
        left_panel.setMinimumWidth(240)
        left_panel.setMaximumWidth(320)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)

        # --- Tools ---
        self._tools_category_label = QLabel(t("analysis_category_tools"))
        left_layout.addWidget(self._tools_category_label)

        # Checkable, and off by default: the cursor follows every mouse
        # move over a plot, so it should only run when actually wanted.
        self._cursor_button = QToolButton()
        self._cursor_button.setCheckable(True)
        self._cursor_button.setIcon(QIcon(draw_crosshair_icon(32)))
        self._cursor_button.setIconSize(QSize(32, 32))
        self._cursor_button.setFixedSize(56, 56)
        self._cursor_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._cursor_button.setToolTip(f"{t('analysis_cursor_button')} — {t('analysis_cursor_tooltip')}")
        self._cursor_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cursor_button.toggled.connect(self._on_cursor_toggled)
        tools_row = QHBoxLayout()
        tools_row.setContentsMargins(0, 0, 0, 0)
        tools_row.addWidget(self._cursor_button)
        tools_row.addStretch(1)
        left_layout.addLayout(tools_row)

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

        # Middle/left: plot area (switchable grid layouts)
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
            # `units=` NOT used: PyQtGraph always renders that internally in
            # round parentheses - hardcode "[s]" in the text itself instead,
            # so the time unit is consistently shown in square brackets
            # everywhere (see gui/live_view.py). Font size set explicitly to
            # match the axis tick labels (see
            # gui/theme.py::axis_tick_point_size) - MUST be set before
            # `style_plot_item()`, see gui/live_view.py::_axis_label_style
            # for the reasoning.
            # `_refresh_plot_axis_labels()`/`retranslate_ui()` later set the
            # text again without kwargs, which keeps this style.
            plot_widget.setLabel(
                "bottom", f"{t('axis_time')} [s]", **{"font-size": f"{axis_tick_point_size()}pt"}
            )
            plot_widget.addLegend()
            style_plot_container(plot_widget)
            style_plot_item(plot_widget.getPlotItem())
            # WITHOUT this explicit call, the ViewBox background color
            # stays at PyQtGraph's default (transparent) - invisible under
            # pure software rendering (shows the container color through),
            # but with `useOpenGL=True` (see gui/live_view.py, active
            # globally for the process), a transparent ViewBox falls back
            # to the OpenGL default color instead of the scene color -
            # visible as a color mismatch between the plot area and the
            # axis label border. Live View therefore sets this explicitly
            # everywhere (see `_channel_background_color`).
            plot_widget.getPlotItem().getViewBox().setBackgroundColor(plot_background_color())
            plot_widget.channel_dropped.connect(self._on_channel_dropped_to_plot)
        # One cursor per plot, created up front but idle - the items only
        # enter the plot when the tool is switched on (see
        # `gui/widgets/crosshair.py`).
        self._crosshairs = [
            SnappingCrosshair(
                plot_widget,
                plot_foreground_color(),
                plot_foreground_color(),
                plot_background_color(),
            )
            for plot_widget in self._plot_widgets
        ]
        for index, plot_widget in enumerate(self._plot_widgets):
            # The scene signal, not a widget event: PyQtGraph delivers mouse
            # moves over the plot through the scene, and it is the only
            # place that sees them without stealing them from the view box's
            # own pan/zoom handling.
            plot_widget.scene().sigMouseMoved.connect(
                lambda pos, i=index: self._on_plot_mouse_moved(i, pos)
            )

        content_row.addWidget(self._plot_area, stretch=1)

        # Right: categorized side panel (files/channels)
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
        # Context menu for top-level files (right-click)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self._tree.delete_key_pressed.connect(self._on_delete_key_pressed)
        # Load a file by dragging from the file explorer - drop zone
        # deliberately confined to the tree (see the ChannelTreeWidget
        # class docstring), not the whole view.
        self._tree.file_dropped.connect(self._load_file)
        self._files_label = QLabel(t("loaded_files_channels"))
        right_layout.addWidget(self._files_label)

        # Search/filter field: hides file/channel rows that neither match
        # the search text themselves nor (for a file) have a matching
        # child - pure visibility filtering, doesn't change the loaded
        # data. Automatically reapplied after every structural change of
        # the tree (see the model signal connections below), so e.g.
        # newly loaded files don't bypass an active filter.
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

        # Category headers visually matching the setup tab (without color overrides).
        category_font = QFont(self.font())
        if category_font.pointSize() > 0:
            category_font.setPointSize(category_font.pointSize() + 1)
        category_font.setBold(True)
        self._layout_category_label.setFont(category_font)
        for category_label in self._function_category_labels.values():
            category_label.setFont(category_font)
        self._tools_category_label.setFont(category_font)
        self._files_category_label.setFont(category_font)

        self._populate_layout_combo()
        self._apply_plot_layout()
        connect_language_changed(self.retranslate_ui)
        connect_theme_changed(self.retheme_plots)

    def retheme_plots(self) -> None:
        """Recolors the plot background/axes after a theme change.

        PyQtGraph widgets don't follow the `QApplication` palette
        automatically (see `gui/theme.py`).
        """
        for plot_widget in self._plot_widgets:
            style_plot_container(plot_widget)
            style_plot_item(plot_widget.getPlotItem())
            plot_widget.getPlotItem().getViewBox().setBackgroundColor(plot_background_color())

        # Analysis function icons follow the WindowText color (see
        # gui/theme.py::nav_icon_color) and therefore must be redrawn
        # after a theme change.
        for kind, icon_fn, _label_key, _tooltip_key in _FUNCTION_SPECS:
            button = self._function_buttons.get(kind)
            if button is not None:
                button.setIcon(QIcon(icon_fn(32)))

        self._tree.retheme_empty_hint()

    def _on_cursor_toggled(self, enabled: bool) -> None:
        """Switches the readout cursor on all plots at once.

        Not per plot: the tool answers 'what does this sample say', and
        having to arm each of up to four plots separately would be busy
        work for no gain."""
        for crosshair in self._crosshairs:
            crosshair.set_enabled(enabled)

    def _on_plot_mouse_moved(self, plot_index: int, scene_pos) -> None:
        """Moves the cursor of plot `plot_index` to the nearest sample.

        The scene signal fires for every mouse move over the plot even
        while the tool is off, so the disabled case returns immediately -
        `SnappingCrosshair.move_to` does that itself, this only spares
        the lookup."""
        if not self._cursor_button.isChecked():
            return
        if 0 <= plot_index < len(self._crosshairs):
            self._crosshairs[plot_index].move_to(scene_pos)

    def retranslate_ui(self) -> None:
        """Updates all static texts after a language change."""
        self._layout_category_label.setText(t("analysis_category_layout"))
        for category_key, category_label in self._function_category_labels.items():
            category_label.setText(t(category_key))
        self._tools_category_label.setText(t("analysis_category_tools"))
        self._cursor_button.setToolTip(f"{t('analysis_cursor_button')} — {t('analysis_cursor_tooltip')}")
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
        """Sets the x-axis label for each plot to match the channels
        currently displayed there (time or frequency axis, see
        `_update_plot`)."""
        for plot_index, plot_widget in enumerate(self._plot_widgets):
            if self._plot_x_columns.get(plot_index) == _FREQUENCY_X_COLUMN:
                plot_widget.setLabel("bottom", t("axis_frequency"), units="Hz")
            else:
                # `units=` NOT used - time unit consistently shown in
                # square brackets everywhere (see gui/live_view.py).
                plot_widget.setLabel("bottom", f"{t('axis_time')} [s]")

    def _update_files_label_count(self) -> None:
        """Updates the file/channel heading including the count of loaded files."""
        if self._loaded_measurements:
            self._files_label.setText(
                f"{t('loaded_files_channels')} - {t('files_loaded_count', count=len(self._loaded_measurements))}"
            )
        else:
            self._files_label.setText(t("loaded_files_channels"))

    def _apply_tree_filter(self, *_args) -> None:
        """Hides file/channel rows that don't match the search text
        (case-insensitive).

        A file stays visible if ITS name matches (then all of its
        children too, unfiltered) OR at least one channel/result child
        matches (then only the matching children). `*_args` accepts the
        unused arguments passed by both `QLineEdit.textChanged`
        (str) AND the model signals `rowsInserted`/`rowsRemoved`
        (QModelIndex, int, int) alike - the search text is always read
        fresh from `self._tree_search_edit`.
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
    # Public API (called from gui/main_window.py)
    # ------------------------------------------------------------------ #

    def prompt_and_load_file(self) -> bool:
        """Opens a file dialog to load a measurement and, once selected,
        starts the (background) loading process, see `_load_file`.

        Called from the "Load measurement..." menu entry in
        `gui/main_window.py` (formerly a button directly in this view -
        now reachable from any view). Returns True if a file was
        selected (main_window then navigates to the analysis tab), False
        if the dialog was cancelled.
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
    # Internals
    # ------------------------------------------------------------------ #

    def _begin_busy(self) -> None:
        """Disables the controls whose actions run in the background
        (file loading, analysis functions, see `gui/workers.py`) and
        shows a wait cursor - prevents multiple such operations from
        being started at the same time, while keeping the user informed
        about the progress.
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
        """Removes a completed `BackgroundWorker` reference so that
        `_background_workers` doesn't grow unbounded over a long program
        runtime."""
        if worker in self._background_workers:
            self._background_workers.remove(worker)
        worker.deleteLater()

    def _on_tree_context_menu(self, point) -> None:
        """Shows a context menu for removing the file or a single
        channel - for analysis result channels, also for saving the
        result."""
        item = self._tree.itemAt(point)
        if item is None:
            return

        menu = QMenu(self)
        if item.parent() is None:
            # Top-level item (file/measurement)
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
        """Deletes the currently selected channel or the selected
        measurement from the file browser (Delete key, see
        `ChannelTreeWidget`)."""
        item = self._tree.currentItem()
        if item is None:
            return
        if item.parent() is None:
            self._remove_file_item(item)
        else:
            self._remove_channel_item(item)

    def _confirm_delete(self, body: str) -> bool:
        """Explicitly asks for confirmation before a delete action
        (file/channel, via context menu or Delete key)."""
        return confirm_delete(self, body)

    def _remove_channel_item(self, item: QTreeWidgetItem) -> None:
        """Removes a single channel (regular or analysis result) from
        the tree, without removing the entire source file."""
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
        """Saves an analysis result channel as a standalone CSV/Parquet file."""
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
        # Remove entry from loaded measurements
        self._loaded_measurements = [pair for pair in self._loaded_measurements if str(pair[0]) != str(file_path_str) and pair[0].name != str(file_path_str)]
        # Also remove any existing plot assignments for this file.
        self._channel_assignments = {
            key: value
            for key, value in self._channel_assignments.items()
            if key[0] != str(file_path_str)
        }
        # Remove top-level item from tree
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

        # Prevent duplicate entries (cheap check, deliberately still
        # before the actual - potentially slow - background loading).
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

        # The loading itself (pd.read_parquet/pd.read_csv) runs in the
        # background, since with large measurement files that would
        # noticeably block the GUI thread (see
        # gui/workers.py::BackgroundWorker).
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

        # Between starting the background load and here, the same file
        # could already have been added via a second, concurrently
        # started load - check again instead of inserting blindly.
        if any(p == path for p, _ in self._loaded_measurements):
            QMessageBox.information(
                self, t("already_loaded_title"), t("already_loaded_body", filename=path.name)
            )
            return

        self._loaded_measurements.append((path, measurement))
        # Create tree entries: file -> modules -> channels
        file_item = QTreeWidgetItem(self._tree, [path.name])  # top-level
        file_item.setFlags(file_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        file_item.setCheckState(0, Qt.CheckState.Unchecked)
        file_item.setData(0, Qt.ItemDataRole.UserRole, str(path))

        # Add direct channel entries under the file (if metadata is available)
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
            # Fallback: use DataFrame columns as channels (no metadata)
            inferred = measurement.channel_names
            for name in inferred:
                if name == "time_s":
                    continue
                ch_item = QTreeWidgetItem(file_item, [name])
                ch_item.setFlags(ch_item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
                ch_item.setCheckState(0, Qt.CheckState.Unchecked)
                # Lightweight channel placeholder (no metadata available)
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
            # Find measurement in loaded list (fallback for channels
            # without their own _ROLE_MEASUREMENT assignment)
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

                # Every channel carries a reference to its own
                # LoadedMeasurement (see _load_file/_add_result_channel) -
                # for analysis results (e.g. FFT), this differs from the
                # source file's measurement DataFrame (different x-axis).
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
                # Only plot channels that are already assigned (drag&drop).
                # The checkbox only shows/hides them afterwards.
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
                # Min/max decimation per pixel (see gui/live_view.py:44-53) -
                # keeps zoom/pan smooth for very large datasets without
                # losing short spikes/outliers (method="peak" instead of
                # "mean").
                curve.setDownsampling(auto=True, method="peak")
                curve.setClipToView(True)
                self._curves.append(curve)

        self._refresh_plot_axis_labels()

    # ------------------------------------------------------------------ #
    # Analysis functions (FFT, low-/high-pass, smoothing)
    # ------------------------------------------------------------------ #

    def _on_analysis_function_clicked(self, kind: str) -> None:
        """Opens the channel/parameter dialog for an analysis function and
        stores the result as a new channel under the source file."""
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

        # The actual computation (compute_fft/apply_filter/
        # apply_smoothing) runs in the background (see
        # gui/workers.py::BackgroundWorker) - for long measurements (e.g.
        # 100 kHz over several minutes), this could otherwise block the
        # GUI thread for a noticeable time. Only the fast preparation
        # steps (determine sample rate, extract x-axis) stay synchronous.
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
        """Runs an analysis function in the background and, on success,
        calls `on_success(result)` on the GUI thread (see the
        `_finish_result` calls in `_on_analysis_function_clicked`)."""
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
        """All files currently present in the tree with their channels
        (regular and analysis results), grouped by file - as
        `(file name, file path, [(channel display name, channel name), ...])`
        for the tree selection in the analysis function dialog (see
        `_AnalysisFunctionDialog`). Files without selectable channels are
        left out instead of being shown as an empty group."""
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
        """Determines the sample rate of a channel: preferably from the
        metadata (`sample_rate_hz`), otherwise estimated from the median
        spacing of the x-axis values (e.g. for files without a metadata
        JSON)."""
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
        """Native sample rate of a single channel from the metadata
        (key `native_sample_rate_hz`, see
        `data/metadata.py::build_measurement_metadata`), if present.

        Can differ from the file tick rate (`_resolve_sample_rate`) if
        the channel was forward-filled via
        `core/rate_merge.py::RateMerger` (currently only possible for
        the NI9210). Deliberately looks up the RAW metadata dictionary
        (`measurement.metadata["channels"]`), NOT via
        `measurement.channels`/`Channel.from_dict` - the latter doesn't
        know `native_sample_rate_hz` as its own dataclass field and
        would silently lose it when reconstructing.
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
        """Returns the data + sample rate to use for FFT/filter.

        For a forward-filled channel (native rate detectably below the
        file tick rate, see `_resolve_native_sample_rate`), the repeated
        values are first removed
        (`analysis.basic_analysis.native_samples`) and computed with the
        true native rate - otherwise a zero-order-hold staircase (see
        `core/rate_merge.py`) would fake a false, sinc-shaped spectrum
        artifact. For all other channels (the normal case, native rate
        == tick rate), exactly unchanged behavior - `measurement.data`/
        `tick_rate_hz` are returned untouched.

        The `* 0.99` tolerance covers rounding when saving/rounding the
        rates, without falsely treating a minimally different but
        actually identical rate as "forward-filled".
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

        # Make visible after assignment: set file + channel to "checked".
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

        # Only stretch rows/columns that are actually used, so single/
        # stacked layouts fill the full width/height.
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
