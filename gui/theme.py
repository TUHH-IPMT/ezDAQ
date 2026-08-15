"""
gui/theme.py

Einfaches Hell/Dunkel-Theming für die gesamte Anwendung.

Verwendung (siehe `gui/i18n.py` für dasselbe Grundmuster):
    from gui.theme import init_theme, set_theme, connect_theme_changed

    init_theme(app)          # einmalig beim App-Start, nach QApplication(...)
    set_theme("dark")        # oder "light" - wirkt sofort auf alle Qt-Widgets

Live-Umschaltung:
    Qt-Standardwidgets (Buttons, Labels, Menüs, ...) folgen automatisch der
    `QApplication`-`QPalette` - dafür reicht `set_theme()`. PyQtGraph-Plots
    (Live View, Analyse) folgen der Palette NICHT automatisch; Ansichten mit
    Plots registrieren sich über `connect_theme_changed(self.retheme_plots)`
    und färben ihre bereits vorhandenen Plot-Widgets über
    `style_plot_container()`/`style_plot_item()` selbst nach.
"""

from __future__ import annotations

import math
from typing import Callable, Optional

import pyqtgraph as pg
from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygonF

_current_theme = "light"

_PLOT_COLORS = {
    # Bewusst Hex statt PyQtGraph-Kurzformen ("w"/"k"): die werden auch in
    # echten Qt-Stylesheets verwendet (siehe
    # `gui/live_view.py::ChannelPopoutWindow._style_value_labels`), wo
    # "w"/"k" KEIN gueltiges CSS sind und dort still verworfen wurden -
    # sichtbar als komplett schwarze Messwertanzeige im Light-Theme (die
    # Kurzformen versteht nur PyQtGraph selbst, nicht Qt's CSS-Parser).
    # `is_theme_default_plot_background()` erkennt "w" als Altwert
    # weiterhin (siehe dort), betrifft also nur NEU gespeicherte Werte.
    "light": {"background": "#ffffff", "foreground": "#000000", "curve": "#1565c0"},
    "dark": {"background": "#232323", "foreground": "#e0e0e0", "curve": "#64b5f6"},
}

# Hintergrundfarbe des Plot-CONTAINERS (Achsentick-Rand, Zwischenraeume
# zwischen Subplots, Messwertanzeige-Boxen) - bewusst NICHT dieselbe wie
# `_PLOT_COLORS[...]["background"]` (das bleibt reserviert fuer die
# eigentliche Plotflaeche, wo Daten/Kurven landen, siehe
# `_channel_background_color` in gui/live_view.py). Entspricht exakt
# `QPalette.ColorRole.Window` aus `_build_light_palette`/
# `_build_dark_palette` unten, damit der Rand der Plot-Widgets optisch mit
# dem Rest der App-Oberflaeche (Fenster-/Panel-Hintergrund) verschmilzt,
# statt wie ein eigenstaendiges weisses/schwarzes Rechteck zu wirken.
_PLOT_CONTAINER_COLORS = {"light": "#f0f0f0", "dark": "#353535"}

# Gleiche Form/Padding/Schrift wie der gruene "Messung starten"-Button
# (siehe `gui/setup_view.py`/`gui/live_view.py`), nur grau statt gruen -
# fuer den Trigger-Scharf-Button (`_trigger_arm_button`), der in BEIDEN
# Ansichten identisch aussehen soll. An einer Stelle definiert, damit
# beide nicht auseinanderlaufen koennen.
TRIGGER_ARM_BUTTON_STYLE = (
    "QPushButton { background-color: #5f6368; color: #f9fafb; border: none;"
    " padding: 6px 16px; border-radius: 4px; font-weight: 700; font-size: 11pt; }"
    "QPushButton:hover { background-color: #4d5054; }"
    "QPushButton:pressed, QPushButton:checked { background-color: #3c3f42; }"
)


