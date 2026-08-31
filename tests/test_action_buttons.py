"""
tests/test_action_buttons.py

Guards that the play/record/stop buttons actually LOOK unavailable when
they are disabled, in both themes.

`gui/theme.py::action_button_style` used to set `color:` in the plain
`QPushButton {}` block. A stylesheet color there applies in every state
and overrides Qt's automatic `QPalette.ColorGroup.Disabled` handling, so
a disabled stop button kept its full-strength label and looked perfectly
clickable. The fix was to drop that line - which is easy to reintroduce
by accident, hence this test.

Measured rather than asserted on the stylesheet text: what matters is
the rendered result, and a future refactor might restore the same defect
through a different property.
"""

from __future__ import annotations

import unittest

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication, QPushButton

# Module-level reference, see tests/test_axis_labels.py for why.
_APP: QApplication | None = None


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def _force_theme(theme: str) -> None:
    """`set_theme()` returns early when the theme is already active, which
    on a fresh QApplication leaves the palette unset - toggle first."""
    from gui.theme import set_theme

    set_theme("dark" if theme == "light" else "light")
    set_theme(theme)


def _label_contrast(style: str, enabled: bool) -> int:
    """Strongest lightness difference between the button's label and its
    own background - a rough but stable measure of how readable the text
    is."""
    app = _app()
    button = QPushButton("Stop")
    button.setStyleSheet(style)
    button.setEnabled(enabled)
    button.resize(120, 40)
    button.show()
    app.processEvents()

    image = button.grab().toImage()
    background = QColor(image.pixel(5, 20)).lightness()
    contrast = max(
        abs(QColor(image.pixel(x, y)).lightness() - background)
        for x in range(20, 100)
        for y in range(12, 28)
    )

    button.close()
    button.deleteLater()
    app.processEvents()
    return contrast


class DisabledActionButtonTest(unittest.TestCase):
    def setUp(self) -> None:
        from gui.theme import get_theme

        _app()
        self._original_theme = get_theme()

    def tearDown(self) -> None:
        _force_theme(self._original_theme)

    def test_disabled_label_is_dimmed_in_both_themes(self) -> None:
        from gui.theme import action_button_style

        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                _force_theme(theme)
                style = action_button_style()

                enabled = _label_contrast(style, enabled=True)
                disabled = _label_contrast(style, enabled=False)

                self.assertLess(
                    disabled,
                    enabled * 0.75,
                    f"disabled label barely dimmed ({disabled} vs {enabled}) - "
                    f"is `color:` set in the plain QPushButton block again?",
                )

    def test_arm_button_disabled_label_is_dimmed(self) -> None:
        """Same construction, same trap."""
        from gui.theme import trigger_arm_button_style

        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                _force_theme(theme)
                style = trigger_arm_button_style()

                enabled = _label_contrast(style, enabled=True)
                disabled = _label_contrast(style, enabled=False)

                self.assertLess(disabled, enabled * 0.75)

    def test_checked_arm_button_still_gets_its_accent_text_color(self) -> None:
        """Dropping the base `color:` must not take the armed state's
        readable text on the accent background with it."""
        from gui.theme import trigger_arm_button_style

        _force_theme("dark")
        style = trigger_arm_button_style()

        self.assertIn("QPushButton:checked", style)
        checked_block = style.split("QPushButton:checked", 1)[1]
        self.assertIn("palette(highlighted-text)", checked_block)


if __name__ == "__main__":
    unittest.main()
