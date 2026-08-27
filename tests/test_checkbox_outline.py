"""
tests/test_checkbox_outline.py

Check boxes must have a visible box, not just a check mark.

Fusion derives the indicator border from the palette's Window color and
LIGHTENS it. On the dark theme that lands on a readable gray; on the
light theme #f0f0f0 lightens to white and the border vanishes into the
indicator's own white fill - a ticked box showed only its check mark, an
unticked one looked like an empty cell. `gui/theme.py` therefore strokes
the outline itself via a QProxyStyle.

Measured on the rendered widget rather than asserted on the style class:
the point is what ends up on screen, and the same defect could return
through a different route.
"""

from __future__ import annotations

import unittest
from collections import Counter

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication, QCheckBox, QHBoxLayout, QWidget

# Module-level reference, see tests/test_axis_labels.py for why.
_APP: QApplication | None = None


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
        from gui.theme import init_theme

        init_theme(_APP)
    return _APP


def _contrast(a: str, b: str) -> float:
    def luminance(hex_color: str) -> float:
        parts = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        parts = [
            c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in parts
        ]
        return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]

    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _force_theme(theme: str) -> None:
    from gui.theme import set_theme

    set_theme("dark" if theme == "light" else "light")
    set_theme(theme)


def _render_unchecked_box(field_color: str) -> Counter:
    """Colors of an unchecked check box drawn on a `field_color` field."""
    app = _app()
    host = QWidget()
    host.setAutoFillBackground(True)
    palette = host.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor(field_color))
    host.setPalette(palette)
    layout = QHBoxLayout(host)
    layout.setContentsMargins(4, 4, 4, 4)
    layout.addWidget(QCheckBox())
    host.resize(40, 28)
    host.show()
    app.processEvents()

    image = host.grab().toImage()
    counts = Counter(
        QColor(image.pixel(x, y)).name()
        for x in range(image.width())
        for y in range(image.height())
    )
    host.close()
    host.deleteLater()
    app.processEvents()
    return counts


class CheckBoxOutlineTest(unittest.TestCase):
    def tearDown(self) -> None:
        _force_theme("light")

    def test_outline_contrasts_with_the_field_in_both_themes(self) -> None:
        from PyQt6.QtGui import QPalette as P
        from gui.theme import checkbox_outline_color

        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                _force_theme(theme)
                field = _app().palette().color(P.ColorRole.Base).name()

                self.assertGreaterEqual(
                    _contrast(checkbox_outline_color().name(), field),
                    3.0,
                    "outline would be hard to make out against the field",
                )

    def test_an_unchecked_box_is_actually_drawn_in_the_light_theme(self) -> None:
        """The regression itself: on white, Fusion alone painted nothing
        but fill and background."""
        _force_theme("light")

        counts = _render_unchecked_box("#ffffff")
        distinct = [
            color for color, n in counts.items() if n >= 20 and color != "#ffffff"
        ]

        self.assertTrue(
            distinct,
            "nothing but the field color was painted - the box is invisible",
        )

    def test_the_dark_theme_still_draws_a_box(self) -> None:
        _force_theme("dark")

        counts = _render_unchecked_box("#232323")
        distinct = [
            color for color, n in counts.items() if n >= 20 and color != "#232323"
        ]

        self.assertTrue(distinct)


if __name__ == "__main__":
    unittest.main()