def _build_light_palette() -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(240, 240, 240))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(0, 0, 0))
    palette.setColor(QPalette.ColorRole.Base, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(245, 245, 245))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(255, 255, 220))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(0, 0, 0))
    palette.setColor(QPalette.ColorRole.Text, QColor(0, 0, 0))
    palette.setColor(QPalette.ColorRole.Button, QColor(240, 240, 240))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(0, 0, 0))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 0, 0))
    # 3D-Schattierungsrollen (Light/Midlight/Dark/Mid/Shadow) - werden von
    # QPalette NICHT automatisch aus Button/Window abgeleitet, wenn man
    # (wie hier) einzelne Rollen statt des Ein-Farb-Konstruktors setzt.
    # Ohne sie fallen QSS-Referenzen wie "palette(light)"/"palette(dark)"
    # (siehe Navigationskacheln in gui/main_window.py) auf Qts
    # themenunabhängige Standardgrautöne zurück statt auf dieses Theme.
    palette.setColor(QPalette.ColorRole.Light, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Midlight, QColor(247, 247, 247))
    palette.setColor(QPalette.ColorRole.Dark, QColor(160, 160, 160))
    palette.setColor(QPalette.ColorRole.Mid, QColor(200, 200, 200))
    palette.setColor(QPalette.ColorRole.Shadow, QColor(105, 105, 105))
    palette.setColor(QPalette.ColorRole.Link, QColor(0, 102, 204))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(51, 153, 255))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(150, 150, 150))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(150, 150, 150))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(150, 150, 150))
    return palette


def _build_dark_palette() -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Base, QColor(35, 35, 35))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(255, 255, 220))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(0, 0, 0))
    palette.setColor(QPalette.ColorRole.Text, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 0, 0))
    # Siehe Kommentar in _build_light_palette() - dieselben Rollen fehlten
    # hier ebenfalls.
    palette.setColor(QPalette.ColorRole.Light, QColor(90, 90, 90))
    palette.setColor(QPalette.ColorRole.Midlight, QColor(70, 70, 70))
    palette.setColor(QPalette.ColorRole.Dark, QColor(20, 20, 20))
    palette.setColor(QPalette.ColorRole.Mid, QColor(40, 40, 40))
    palette.setColor(QPalette.ColorRole.Shadow, QColor(10, 10, 10))
    palette.setColor(QPalette.ColorRole.Link, QColor(93, 173, 226))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(0, 0, 0))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(127, 127, 127))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(127, 127, 127))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(127, 127, 127))
    return palette


_PALETTES = {"light": _build_light_palette, "dark": _build_dark_palette}


def init_theme(app) -> None:
    """Muss einmalig beim App-Start aufgerufen werden (nach `QApplication(...)`,
    vor der ersten Fenster-Erzeugung).

    Setzt den `Fusion`-Stil, damit Light/Dark-Paletten auf allen Plattformen
    zuverlässig greifen (der native Windows-Stil ignoriert eine eigene
    `QPalette` für viele Widgets).
    """
    app.setStyle("Fusion")
    app.setPalette(_PALETTES[_current_theme]())
    # Globaler PyQtGraph-Default fuer neu erzeugte Widgets, BEVOR
    # `style_plot_container()` explizit greift - Fenster-Hintergrundfarbe
    # (siehe dort), nicht die Plotflaechen-Farbe.
    pg.setConfigOption("background", _PLOT_CONTAINER_COLORS[_current_theme])
    pg.setConfigOption("foreground", _PLOT_COLORS[_current_theme]["foreground"])


def get_theme() -> str:
    """Gibt das aktuelle Theme zurück ("light" oder "dark")."""
    return _current_theme


def curve_color() -> str:
    """Standard-Kurvenfarbe für neue Plots im aktuellen Theme."""
    return _PLOT_COLORS[_current_theme]["curve"]


def plot_foreground_color() -> str:
    """Standard-Vordergrundfarbe (Achsen/Text) im aktuellen Theme."""
    return _PLOT_COLORS[_current_theme]["foreground"]


