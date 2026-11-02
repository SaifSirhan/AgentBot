"""Theme registry and QSS integrity (offscreen Qt).

A QSS typo is invisible at load time — Qt logs a warning and renders the widget
unstyled — so the themes are parsed here with a message handler installed, and
the code-side palette is checked against the default theme file it must track.

Run: python -m unittest tests.test_gui_theme -v
"""
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtCore import qInstallMessageHandler  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

import agent_gui_qt as g  # noqa: E402

_PARSE_ERROR = re.compile(
    r"(could not parse|error parsing|unknown property|invalid property)",
    re.IGNORECASE)


def stylesheet_problems(app, qss):
    """Qt messages that mean part of the stylesheet was silently dropped."""
    seen = []
    previous = qInstallMessageHandler(
        lambda mode, context, text: seen.append(text))
    try:
        app.setStyleSheet(qss)
        probe = QWidget()
        probe.resize(240, 160)
        probe.show()
        app.processEvents()
        probe.close()
        probe.deleteLater()
        app.processEvents()
    finally:
        qInstallMessageHandler(previous)
        app.setStyleSheet("")
        app.processEvents()
    return [text for text in seen if _PARSE_ERROR.search(text)]


class ThemeRegistryTests(unittest.TestCase):
    def test_holographic_dark_is_registered_first(self):
        self.assertEqual(g.THEME_NAMES[0], "holographic_dark")
        self.assertIn("brutalist", g.THEME_NAMES)
        self.assertIn("crt_green", g.THEME_NAMES)

    def test_default_is_holographic_dark_when_config_is_silent(self):
        with mock.patch.object(g, "read_config_file_raw", return_value={}):
            self.assertEqual(g.read_theme_name(), "holographic_dark")

    def test_a_persisted_theme_still_wins(self):
        with mock.patch.object(
                g, "read_config_file_raw",
                return_value={"GUI_THEME": "brutalist"}):
            self.assertEqual(g.read_theme_name(), "brutalist")

    def test_an_unknown_theme_falls_back_to_the_default(self):
        with mock.patch.object(
                g, "read_config_file_raw",
                return_value={"GUI_THEME": "solar_pumpkin"}):
            self.assertEqual(g.read_theme_name(), "holographic_dark")

    def test_stylesheet_for_an_unknown_name_falls_back(self):
        self.assertEqual(g.theme_stylesheet("solar_pumpkin"),
                         g.theme_stylesheet(g.DEFAULT_THEME))

    def test_every_registered_theme_has_a_file(self):
        for name in g.THEME_NAMES:
            path = g.THEMES_DIR / f"{name}.qss"
            self.assertTrue(path.is_file(), path)
            self.assertTrue(path.read_text(encoding="utf-8").strip(), path)

    def test_cycling_reaches_every_theme(self):
        # The header button walks THEME_NAMES by index; a gap would strand one.
        visited = {g.THEME_NAMES[(i + 1) % len(g.THEME_NAMES)]
                   for i in range(len(g.THEME_NAMES))}
        self.assertEqual(visited, set(g.THEME_NAMES))


class PaletteDriftTests(unittest.TestCase):
    """The inline constants must match the default theme they stand in for."""

    def setUp(self):
        self.qss = g.theme_stylesheet(g.DEFAULT_THEME).upper()

    def assert_in_theme(self, color, label):
        self.assertIn(color.upper(), self.qss,
                      f"{label} {color} is not in {g.DEFAULT_THEME}.qss")

    def test_canvas_and_accent_agree_with_the_theme_file(self):
        self.assert_in_theme(g.COLOR_BG, "canvas")
        self.assert_in_theme(g.COLOR_ACCENT, "accent")
        self.assert_in_theme(g.COLOR_RAISED, "raised surface")
        self.assert_in_theme(g.COLOR_SIDEBAR, "sidebar")

    def test_palette_is_dark_with_a_bright_accent(self):
        def channels(hex_color):
            value = hex_color.lstrip("#")
            return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))

        for name in ("COLOR_BG", "COLOR_BG_ALT", "COLOR_SIDEBAR",
                     "COLOR_RAISED"):
            red, green, blue = channels(getattr(g, name))
            self.assertLess(max(red, green, blue), 40, f"{name} is not dark")

        accent = channels(g.COLOR_ACCENT)
        self.assertGreater(max(accent), 200, "the accent needs a hot channel")
        # Neon means contrast against the canvas, not just a light colour:
        # cyan scores low on brightness-summed-by-eye without this.
        self.assertGreater(sum(accent), 4 * sum(channels(g.COLOR_BG)),
                           "the accent must separate from the canvas")


class StylesheetParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_default_theme_parses_without_dropped_rules(self):
        problems = stylesheet_problems(
            self.app, g.theme_stylesheet(g.DEFAULT_THEME))
        self.assertEqual(problems, [], "\n".join(problems))

    def test_existing_themes_still_parse(self):
        for name in g.THEME_NAMES:
            with self.subTest(theme=name):
                self.assertEqual(
                    stylesheet_problems(self.app, g.theme_stylesheet(name)),
                    [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
