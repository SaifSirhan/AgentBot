"""Drag & drop file input tests (offscreen Qt, simulated drop events).

Run: python -m unittest tests.test_drag_drop -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

import agent_gui_qt as g  # noqa: E402


def file_mime(*paths):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    return mime


class DragDropTests(unittest.TestCase):
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

    def tmp_path(self, name):
        return str(Path(tempfile.gettempdir()) / f"agentbot_dd_{name}")

    def drag_enter(self, mime):
        event = QDragEnterEvent(
            QPoint(10, 10), Qt.DropAction.CopyAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self.window.dragEnterEvent(event)
        return event

    def drop(self, mime):
        event = QDropEvent(
            QPointF(10, 10), Qt.DropAction.CopyAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self.window.dropEvent(event)
        return event

    def test_accepts_drops(self):
        self.assertTrue(self.window.acceptDrops())
        self.assertFalse(self.window.input_field.acceptDrops())
        self.assertFalse(self.window.input_field.viewport().acceptDrops())

    def test_drag_enter_highlights_and_leave_reverts(self):
        event = self.drag_enter(file_mime(self.tmp_path("a.txt")))
        self.assertTrue(event.isAccepted())
        self.assertEqual(event.dropAction(), Qt.DropAction.CopyAction)
        self.assertTrue(self.window._drop_highlighted)
        self.assertFalse(self.window._drop_overlay.isHidden())
        self.assertIn("#70FFD4", self.window._drop_overlay.styleSheet())

        leave = QDragLeaveEvent()
        self.window.dragLeaveEvent(leave)
        self.assertFalse(self.window._drop_highlighted)
        self.assertTrue(self.window._drop_overlay.isHidden())
        self.assertNotIn("#70FFD4", self.window._drop_overlay.styleSheet())

    def test_drop_inserts_path_into_command_input(self):
        path = self.tmp_path("a.txt")
        event = self.drop(file_mime(path))
        self.assertTrue(event.isAccepted())
        self.assertEqual(self.window.input_field.objectName(), "commandInput")
        self.assertEqual(self.window.input_field.toPlainText(), path)
        self.assertFalse(self.window._drop_highlighted)
        self.assertTrue(self.window._drop_overlay.isHidden())

    def test_drop_appends_to_existing_text(self):
        self.window.input_field.setPlainText("summarise")
        path = self.tmp_path("report.pdf")
        self.drop(file_mime(path))
        expected = f"summarise, {path}"
        self.assertEqual(self.window.input_field.toPlainText(), expected)
        self.assertEqual(
            self.window.input_field.textCursor().position(), len(expected))

    def test_multiple_files_are_comma_separated(self):
        a = self.tmp_path("a.txt")
        b = self.tmp_path("b.txt")
        self.drop(file_mime(a, b))
        self.assertEqual(self.window.input_field.toPlainText(), f"{a}, {b}")

    def test_non_file_urls_are_ignored(self):
        mime = QMimeData()
        mime.setUrls([QUrl("https://example.com")])
        event = self.drag_enter(mime)
        self.assertFalse(event.isAccepted())
        self.assertFalse(self.window._drop_highlighted)

        drop = self.drop(mime)
        self.assertFalse(drop.isAccepted())
        self.assertEqual(self.window.input_field.toPlainText(), "")

    def test_plain_text_drag_is_ignored(self):
        mime = QMimeData()
        mime.setText("hello")
        event = self.drag_enter(mime)
        self.assertFalse(event.isAccepted())
        self.assertFalse(self.window._drop_highlighted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
