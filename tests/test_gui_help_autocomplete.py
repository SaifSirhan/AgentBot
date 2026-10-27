"""Tests for the /help manual and inline command completion (offscreen).

Run: python -m unittest tests.test_gui_help_autocomplete -v
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QTextCursor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

import agent_gui_qt as g  # noqa: E402


class HelpMessageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_renders_plain_text_and_preserves_newlines(self):
        message = g.HelpMessage(g.HELP_TEXT)
        labels = message.findChildren(QLabel)
        self.assertEqual(len(labels), 1)
        label = labels[0]
        self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(label.text(), g.HELP_TEXT)
        self.assertEqual(
            label.text().count("\n"), g.HELP_TEXT.count("\n"))
        self.assertGreater(label.text().count("\n"), 20)
        self.assertTrue(label.wordWrap())
        self.assertEqual(label.minimumWidth(), 0)
        self.assertTrue(label.alignment() & Qt.AlignmentFlag.AlignLeft)
        self.assertTrue(label.alignment() & Qt.AlignmentFlag.AlignTop)
        self.assertEqual(message.raw_text, g.HELP_TEXT)


class HelpCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = g.AgentWindow()
        for _ in range(10):
            self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_help_command_adds_plain_text_manual(self):
        self.assertTrue(self.window._handle_slash_command("/help"))
        widgets = [
            self.window.chat._layout.itemAt(index).widget()
            for index in range(self.window.chat._layout.count())
        ]
        manuals = [w for w in widgets if isinstance(w, g.HelpMessage)]
        self.assertEqual(len(manuals), 1)
        label = manuals[0].findChildren(QLabel)[0]
        self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(label.text(), g.HELP_TEXT)


class TerminalInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.input = g.TerminalInput()
        self.input.resize(480, 48)
        self.input.show()
        self.app.processEvents()

    def tearDown(self):
        self.input.hide()
        self.input.deleteLater()
        self.app.processEvents()

    # -- helpers --------------------------------------------------------
    def type(self, text):
        QTest.keyClicks(self.input, text)

    def press(self, key):
        QTest.keyClick(self.input, key)

    def set_text(self, text):
        self.input.setPlainText(text)
        cursor = self.input.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.input.setTextCursor(cursor)
        self.app.processEvents()
        return cursor

    # -- prefix completion ---------------------------------------------
    def test_prefix_match_paints_suffix_ghost(self):
        self.type("/set")
        self.assertEqual(self.input.toPlainText(), "/set")
        self.assertEqual(self.input._ghost, "tings")
        self.assertFalse(self.input._replace)
        self.assertFalse(self.input.grab().isNull())
        self.app.processEvents()

    def test_right_arrow_accepts_prefix_ghost(self):
        self.type("/set")
        self.press(Qt.Key.Key_Right)
        self.assertEqual(self.input.toPlainText(), "/settings")
        self.assertEqual(self.input._ghost, "")
        self.assertEqual(
            self.input.textCursor().position(),
            len(self.input.toPlainText()))

    def test_exact_command_has_no_ghost(self):
        self.type("/help")
        self.assertEqual(self.input._ghost, "")
        self.assertEqual(self.input._matches, [])

    # -- tab cycling ----------------------------------------------------
    def test_tab_cycles_prefix_matches_then_fuzzy_matches(self):
        self.type("/t")
        self.assertEqual(self.input._ghost, "asks")
        self.press(Qt.Key.Key_Tab)
        self.assertEqual(self.input._ghost, "heme")
        self.press(Qt.Key.Key_Tab)
        self.assertEqual(self.input._ghost, "g-clear")
        self.press(Qt.Key.Key_Tab)
        self.assertTrue(self.input._replace)
        self.assertEqual(
            self.input._ghost,
            "  \u2192 " + self.input._matches[3])

        # Keep cycling until the list wraps back to the first match.
        for _ in range(len(self.input._matches) - 3):
            self.press(Qt.Key.Key_Tab)
        self.assertEqual(self.input._match_index, 0)
        self.assertEqual(self.input._ghost, "asks")
        self.assertFalse(self.input._replace)

    def test_tab_without_ghost_does_not_insert_tab(self):
        self.type("plain")
        self.press(Qt.Key.Key_Tab)
        self.assertEqual(self.input.toPlainText(), "plain")

    # -- fuzzy completion ----------------------------------------------
    def test_fuzzy_subsequence_shows_hint_and_right_replaces(self):
        self.type("/thm")
        self.assertTrue(self.input._replace)
        self.assertEqual(self.input._ghost, "  \u2192 /theme")
        self.press(Qt.Key.Key_Right)
        self.assertEqual(self.input.toPlainText(), "/theme")
        self.assertEqual(self.input._ghost, "")

    # -- editing --------------------------------------------------------
    def test_backspace_recomputes_ghost(self):
        self.set_text("/settings")
        self.assertEqual(self.input._ghost, "")

        self.press(Qt.Key.Key_Backspace)
        self.assertEqual(self.input.toPlainText(), "/setting")
        self.assertEqual(self.input._ghost, "s")

        self.press(Qt.Key.Key_Backspace)
        self.assertEqual(self.input.toPlainText(), "/settin")
        self.assertEqual(self.input._ghost, "gs")

        self.press(Qt.Key.Key_Right)
        self.assertEqual(self.input.toPlainText(), "/settings")

    # -- states with no completion -------------------------------------
    def test_no_ghost_for_plain_text(self):
        self.type("hello")
        self.assertEqual(self.input._ghost, "")

    def test_no_ghost_after_a_space(self):
        self.type("/set now")
        self.assertEqual(self.input._ghost, "")

    def test_no_ghost_for_unknown_command(self):
        self.type("/zz")
        self.assertEqual(self.input._ghost, "")

    def test_no_ghost_when_cursor_is_not_at_end(self):
        self.type("/set")
        cursor = self.input.textCursor()
        cursor.setPosition(2)
        self.input.setTextCursor(cursor)
        self.app.processEvents()
        self.assertEqual(self.input._ghost, "")

    def test_no_ghost_with_selection(self):
        self.type("/set")
        self.input.selectAll()
        self.app.processEvents()
        self.assertEqual(self.input._ghost, "")

    def test_no_ghost_for_multiline_input(self):
        self.type("/set")
        self.press(Qt.Key.Key_Return)
        self.type("t")
        self.assertEqual(self.input._ghost, "")

    # -- matching -------------------------------------------------------
    def test_matching_is_case_insensitive(self):
        self.assertEqual(self.input._completions("/SET")[0], "/settings")
        self.type("/SET")
        self.assertEqual(self.input._ghost, "tings")


class TaskContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = g.AgentWindow()
        for _ in range(10):
            self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_new_skill_task_clears_history(self):
        fake = mock.Mock()
        self.window.conversation_history = ["User: watch Ghosts", "System: old"]
        with mock.patch.object(g, "get_agent", return_value=fake):
            self.window._reset_task_context_if_new("watch Jujutsu Kaisen")
        self.assertEqual(self.window.conversation_history, [])
        fake.save_conversation_history.assert_called_once_with([])

    def test_continuation_keeps_history(self):
        history = ["User: watch Ghosts", "System: old"]
        self.window.conversation_history = list(history)
        with mock.patch.object(g, "get_agent", return_value=mock.Mock()):
            self.window._reset_task_context_if_new("now click the poster")
        self.assertEqual(self.window.conversation_history, history)

    def test_task_command_clears_history(self):
        fake = mock.Mock()
        self.window.conversation_history = ["User: watch Ghosts", "System: old"]
        with mock.patch.object(g, "get_agent", return_value=fake), \
             mock.patch.object(self.window, "_run_background_command"):
            self.assertTrue(self.window._handle_slash_command(
                "/browse https://example.com | task"))
        self.assertEqual(self.window.conversation_history, [])

    def test_help_keeps_history(self):
        self.window.conversation_history = ["User: watch Ghosts", "System: old"]
        with mock.patch.object(g, "get_agent", return_value=mock.Mock()):
            self.window._handle_slash_command("/help")
        self.assertIn("User: watch Ghosts", self.window.conversation_history)


if __name__ == "__main__":
    unittest.main()
