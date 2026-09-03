"""
tests/test_update_checker.py

Tests for `core/update_checker.py`.

Deliberately does NOT need a `QApplication`/Qt: the module is plain
stdlib (`urllib`) - see its module docstring for why - so
`check_for_update()` is exercised here with `urllib.request.urlopen`
mocked out, instead of hitting the real GitHub API.
"""

from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from core.update_checker import (
    UpdateCheckResult,
    check_for_update,
    is_newer_version,
    parse_version,
)


class ParseVersionTest(unittest.TestCase):
    def test_plain_version(self) -> None:
        self.assertEqual(parse_version("0.5.0"), (0, 5, 0))

    def test_leading_v_is_stripped(self) -> None:
        self.assertEqual(parse_version("v0.5.0"), (0, 5, 0))

    def test_non_numeric_suffix_is_ignored(self) -> None:
        # "-beta" carries no digits of its own, so it simply contributes
        # nothing to the parsed tuple.
        self.assertEqual(parse_version("v1.2.3-beta"), (1, 2, 3))

    def test_garbage_falls_back_to_zero(self) -> None:
        self.assertEqual(parse_version("not-a-version"), (0,))


class IsNewerVersionTest(unittest.TestCase):
    def test_newer_patch(self) -> None:
        self.assertTrue(is_newer_version("v0.5.1", "0.5.0"))

    def test_older_patch(self) -> None:
        self.assertFalse(is_newer_version("v0.4.0", "0.5.0"))

    def test_equal_version_is_not_newer(self) -> None:
        self.assertFalse(is_newer_version("v0.5.0", "0.5.0"))

    def test_double_digit_component_compares_numerically(self) -> None:
        # A plain string compare would rank "0.5.10" below "0.5.9"
        # ("1" < "9" as characters) - the int-tuple comparison must not.
        self.assertTrue(is_newer_version("v0.5.10", "0.5.9"))

    def test_missing_trailing_component_treated_as_zero(self) -> None:
        self.assertFalse(is_newer_version("v0.5", "0.5.0"))
        self.assertTrue(is_newer_version("v0.6", "0.5.0"))


class CheckForUpdateTest(unittest.TestCase):
    def _mock_response(self, payload: dict) -> io.BytesIO:
        # `BytesIO` already supports `with` on its own (like every stdlib
        # IO object: `__enter__` returns self, `__exit__` closes it) -
        # exactly the interface `urlopen()`'s real return value offers,
        # so no extra wrapping is needed to stand in for it.
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    def test_update_available(self) -> None:
        payload = {
            "tag_name": "v9.9.9",
            "html_url": "https://github.com/TUHH-IPMT/ezDAQ/releases/tag/v9.9.9",
        }
        with patch("urllib.request.urlopen", return_value=self._mock_response(payload)):
            result = check_for_update(current_version="0.5.0")

        self.assertEqual(
            result,
            UpdateCheckResult(
                update_available=True,
                current_version="0.5.0",
                latest_version="v9.9.9",
                release_url="https://github.com/TUHH-IPMT/ezDAQ/releases/tag/v9.9.9",
            ),
        )

    def test_already_up_to_date(self) -> None:
        payload = {
            "tag_name": "v0.5.0",
            "html_url": "https://github.com/TUHH-IPMT/ezDAQ/releases/tag/v0.5.0",
        }
        with patch("urllib.request.urlopen", return_value=self._mock_response(payload)):
            result = check_for_update(current_version="0.5.0")

        self.assertFalse(result.update_available)

    def test_network_failure_propagates(self) -> None:
        """`check_for_update()` deliberately does not swallow errors (see
        its docstring) - the caller (`gui/main_window.py`) needs to tell
        a failed check apart from a genuine "up to date" result."""
        with patch("urllib.request.urlopen", side_effect=OSError("no network")):
            with self.assertRaises(OSError):
                check_for_update(current_version="0.5.0")


if __name__ == "__main__":
    unittest.main()