def plot_background_color() -> str:
    """Standard-Hintergrundfarbe für neue Plots im aktuellen Theme.

    Fallback für Kanäle ohne individuell konfigurierte Hintergrundfarbe
    (siehe `gui/live_view.py::ChannelDisplayDialog`).
    """
    return _PLOT_COLORS[_current_theme]["background"]


def plot_container_background_color() -> str:
    """Hintergrundfarbe für den Plot-CONTAINER (alles außer der eigentlichen
    Plotfläche, siehe `_PLOT_CONTAINER_COLORS`) - für die Messwertanzeige-
    Boxen (siehe `gui/live_view.py`) UND `style_plot_container()`."""
    return _PLOT_CONTAINER_COLORS[_current_theme]


def is_theme_default_plot_background(color: str | None) -> bool:
    """Erkennt gespeicherte Plot-Hintergründe aus einem Theme-Default.

    Ältere Konfigurationen speichern den hellen Default als ``"w"``;
    spätere Versionen können die Hexwerte des hellen oder dunklen Themes
    enthalten. Diese Werte sind keine individuellen Kanalfarben und sollen
    bei einem Theme-Wechsel dem aktuellen Theme folgen.
    """
    return color in {"w", "#ffffff", "#232323"}


def style_plot_container(widget) -> None:
    """Setzt den Hintergrund eines PyQtGraph-Containers (`PlotWidget` oder
    `GraphicsLayoutWidget`) auf die Fenster-Hintergrundfarbe (siehe
    `plot_container_background_color()`) - NICHT auf die Plotflächen-Farbe,
    die bleibt der eigentlichen Datenfläche (ViewBox) vorbehalten."""
    widget.setBackground(_PLOT_CONTAINER_COLORS[_current_theme])


# PyQtGraph rendert Achsenticks standardmaessig in der App-Standard-
# Schriftgroesse - auf den eng gepackten Achsen der Live-/Analyse-Plots
# wirkt das unnoetig klein. `+2pt` relativ zur Standardgroesse (statt
# einer festen Punktgroesse), damit eine System-Schriftgroessen-Aenderung
# weiterhin respektiert wird.
_AXIS_TICK_FONT_SIZE_INCREASE = 2


def _axis_tick_font() -> QFont:
    font = QFont()
    font.setPointSize(font.pointSize() + _AXIS_TICK_FONT_SIZE_INCREASE)
    return font


def style_plot_item(plot_item) -> None:
    """Färbt Achsen und Titel eines einzelnen PyQtGraph-`PlotItem` im
    aktuellen Theme und vergrössert die Achsentick-Beschriftung leicht
    (siehe `_axis_tick_font`).

    Nötig, weil bereits erzeugte `PlotItem`s die globalen
    `pg.setConfigOption(...)`-Werte NICHT rückwirkend übernehmen - nur neu
    erzeugte Plots tun das automatisch. Der Titel (`addPlot(title=...)`)
    ist davon extra betroffen: er bleibt sonst in seiner ursprünglichen
    Farbe (z. B. Schwarz aus dem Hell-Theme) hängen, auch nachdem die
    Achsen bereits per `axis.setTextPen(...)` umgefärbt wurden.
    """
    foreground = _PLOT_COLORS[_current_theme]["foreground"]
    tick_font = _axis_tick_font()
    for axis_name in ("left", "bottom", "right", "top"):
        axis = plot_item.getAxis(axis_name)
        if axis is not None:
            axis.setPen(foreground)
            axis.setTextPen(foreground)
            axis.setTickFont(tick_font)
    if plot_item.titleLabel.text:
        plot_item.setTitle(plot_item.titleLabel.text, color=foreground)


