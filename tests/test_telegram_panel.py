"""TelegramPanel widget tests (offscreen, no Telegram connection).

Run: python -m unittest tests.test_telegram_panel -v
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import agent_gui_qt as g  # noqa: E402


class TelegramPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.panel = g.TelegramPanel()
        self.panel.resize(320, 400)
        self.panel.show()
        self.app.processEvents()

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()

    def test_starts_empty(self):
        self.assertEqual(self.panel.entries, [])
        self.assertTrue(self.panel.placeholder.isVisible())
        self.assertTrue(self.panel.list_scroll.isHidden())
        self.assertEqual(self.panel.clear_btn.text(), "clear")

    def test_summary_is_plain_text(self):
        self.panel.add_summary("Alice", "report.pdf", "<b>x</b> & y")
        self.assertEqual(len(self.panel.entries), 1)
        entry = self.panel.entries[0]
        self.assertEqual(entry.sender_label.text(), "Alice")
        self.assertEqual(entry.file_label.text(), "report.pdf")
        self.assertEqual(entry.summary_label.text(), "<b>x</b> & y")
        for lbl in (entry.sender_label, entry.file_label,
                    entry.summary_label):
            self.assertEqual(lbl.textFormat(), Qt.TextFormat.PlainText)
        self.assertTrue(self.panel.placeholder.isHidden())
        self.assertFalse(self.panel.list_scroll.isHidden())

    def test_newest_summary_is_first(self):
        self.panel.add_summary("A", "a.txt", "one")
        self.panel.add_summary("B", "b.txt", "two")
        self.assertEqual(
            [e.summary_label.text() for e in self.panel.entries],
            ["two", "one"])
        self.assertIs(
            self.panel.list_layout.itemAt(0).widget(),
            self.panel.entries[0])

    def test_entries_are_capped(self):
        total = g.TELEGRAM_MAX_ENTRIES + 5
        for i in range(total):
            self.panel.add_summary("A", f"{i}.txt", f"summary {i}")
        self.assertEqual(len(self.panel.entries), g.TELEGRAM_MAX_ENTRIES)
        self.assertEqual(
            self.panel.entries[0].summary_label.text(),
            f"summary {total - 1}")
        widgets = [
            self.panel.list_layout.itemAt(i).widget()
            for i in range(self.panel.list_layout.count())
        ]
        self.assertEqual(
            len([w for w in widgets if w is not None]),
            g.TELEGRAM_MAX_ENTRIES)

    def test_clear_restores_empty_state(self):
        self.panel.add_summary("A", "a.txt", "one")
        self.panel.add_summary("B", "b.txt", "two")
        self.panel.clear_btn.click()
        self.assertEqual(self.panel.entries, [])
        self.assertTrue(self.panel.placeholder.isVisible())
        self.assertTrue(self.panel.list_scroll.isHidden())


if __name__ == "__main__":
    unittest.main(verbosity=2)
