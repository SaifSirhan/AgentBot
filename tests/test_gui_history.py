"""Prompt history: the pure walker and its Up/Down wiring (offscreen Qt).

Run: python -m unittest tests.test_gui_history -v
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
from PySide6.QtWidgets import QApplication  # noqa: E402

import agent_gui_qt as g  # noqa: E402


class CommandHistoryTests(unittest.TestCase):
    def setUp(self):
        self.history = g.CommandHistory()

    def test_walk_back_then_forward(self):
        for text in ("one", "two", "three"):
            self.history.add(text)
        self.assertEqual(self.history.prev(), "three")
        self.assertEqual(self.history.prev(), "two")
        self.assertEqual(self.history.prev(), "one")
        self.assertIsNone(self.history.prev(), "nothing older than the oldest")
        self.assertEqual(self.history.next(), "two")
        self.assertEqual(self.history.next(), "three")

    def test_live_prompt_is_returned_when_walking_past_the_newest(self):
        self.history.add("saved")
        self.assertEqual(self.history.prev("unsent draft"), "saved")
        self.assertEqual(self.history.next("saved"), "unsent draft")
        self.assertIsNone(self.history.next("unsent draft"))

    def test_consecutive_duplicates_are_stored_once(self):
        self.history.add("same")
        self.history.add("same")
        self.history.add("other")
        self.history.add("other")
        self.assertEqual(self.history.entries, ["same", "other"])

    def test_non_adjacent_duplicates_are_kept(self):
        for text in ("a", "b", "a"):
            self.history.add(text)
        self.assertEqual(self.history.entries, ["a", "b", "a"])

    def test_blank_submissions_are_ignored(self):
        self.history.add("")
        self.history.add("   ")
        self.history.add(None)
        self.assertEqual(self.history.entries, [])

    def test_limit_drops_the_oldest(self):
        history = g.CommandHistory(limit=3)
        for text in ("one", "two", "three", "four"):
            history.add(text)
        self.assertEqual(history.entries, ["two", "three", "four"])

    def test_reset_ends_the_walk(self):
        self.history.add("one")
        self.history.add("two")
        self.assertEqual(self.history.prev(), "two")
        self.history.reset()
        # Back at the bottom, so the next Up starts again from the newest.
        self.assertEqual(self.history.prev(), "two")


class TerminalInputHistoryTests(unittest.TestCase):
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

    def _press(self, key):
        QTest.keyPress(self.input, key)
        QTest.keyRelease(self.input, key)
        self.app.processEvents()

    def _type(self, text):
        QTest.keyClicks(self.input, text)
        self.app.processEvents()

    def _caret_to(self, block, position=0):
        cursor = self.input.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        for _ in range(block):
            cursor.movePosition(QTextCursor.MoveOperation.NextBlock)
        cursor.movePosition(QTextCursor.MoveOperation.Right,
                            QTextCursor.MoveMode.MoveAnchor, position)
        self.input.setTextCursor(cursor)

    def test_up_recalls_newest_then_older_and_holds_at_the_oldest(self):
        self.input.history.add("alpha")
        self.input.history.add("beta")
        self._press(Qt.Key.Key_Up)
        self.assertEqual(self.input.toPlainText(), "beta")
        self._press(Qt.Key.Key_Up)
        self.assertEqual(self.input.toPlainText(), "alpha")
        self._press(Qt.Key.Key_Up)
        self.assertEqual(self.input.toPlainText(), "alpha")

    def test_down_returns_the_draft_being_typed(self):
        self.input.history.add("alpha")
        self._type("draft")
        self._press(Qt.Key.Key_Up)
        self.assertEqual(self.input.toPlainText(), "alpha")
        self._press(Qt.Key.Key_Down)
        self.assertEqual(self.input.toPlainText(), "draft")

    def test_empty_history_leaves_the_arrows_to_the_editor(self):
        self._type("hello")
        self._press(Qt.Key.Key_Up)
        self.assertEqual(self.input.toPlainText(), "hello")

    def test_up_on_a_lower_line_keeps_normal_cursor_movement(self):
        self.input.history.add("alpha")
        self.input.setPlainText("one\ntwo")
        self._caret_to(1)
        self._press(Qt.Key.Key_Up)
        self.assertEqual(self.input.toPlainText(), "one\ntwo")

    def test_up_from_the_first_line_of_a_multiline_draft_recalls(self):
        self.input.history.add("alpha")
        self.input.setPlainText("one\ntwo")
        self._caret_to(0)
        self._press(Qt.Key.Key_Up)
        self.assertEqual(self.input.toPlainText(), "alpha")

    def test_up_from_the_last_line_moves_the_caret_instead_of_recalling(self):
        self.input.history.add("alpha")
        self.input.history.add("beta")
        self.input.setPlainText("one\ntwo")
        self._caret_to(1)
        self._press(Qt.Key.Key_Up)
        self.assertEqual(self.input.toPlainText(), "one\ntwo")

    def test_down_from_a_recall_restores_a_multiline_draft(self):
        self.input.history.add("alpha")
        self.input.history.add("beta")
        self.input.setPlainText("one\ntwo")
        # Recall starts from the top line; the draft is kept for the way back.
        self._caret_to(0)
        self._press(Qt.Key.Key_Up)
        self.assertEqual(self.input.toPlainText(), "beta")
        self._press(Qt.Key.Key_Down)
        self.assertEqual(self.input.toPlainText(), "one\ntwo")

    def test_typing_abandons_the_recalled_entry(self):
        for text in ("a", "b", "c"):
            self.input.history.add(text)
        self._press(Qt.Key.Key_Up)
        self.assertEqual(self.input.toPlainText(), "c")
        self._type("x")
        self.assertEqual(self.input.toPlainText(), "cx")
        # The index was reset by the keystroke, so Up restarts at the newest.
        self._press(Qt.Key.Key_Up)
        self.assertEqual(self.input.toPlainText(), "c")

    def test_recalled_text_is_left_on_one_line_with_the_caret_at_the_end(self):
        self.input.history.add("multi\nline")
        self._press(Qt.Key.Key_Up)
        self.assertEqual(self.input.toPlainText(), "multi\nline")
        cursor = self.input.textCursor()
        self.assertFalse(cursor.hasSelection())
        self.assertEqual(cursor.position(), len("multi\nline"))


class SendRecordsHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = g.AgentWindow()
        self.window.show()
        for _ in range(20):
            self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def _send(self, text):
        self.window.input_field.setPlainText(text)
        with mock.patch.object(self.window, "_start_agent_turn"), \
                mock.patch.object(
                    self.window, "_cancel_continuous_listening"):
            self.window.send()

    def test_each_submission_is_recorded_in_order(self):
        self._send("first prompt")
        self._send("second prompt")
        self.assertEqual(
            self.window.input_field.history.entries,
            ["first prompt", "second prompt"])

    def test_slash_commands_are_recorded_too(self):
        with mock.patch.object(self.window, "_handle_slash_command"):
            self.window.input_field.setPlainText("/help")
            self.window.send()
        self.assertEqual(self.window.input_field.history.entries, ["/help"])

    def test_an_empty_submission_records_nothing(self):
        with mock.patch.object(self.window, "_start_agent_turn"), \
                mock.patch.object(
                    self.window, "_cancel_continuous_listening"):
            self.window.send()
        self.assertEqual(self.window.input_field.history.entries, [])

    def test_send_clears_the_walk_so_the_next_up_is_the_last_prompt(self):
        self._send("alpha")
        self._send("beta")
        self.window.input_field.setPlainText("draft")
        QTest.keyPress(self.window.input_field, Qt.Key.Key_Up)
        QTest.keyRelease(self.window.input_field, Qt.Key.Key_Up)
        self.app.processEvents()
        self.assertEqual(self.window.input_field.toPlainText(), "beta")


if __name__ == "__main__":
    unittest.main(verbosity=2)
