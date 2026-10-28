"""TerminalInput context menu and clipboard-file paste tests (offscreen Qt).

Run: python -m unittest tests.test_gui_input -v
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtCore import QMimeData, QPoint, QUrl  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QContextMenuEvent,
    QImage,
    QTextCursor,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

import agent_gui_qt as g  # noqa: E402


def file_mime(*paths):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    return mime


class TerminalInputClipboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gui_input_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.input = g.TerminalInput()
        self.input.resize(480, 48)
        self.input.show()
        QApplication.clipboard().clear()
        self.app.processEvents()

    def tearDown(self):
        self.input.hide()
        self.input.deleteLater()
        self.app.processEvents()
        QApplication.clipboard().clear()

    # -- context menu ---------------------------------------------------
    def test_context_menu_has_standard_actions(self):
        menu = self.input._build_context_menu()
        texts = [action.text() for action in menu.actions()]
        self.assertEqual(texts, ["Cut", "Copy", "Paste", "Select All"])

    def test_copy_cut_and_select_all_actions_work(self):
        self.input.setPlainText("hello")
        self.input.selectAll()
        menu = self.input._build_context_menu()
        actions = {action.text(): action for action in menu.actions()}

        actions["Copy"].trigger()
        self.assertEqual(QApplication.clipboard().text(), "hello")

        self.input.selectAll()
        actions["Cut"].trigger()
        self.assertEqual(self.input.toPlainText(), "")
        self.assertEqual(QApplication.clipboard().text(), "hello")

        self.input.setPlainText("abc")
        menu = self.input._build_context_menu()
        {a.text(): a for a in menu.actions()}["Select All"].trigger()
        self.assertTrue(self.input.textCursor().hasSelection())

    def test_paste_action_works(self):
        QApplication.clipboard().setText("pasted text")
        self.input.setPlainText("")
        menu = self.input._build_context_menu()
        actions = {action.text(): action for action in menu.actions()}
        self.assertTrue(actions["Paste"].isEnabled())
        actions["Paste"].trigger()
        self.assertEqual(self.input.toPlainText(), "pasted text")

    def test_context_menu_event_shows_custom_menu(self):
        called = {}

        class FakeMenu:
            def exec(self, pos):
                called["pos"] = pos

        event = QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse, QPoint(4, 4), QPoint(40, 40))
        with mock.patch.object(
                self.input, "_build_context_menu", return_value=FakeMenu()):
            self.input.contextMenuEvent(event)
        self.assertIn("pos", called)

    # -- file / image paste ---------------------------------------------
    def test_file_urls_paste_as_paths(self):
        first = self.tmp / "one.txt"
        second = self.tmp / "two.png"
        self.input.setPlainText("look: ")
        cursor = self.input.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.input.setTextCursor(cursor)

        self.input.insertFromMimeData(file_mime(first, second))

        text = self.input.toPlainText()
        self.assertEqual(
            text, f"look: {os.path.normpath(str(first))}, "
                  f"{os.path.normpath(str(second))}")

    def test_file_urls_beat_plain_text(self):
        path = self.tmp / "picked.docx"
        mime = file_mime(path)
        mime.setText("this text must be ignored")
        self.input.setPlainText("")
        self.input.insertFromMimeData(mime)
        text = self.input.toPlainText()
        self.assertIn(os.path.normpath(str(path)), text)
        self.assertNotIn("ignored", text)

    def test_pasted_image_saves_png_and_inserts_path(self):
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(QColor("red"))
        mime = QMimeData()
        mime.setImageData(image)
        target = self.tmp / "temp_paste.png"
        self.input.setPlainText("screenshot: ")
        cursor = self.input.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.input.setTextCursor(cursor)

        with mock.patch.object(
                g, "_paste_image_path", return_value=str(target)):
            self.input.insertFromMimeData(mime)

        text = self.input.toPlainText()
        self.assertEqual(text, f"screenshot: {os.path.normpath(str(target))}")
        self.assertTrue(target.is_file())
        saved = QImage(str(target))
        self.assertFalse(saved.isNull())
        self.assertEqual(saved.size(), image.size())

    def test_plain_text_paste_falls_back(self):
        mime = QMimeData()
        mime.setText("hello")
        self.input.setPlainText("")
        self.input.insertFromMimeData(mime)
        self.assertEqual(self.input.toPlainText(), "hello")


if __name__ == "__main__":
    unittest.main(verbosity=2)