def repolish(widget) -> None:
    """Erzwingt eine erneute Auswertung des Stylesheets eines Widgets."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


# ---------------------------------------------------------------------- #
# Einfache, selbst gezeichnete Navigations-Icons
# ---------------------------------------------------------------------- #
#
# `QStyle.standardIcon(...)` wäre die naheliegende Alternative, liefert
# für die hier gebrauchten Symbole aber FEST eingefärbte Pixmaps, die der
# Palette nicht folgen - ein Theme-Wechsel würde die Icon-Farbe nicht
# ändern. Diese Icons werden stattdessen mit der aktuellen
# `WindowText`-Farbe gezeichnet und bei jedem Theme-Wechsel neu erzeugt.


def nav_icon_color() -> QColor:
    """Aktuelle Vordergrundfarbe für Navigations-Icons (folgt der Palette)."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None:
        return app.palette().color(QPalette.ColorRole.WindowText)
    return QColor(0, 0, 0)


def _new_icon_pixmap(size: int) -> tuple[QPixmap, QPainter]:
    """Erzeugt eine leere Pixmap für ein selbst gezeichnetes Icon.

    Wird in Geräte-Pixeln (nicht logischen Pixeln) angelegt und mit dem
    aktuellen `devicePixelRatio()` markiert - sonst erscheinen die Icons bei
    Windows-Skalierung >100% (hier: 250%) sichtbar verpixelt, weil Qt die
    sonst zu kleine Pixmap beim Zeichnen hochskaliert. Zeichencode in den
    draw_*_icon()-Funktionen bleibt unverändert (nutzt weiterhin `size` in
    logischen Einheiten) - QPainter skaliert automatisch anhand des
    devicePixelRatio der Ziel-Pixmap.
    """
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    dpr = app.devicePixelRatio() if app is not None else 1.0
    physical_size = max(1, round(size * dpr))

    pixmap = QPixmap(physical_size, physical_size)
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    return pixmap, painter


def draw_gear_icon(size: int = 36) -> QPixmap:
    """Zahnrad-Symbol (Setup/Konfiguration).

    Blockige, rechteckige Zähne statt dünner Speichen - dünne Linien vom
    Kreis nach außen sehen bei kleiner Größe eher wie eine Sonne aus.
    """
    pixmap, painter = _new_icon_pixmap(size)
    color = nav_icon_color()
    center = size / 2
    body_radius = size * 0.30
    hole_radius = size * 0.13
    tooth_len = size * 0.11
    tooth_width = size * 0.15

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)

    # Zähne: kleine Rechtecke, radial um den Mittelpunkt verteilt.
    for i in range(8):
        painter.save()
        painter.translate(center, center)
        painter.rotate(i * 45)
        painter.drawRect(QRectF(body_radius, -tooth_width / 2, tooth_len, tooth_width))
        painter.restore()

    # Zahnkranz-Körper.
    painter.drawEllipse(QPointF(center, center), body_radius, body_radius)

    # Mittelloch ausstanzen, damit ein echter Ring entsteht.
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    painter.drawEllipse(QPointF(center, center), hole_radius, hole_radius)

    painter.end()
    return pixmap


def draw_play_icon(size: int = 36, y_offset: float = 0.0, color: QColor | None = None) -> QPixmap:
    """Play-Dreieck (Live View).

    `y_offset` erlaubt eine kleine vertikale Feinausrichtung für Buttons.
    `color` überschreibt die sonst theme-abhängige `nav_icon_color()` -
    nötig für Buttons mit fest codiertem (nicht theme-abhängigem)
    Hintergrund, z. B. den grünen Start-Button in `gui/setup_view.py`,
    wo Schwarz (Hell-Modus-Vordergrundfarbe) auf Dunkelgrün kaum sichtbar wäre.
    """
    pixmap, painter = _new_icon_pixmap(size)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color if color is not None else nav_icon_color())
    m = size * 0.24
    y = float(y_offset)
    triangle = QPolygonF(
        [
            QPointF(m, m * 0.7 + y),
            QPointF(m, size - m * 0.7 + y),
            QPointF(size - m * 0.75, size / 2 + y),
        ]
    )
    painter.drawPolygon(triangle)
    painter.end()
    return pixmap


