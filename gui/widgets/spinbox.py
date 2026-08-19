"""
gui/widgets/spinbox.py

`PrecisionDoubleSpinBox`: a `QDoubleSpinBox` variant without an artificial
decimal-places limit when typing/pasting, with "clean" display (no
superfluous trailing zeros) and without the German system locale as a
pitfall when pasting values that use a dot as the decimal separator.

Background (see the channel parameter dialog in `gui/widgets/channel_table.py`):
    A normal `QDoubleSpinBox` couples two independent things together via
    `setDecimals()`: how many decimal places are accepted at all when
    typing AND how many are shown when displaying (always exactly that
    many, padded with trailing zeros). A low value (e.g. 4) artificially
    limits input precision; a high value, on the other hand, makes even a
    value like 2.0 appear as "2.0000000000".

    In addition, a `QDoubleSpinBox` without an explicit locale uses the
    system locale (`QLocale.system()`) to parse typed/pasted text - under
    a German Windows locale ("," as the decimal separator, "." as the
    thousands separator), a pasted value like "1.5" is therefore NOT
    interpreted as 1.5, but as 1500 (!) - a pasted dot-decimal value is
    silently corrupted by a factor of 1000, without any error message.

    So that, despite the enforced "C" locale (dot display), the German
    typing habit ("," as the decimal separator) still works, an entered/
    pasted "," is transparently treated as "." during validation - the
    display itself, however, always stays with "." (see `textFromValue`).
"""

from __future__ import annotations

from PyQt6.QtCore import QLocale
from PyQt6.QtGui import QValidator
from PyQt6.QtWidgets import QDoubleSpinBox, QSpinBox, QWidget


class _NoWheelMixin:
    """Ignores mouse wheel events instead of changing the value.

    By default, a spinbox field in Qt also reacts to the mouse wheel even
    WITHOUT focus - if you scroll over a longer form, for example, and the
    mouse pointer happens to pass over a number field, its value changes
    unnoticed instead of the page scrolling further. The event is
    deliberately ignored here (not just "do nothing"), so that it is
    passed through to the surrounding scroll widget (e.g. `QScrollArea`)
    and the page scrolls normally.
    """

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt API
        event.ignore()


class NoWheelSpinBox(_NoWheelMixin, QSpinBox):
    """`QSpinBox` that ignores mouse wheel events - see `_NoWheelMixin`."""


class NoWheelDoubleSpinBox(_NoWheelMixin, QDoubleSpinBox):
    """`QDoubleSpinBox` that ignores mouse wheel events - see `_NoWheelMixin`."""


class GroupedDoubleSpinBox(NoWheelDoubleSpinBox):
    """`QDoubleSpinBox` with thousands-separator display
    (`setGroupSeparatorShown(True)`) that can still be edited normally
    when deleting individual digits.

    With separator display enabled, Qt's standard validator completely
    rejects some intermediate editing states (not just marking them as
    incomplete) instead of treating them as a valid intermediate step -
    e.g. deleting the leading "1" from "1.000,0" produces the text
    ".000,0" (an "orphaned" separator right at the start), which Qt
    rejects as `Invalid`. This makes it appear as if the user cannot
    delete the digit at all - every deletion attempt is silently
    discarded. So all separators are stripped from the intermediate text
    before the actual validation - Qt automatically restores correct
    grouping on the next commit (e.g. loss of focus) via `textFromValue()`
    anyway.
    """

    def validate(self, text: str, pos: int) -> tuple[QValidator.State, str, int]:  # noqa: N802
        separator = self.locale().groupSeparator()
        if not separator or separator not in text:
            return super().validate(text, pos)
        removed_before_pos = text[:pos].count(separator)
        cleaned = text.replace(separator, "")
        return super().validate(cleaned, max(0, pos - removed_before_pos))


class PrecisionDoubleSpinBox(NoWheelDoubleSpinBox):
    """`QDoubleSpinBox` with high input precision, clean display, and dot
    OR comma as the decimal separator when typing/pasting, independent of
    the system locale (the display itself always uses a dot).

    `decimals` (default 10) now only sets the *maximum* accepted input
    precision - `textFromValue()` still always shows the shortest possible
    representation without superfluous trailing zeros.
    """

    def __init__(self, decimals: int = 10, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # The "C" locale forces "." as the decimal separator when parsing
        # typed/pasted text, independent of the system locale (see the
        # module docstring) - must be set BEFORE setDecimals()/setValue()
        # so the internal validator uses it from the start.
        self.setLocale(QLocale(QLocale.Language.C))
        self.setDecimals(decimals)

    def validate(self, text: str, pos: int) -> tuple[QValidator.State, str, int]:  # noqa: N802
        # Treat "," like "." BEFORE the standard validator (set to the
        # "." locale) checks - otherwise a typed/pasted "," would be
        # rejected as an invalid character. As a result, the display jumps
        # to "." immediately while typing, which is deliberately accepted
        # here (see the class docstring).
        return super().validate(text.replace(",", "."), pos)

    def valueFromText(self, text: str) -> float:  # noqa: N802 - Qt API
        # Extra safeguard (e.g. for a programmatic setText() that bypasses
        # the validate() path) - see validate().
        return super().valueFromText(text.replace(",", "."))

    def textFromValue(self, value: float) -> str:  # noqa: N802 - Qt API
        text = f"{value:.{self.decimals()}f}"
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text


def parse_optional_float(text: str) -> float | None:
    """Converts freely entered text (e.g. an editable table/tree cell
    without a `QDoubleSpinBox`, see `gui/sensor_database_dialog.py`) into
    an optional float - both comma AND dot are accepted as the decimal
    separator (same principle as `PrecisionDoubleSpinBox`, see the module
    docstring above); empty text results in `None` instead of an error."""
    text = text.strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def format_optional_float(value: float | None) -> str:
    """Reverses `parse_optional_float` - `None` is rendered as empty text,
    otherwise without superfluous trailing zeros (see
    `PrecisionDoubleSpinBox.textFromValue`)."""
    if value is None:
        return ""
    text = f"{value:.10f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text
