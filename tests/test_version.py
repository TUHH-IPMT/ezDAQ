"""
tests/test_version.py

Keeps the application version consistent across the places that show it.

`config/settings.py::APP_VERSION` is the single source of truth. The
About dialog reads it directly (`gui/main_window.py::_on_about`), but
`packaging/ezDAQ.iss` cannot - Inno Setup has no way to import
Python, so the value is duplicated there. That duplication is the whole
reason this test exists: an installer announcing a different version
than the running application is the kind of mismatch nobody notices
until a user reports a bug against the wrong build.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from config.settings import APP_VERSION

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_INNO_SCRIPT = _PROJECT_ROOT / "packaging" / "ezDAQ.iss"


class AppVersionTest(unittest.TestCase):
    def test_version_looks_like_a_version(self) -> None:
        self.assertRegex(APP_VERSION, r"^\d+(\.\d+)+$")

    def test_inno_script_exists_where_the_readme_says(self) -> None:
        """The README documents this path in the build command - a moved
        file would make the instructions wrong AND silently disable the
        version check below."""
        self.assertTrue(_INNO_SCRIPT.is_file(), _INNO_SCRIPT)

    def test_inno_script_version_matches_app_version(self) -> None:
        script = _INNO_SCRIPT.read_text(encoding="utf-8")
        match = re.search(r'#define\s+AppVersion\s+"([^"]+)"', script)
        self.assertIsNotNone(match, "no AppVersion defined in ezDAQ.iss")

        self.assertEqual(
            match.group(1),
            APP_VERSION,
            "packaging/ezDAQ.iss and config/settings.py::APP_VERSION "
            "disagree - bump both together",
        )


class AboutDialogVersionTest(unittest.TestCase):
    """The About text carries the version through a `{version}`
    placeholder rather than a hardcoded number, in both languages."""

    def test_both_languages_use_the_placeholder(self) -> None:
        from gui.i18n import _translations

        for language in ("de", "en"):
            with self.subTest(language=language):
                body = _translations[language]["about_body"]
                self.assertIn("{version}", body)

    def test_rendered_text_contains_the_version(self) -> None:
        from gui.i18n import set_language, get_language, t

        original = get_language()
        try:
            for language in ("de", "en"):
                with self.subTest(language=language):
                    set_language(language)
                    rendered = t("about_body", version=APP_VERSION)
                    self.assertIn(f"Version {APP_VERSION}", rendered)
                    self.assertNotIn("{version}", rendered)
        finally:
            set_language(original)


if __name__ == "__main__":
    unittest.main()
