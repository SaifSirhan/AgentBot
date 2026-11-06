"""Pure-terminal UI contract tests (offscreen Qt).

Run: python -m unittest tests.test_gui_terminal -v
"""
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

import agent_gui_qt as g  # noqa: E402

QSS = Path(__file__).parent.parent / "themes" / "holographic_dark.qss"


def rule_body(selector):
    """Return the declaration block QSS applies to an exact selector."""
    text = QSS.read_text(encoding="utf-8")
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", text)
    return match.group(1) if match else ""


class TerminalChromeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = g.AgentWindow()
        self.window.show()
        self.app.processEvents()
        self.input_field = self.window.input_field

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_prompt_row_carries_no_buttons(self):
        self.assertFalse(hasattr(self.window, "send_button"),
                         "the send button must be gone")
        self.assertFalse(hasattr(self.window, "settings_button"),
                         "settings moves to the /settings command")
        self.assertFalse(
            self.window.input_pill.findChildren(QPushButton),
            "no control may sit in the prompt row")

    def test_title_bar_has_no_settings_button(self):
        self.assertFalse(hasattr(self.window._title_bar, "settings_button"))
        for name in ("min_button", "max_button", "close_button"):
            self.assertTrue(hasattr(self.window._title_bar, name), name)

    def test_prompt_row_is_flat_not_a_pill(self):
        body = rule_body("QFrame#promptRow")
        radius = re.search(r"border-radius:\s*([^;]+)", body)
        self.assertIsNotNone(body, "promptRow still has a rule")
        self.assertEqual(
            (radius.group(1).strip() if radius else "0"), "0px",
            "the rounded pill must be flattened")

    def test_prompt_row_has_no_divider(self):
        body = rule_body("QFrame#promptRow")
        self.assertNotRegex(body, r"\bborder-top\s*:")
        self.assertNotRegex(body, r"\boutline\s*:")
        self.assertIn("border: none", body)
        self.assertIn("background-color: transparent", body,
                      "the prompt must blend into the canvas")
        self.assertEqual(rule_body("QFrame#promptRow:focus"), "",
                         "focus must not reintroduce a top border")
        self.assertEqual(rule_body("QFrame#promptRow:hover"), "",
                         "hover must not reintroduce a top border")

    def test_canvas_is_matte_black(self):
        self.assertEqual(g.COLOR_BG.upper(), "#0B0E13")
        self.assertIn("#0B0E13", rule_body("QMainWindow, QDialog"))

    def test_cursor_blinks(self):
        # Asserted on the constant: main() is what applies it, and the
        # offscreen platform's default flash time would hide a frozen caret.
        self.assertGreater(g.CURSOR_FLASH_MS, 0)
        self.assertGreaterEqual(self.input_field.cursorWidth(), 6)

    def test_background_scanlines_bleed_through(self):
        layer = self.window.chat.scanlines
        self.assertIs(layer.parentWidget(), self.window.chat.viewport())
        self.assertTrue(
            layer.testAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents),
            "the layer must not eat clicks")
        self.assertLessEqual(layer.ALPHA, 16, "lines must stay faint")
        self.assertEqual(layer.PERIOD, 4)

    def test_chat_text_is_painted_over_the_canvas(self):
        message = g.UserMessage("the quick brown fox")
        self.window.chat.add_message(message, fade=False)
        self.app.processEvents()
        top = message.mapTo(self.window, QPoint(0, 0))
        shot = self.window.grab().toImage()
        scale = shot.width() / float(self.window.width())
        brightest = 0
        for y in range(max(0, top.y()), top.y() + max(1, message.height())):
            for x in range(top.x(), top.x() + min(220, message.width())):
                c = shot.pixelColor(int(x * scale), int(y * scale))
                brightest = max(brightest, c.red() + c.green() + c.blue())
        self.assertGreater(brightest, 200, "chat text is not being painted")

    def test_chat_text_stays_visible_above_the_scanlines(self):
        """The lines are canvas paint, so a message drawn over them must still
        read bright — an overlay child used to erase the text behind it."""
        message = g.UserMessage("the quick brown fox")
        self.window.chat.add_message(message, fade=False)
        self.app.processEvents()
        shot = self.window.grab().toImage()
        top = message.mapTo(self.window, QPoint(0, 0))
        brightest = 0
        for y in range(top.y(), top.y() + max(1, message.height())):
            for x in range(top.x(), top.x() + min(200, message.width())):
                c = shot.pixelColor(x, y)
                brightest = max(brightest, c.red() + c.green() + c.blue())
        self.assertGreater(brightest, 200, "chat text is hidden by the canvas")

    def test_settings_still_reachable_by_command(self):
        from PySide6.QtWidgets import QFrame

        self.window._handle_slash_command("/settings")
        self.app.processEvents()
        panel = self.window.findChild(QFrame, "settingsPanel")
        self.assertIsNotNone(panel, "/settings must open the panel")
        self.assertTrue(panel.isVisible())


class CopyNoticeTests(unittest.TestCase):
    """Clipboard feedback is a transient terminal notice, not a chat message."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = g.AgentWindow()
        self.window.show()
        self.app.processEvents()
        self.window.chat.chat_log.append(("user", "hello"))
        self.window.chat.chat_log.append(("agent", "hi there"))

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def _notices(self):
        return [w for w in self.window.chat._body.children()
                if isinstance(w, g.SystemMessage)]

    def test_copy_all_leaves_no_transcript_message(self):
        before = list(self.window.chat.chat_log)
        with mock.patch.object(type(self.window), "NOTICE_MS", 20):
            self.window.copy_all()
            self.app.processEvents()
            self.assertEqual(len(self._notices()), 1, "a notice should show")
            QTest.qWait(120)
        self.assertEqual(self._notices(), [], "the notice must fade out")
        self.assertEqual(self.window.chat.chat_log, before,
                         "the notice must not enter the transcript")

    def test_clipboard_still_receives_the_chat(self):
        self.window.copy_all()
        self.assertIn("hello", QApplication.clipboard().text())


if __name__ == "__main__":
    unittest.main()