def draw_trigger_icon(size: int = 36, y_offset: float = 0.0, color: QColor | None = None) -> QPixmap:
    """Blitz-Symbol für den Trigger-Scharf-Button (`_trigger_arm_button` in
    `gui/setup_view.py`/`gui/live_view.py`) - das gebräuchliche Zeichen für
    "Trigger" (z. B. auch bei Oszilloskopen).

    `y_offset`/`color` wie bei `draw_play_icon` - der Button hat einen fest
    codierten grauen Hintergrund unabhängig vom Theme, das Icon braucht
    daher ebenso IMMER Weiß statt der sonst theme-abhängigen
    `nav_icon_color()`.
    """
    pixmap, painter = _new_icon_pixmap(size)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color if color is not None else nav_icon_color())
    m = size * 0.14
    w = size - 2 * m
    h = size - 2 * m
    y = float(y_offset)
    bolt = QPolygonF(
        [
            QPointF(m + w * 0.58, m + y),
            QPointF(m + w * 0.14, m + h * 0.58 + y),
            QPointF(m + w * 0.42, m + h * 0.58 + y),
            QPointF(m + w * 0.32, m + h * 1.0 + y),
            QPointF(m + w * 0.86, m + h * 0.38 + y),
            QPointF(m + w * 0.55, m + h * 0.38 + y),
        ]
    )
    painter.drawPolygon(bolt)
    painter.end()
    return pixmap


def draw_magnifier_icon(size: int = 36) -> QPixmap:
    """Lupe mit einem schematischen Zeitgraphen im Inneren (Analyse).

    Der Graph ist auf das Lupenglas geclippt (liegt also wirklich "in" der
    Lupe, nicht nur daneben). Der Griff verläuft exakt radial vom
    Kreisrand nach außen (45°-Richtung von der Lupenmitte), statt versetzt
    tangential anzusetzen.
    """
    pixmap, painter = _new_icon_pixmap(size)
    color = nav_icon_color()

    lens_center = QPointF(size * 0.44, size * 0.44)
    lens_radius = size * 0.32

    # Schematischer Zeitgraph (Zickzack-Linie), auf das Lupenglas geclippt.
    inner_radius = lens_radius * 0.82
    clip_path = QPainterPath()
    clip_path.addEllipse(lens_center, inner_radius, inner_radius)
    painter.save()
    painter.setClipPath(clip_path)
    graph_pen = QPen(color)
    graph_pen.setWidthF(size * 0.045)
    graph_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    graph_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(graph_pen)
    r = inner_radius * 0.95
    cx, cy = lens_center.x(), lens_center.y()
    graph_points = [
        QPointF(cx - r, cy + r * 0.35),
        QPointF(cx - r * 0.45, cy - r * 0.55),
        QPointF(cx - r * 0.05, cy + r * 0.45),
        QPointF(cx + r * 0.45, cy - r * 0.65),
        QPointF(cx + r, cy + r * 0.05),
    ]
    for p1, p2 in zip(graph_points, graph_points[1:]):
        painter.drawLine(p1, p2)
    painter.restore()

    # Lupenrand.
    lens_pen = QPen(color)
    lens_pen.setWidthF(size * 0.09)
    lens_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(lens_pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(lens_center, lens_radius, lens_radius)

    # Griff: startet exakt auf dem Kreisrand und läuft radial (45°) weiter
    # nach außen - dieselbe Richtung wie die Linie von der Lupenmitte zum
    # Startpunkt, also kein Versatz/Tangente.
    angle = math.radians(45)
    direction = QPointF(math.cos(angle), math.sin(angle))
    handle_start = QPointF(
        lens_center.x() + lens_radius * direction.x(),
        lens_center.y() + lens_radius * direction.y(),
    )
    handle_length = size * 0.26
    handle_end = QPointF(
        handle_start.x() + handle_length * direction.x(),
        handle_start.y() + handle_length * direction.y(),
    )
    painter.drawLine(handle_start, handle_end)

    painter.end()
    return pixmap


def draw_plus_icon(size: int = 16) -> QPixmap:
    """Plus-Symbol für Aktionsbuttons (z. B. Kanal hinzufügen)."""
    pixmap, painter = _new_icon_pixmap(size)
    pen = QPen(nav_icon_color())
    pen.setWidthF(max(1.8, size * 0.14))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    c = size / 2
    m = size * 0.24
    painter.drawLine(QPointF(c, m), QPointF(c, size - m))
    painter.drawLine(QPointF(m, c), QPointF(size - m, c))
    painter.end()
    return pixmap


def draw_minus_icon(size: int = 16) -> QPixmap:
    """Minus-Symbol für Aktionsbuttons (z. B. Kanal entfernen)."""
    pixmap, painter = _new_icon_pixmap(size)
    pen = QPen(nav_icon_color())
    pen.setWidthF(max(1.8, size * 0.14))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    c = size / 2
    m = size * 0.24
    painter.drawLine(QPointF(m, c), QPointF(size - m, c))
    painter.end()
    return pixmap


def draw_stop_icon(size: int = 16, y_offset: float = 0.0) -> QPixmap:
    """Quadratisches Stop-Symbol für Abbruch-/Stop-Aktionen."""
    pixmap, painter = _new_icon_pixmap(size)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(nav_icon_color())
    m = size * 0.23
    side = size - 2 * m
    painter.drawRect(QRectF(m, m + y_offset, side, side))
    painter.end()
    return pixmap


def draw_ellipsis_icon(size: int = 16) -> QPixmap:
    """Drei-Punkte-Symbol ("…") als Hinweis, dass ein Button ein eigenes
    Auswahlfenster öffnet statt einer Direktaktion (z. B. Hardwarekanal-/
    Signaltyp-Auswahl in `gui/widgets/channel_table.py`)."""
    pixmap, painter = _new_icon_pixmap(size)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(nav_icon_color())
    dot_radius = max(1.0, size * 0.09)
    center_y = size / 2
    for i in (0.22, 0.5, 0.78):
        painter.drawEllipse(QPointF(size * i, center_y), dot_radius, dot_radius)
    painter.end()
    return pixmap


def draw_fft_icon(size: int = 36) -> QPixmap:
    """Spektrum-Symbol (Balken unterschiedlicher Höhe) für die FFT-Analyse."""
    pixmap, painter = _new_icon_pixmap(size)
    color = nav_icon_color()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)

    base_y = size * 0.82
    heights = [0.30, 0.55, 0.78, 0.45, 0.62, 0.22]
    bar_width = size * 0.10
    gap = size * 0.04
    total_width = len(heights) * bar_width + (len(heights) - 1) * gap
    x = (size - total_width) / 2
    for h in heights:
        bar_height = size * h
        painter.drawRect(QRectF(x, base_y - bar_height, bar_width, bar_height))
        x += bar_width + gap

    painter.end()
    return pixmap


def _draw_filter_response_icon(size: int, rising: bool) -> QPixmap:
    """Gemeinsame Basis für Tief-/Hochpass-Icons: schematischer
    Amplitudengang (Kurve, die auf einer Seite abfällt/ansteigt)."""
    pixmap, painter = _new_icon_pixmap(size)
    color = nav_icon_color()

    axis_pen = QPen(color)
    axis_pen.setWidthF(size * 0.045)
    axis_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(axis_pen)
    m = size * 0.16
    painter.drawLine(QPointF(m, size - m), QPointF(size - m, size - m))
    painter.drawLine(QPointF(m, size - m), QPointF(m, m))

    curve_pen = QPen(color)
    curve_pen.setWidthF(size * 0.09)
    curve_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    curve_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(curve_pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    high = size * 0.28
    low = size - m
    mid_x = size * 0.55
    path = QPainterPath()
    if rising:
        path.moveTo(m, low)
        path.lineTo(mid_x * 0.75, low)
        path.cubicTo(mid_x, low, mid_x * 1.05, high, size - m * 1.3, high)
    else:
        path.moveTo(m, high)
        path.lineTo(mid_x * 0.75, high)
        path.cubicTo(mid_x, high, mid_x * 1.05, low, size - m * 1.3, low)
    painter.drawPath(path)

    painter.end()
    return pixmap


def draw_lowpass_icon(size: int = 36) -> QPixmap:
    """Amplitudengang-Symbol für Tiefpassfilter (fällt nach rechts ab)."""
    return _draw_filter_response_icon(size, rising=False)


def draw_highpass_icon(size: int = 36) -> QPixmap:
    """Amplitudengang-Symbol für Hochpassfilter (steigt nach rechts an)."""
    return _draw_filter_response_icon(size, rising=True)


def draw_smoothing_icon(size: int = 36) -> QPixmap:
    """Symbol für den Glättungsfilter: verrauschte Linie über einer
    geglätteten Kurve."""
    pixmap, painter = _new_icon_pixmap(size)
    color = nav_icon_color()

    smooth_pen = QPen(color)
    smooth_pen.setWidthF(size * 0.09)
    smooth_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    smooth_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(smooth_pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    m = size * 0.16
    y_mid = size * 0.6
    smooth_path = QPainterPath()
    smooth_path.moveTo(m, y_mid + size * 0.12)
    smooth_path.cubicTo(size * 0.35, y_mid - size * 0.22, size * 0.65, y_mid + size * 0.22, size - m, y_mid - size * 0.12)
    painter.drawPath(smooth_path)

    noisy_pen = QPen(color)
    noisy_pen.setWidthF(size * 0.035)
    noisy_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    noisy_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(noisy_pen)
    y0 = size * 0.28
    jags = [0.30, 0.18, 0.36, 0.12, 0.32, 0.20, 0.34]
    step = (size - 2 * m) / (len(jags) - 1)
    noisy_path = QPainterPath()
    noisy_path.moveTo(m, y0 + size * jags[0])
    for i, j in enumerate(jags[1:], start=1):
        noisy_path.lineTo(m + step * i, y0 + size * j)
    painter.drawPath(noisy_path)

    painter.end()
    return pixmap


def set_theme(theme: str) -> None:
    """Setzt das Theme auf "light" oder "dark" und wendet es sofort an.

    Aktualisiert die `QApplication`-Palette (wirkt automatisch auf alle
    Standard-Qt-Widgets) und die globalen PyQtGraph-Farboptionen (wirkt auf
    ab jetzt neu erzeugte Plots) - und benachrichtigt registrierte Ansichten
    über `connect_theme_changed`, damit sie ihre bereits vorhandenen Plots
    selbst nachfärben.
    """
    global _current_theme
    if theme not in _PALETTES or theme == _current_theme:
        return
    _current_theme = theme

    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None:
        app.setPalette(_PALETTES[theme]())

    pg.setConfigOption("background", _PLOT_CONTAINER_COLORS[theme])
    pg.setConfigOption("foreground", _PLOT_COLORS[theme]["foreground"])

    _get_signals().theme_changed.emit(theme)


# ---------------------------------------------------------------------- #
# Live-Umschalt-Signal
# ---------------------------------------------------------------------- #
#
# Lazy konstruiert aus demselben Grund wie in `gui/i18n.py`: dieses Modul
# kann importiert werden, bevor `QApplication` existiert.

_signals: Optional["_ThemeSignals"] = None


def _get_signals() -> "_ThemeSignals":
    global _signals
    if _signals is None:
        from PyQt6.QtCore import QObject, pyqtSignal

        class _ThemeSignals(QObject):
            theme_changed = pyqtSignal(str)

        _signals = _ThemeSignals()
    return _signals


def connect_theme_changed(slot: Callable[[], None]) -> None:
    """Registriert `slot` (typischerweise `view.retheme_plots`), der bei
    jedem Theme-Wechsel aufgerufen wird."""
    _get_signals().theme_changed.connect(slot)
