"""Read-only memory constellation UI and user-model state tests."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QSplitter,
    QTextEdit,
)

import agent_gui_qt as g
import config
import memory


def trait(text, confidence=7, evidence_count=2, last_updated="2026-11-01"):
    return {
        "trait": text,
        "confidence": confidence,
        "evidence_count": evidence_count,
        "last_updated": last_updated,
    }


def snapshot(traits=None, status="valid", **counts):
    return {
        "status": status,
        "path": "user_model.json",
        "traits": list(traits or []),
        "duplicate_count": counts.get("duplicate_count", 0),
        "unsafe_count": counts.get("unsafe_count", 0),
        "invalid_count": counts.get("invalid_count", 0),
    }


class MemoryConstellationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.inspect_user_model = memory.inspect_user_model
        patcher = mock.patch.object(
            memory, "inspect_user_model", return_value=snapshot(status="empty"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.view = g.MemoryConstellationView()
        self.view.resize(860, 520)
        self.view.show()
        self.app.processEvents()
        self.addCleanup(self.view.close)
        self.addCleanup(self.view.deleteLater)

    def test_valid_data_shows_exact_warning_and_first_trait_details(self):
        self.view.set_snapshot(snapshot([
            trait("values solitude", 9, 4, "2026-11-20"),
            trait("prefers concise replies", 6, 1, "2026-10-01"),
        ]))

        self.assertEqual(self.view.warning_label.text(), memory.USER_TRAITS_WARNING)
        self.assertEqual(len(self.view.canvas.traits), 2)
        self.assertEqual(self.view.canvas.selected_index, 0)
        self.assertEqual(self.view.detail_text.text(), "values solitude")
        self.assertEqual(self.view.confidence_label.text(), "9 / 10")
        self.assertEqual(self.view.evidence_label.text(), "4")
        self.assertEqual(self.view.updated_label.text(), "2026-11-20")

    def test_selecting_a_node_updates_the_details(self):
        self.view.set_snapshot(snapshot([
            trait("values solitude", 9, 4),
            trait("prefers concise replies", 6, 1),
        ]))

        self.view.canvas.select_node(1)
        self.app.processEvents()

        self.assertEqual(self.view.detail_text.text(), "prefers concise replies")
        self.assertEqual(self.view.confidence_label.text(), "6 / 10")
        self.assertEqual(self.view.evidence_label.text(), "1")

    def test_canvas_supports_keyboard_node_selection(self):
        self.view.set_snapshot(snapshot([
            trait("values solitude", 9, 4),
            trait("prefers concise replies", 6, 1),
        ]))
        self.view.canvas.setFocus()
        self.app.processEvents()

        self.app.sendEvent(self.view.canvas, QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier))

        self.assertEqual(self.view.canvas.selected_index, 1)
        self.assertEqual(self.view.detail_text.text(), "prefers concise replies")

    def test_empty_missing_malformed_unavailable_invalid_and_unsafe_states(self):
        cases = {
            "empty": "No manual traits yet",
            "missing": "user_model.json is missing",
            "malformed": "user_model.json is malformed",
            "unavailable": "user_model.json is unavailable",
            "invalid": "unsupported shape",
            "unsafe": "No safe manual traits",
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                self.view.set_snapshot(snapshot(status=status))
                self.assertIn(expected, self.view.status_label.text())
                self.assertEqual(self.view.canvas.traits, [])
                self.assertEqual(self.view.warning_label.text(),
                                 memory.USER_TRAITS_WARNING)

    def test_filtered_counts_are_visible_without_rejected_text(self):
        self.view.set_snapshot(snapshot(
            [trait("values solitude")],
            duplicate_count=2,
            unsafe_count=1,
            invalid_count=3,
        ))

        status = self.view.status_label.text()
        self.assertIn("2 duplicates ignored", status)
        self.assertIn("1 unsafe entry hidden", status)
        self.assertIn("3 invalid entries ignored", status)

    def test_layout_uses_confidence_evidence_and_recency(self):
        traits = [
            trait("new strong trait", 10, 16, "2026-11-20"),
            trait("old weak trait", 1, 0, "2020-01-01"),
        ]
        self.view.canvas.resize(800, 320)
        self.view.canvas.set_traits(traits)
        first = self.view.canvas._layout_nodes()
        second = self.view.canvas._layout_nodes()

        self.assertEqual(first, second)
        self.assertGreater(first[0]["radius"], first[1]["radius"])
        self.assertGreater(first[0]["rings"], first[1]["rings"])
        self.assertLess(first[0]["distance"], first[1]["distance"])

    def test_view_has_no_editing_controls(self):
        self.view.set_snapshot(snapshot([trait("values solitude")]))

        for widget_type in (QLineEdit, QPlainTextEdit, QTextEdit):
            self.assertEqual(self.view.findChildren(widget_type), [])
        labels = [button.text().lower()
                  for button in self.view.findChildren(g.QPushButton)]
        for forbidden in ("add", "edit", "delete", "save"):
            self.assertFalse(any(forbidden in label for label in labels))

    def test_refresh_does_not_modify_the_source_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / memory.USER_MODEL_FILENAME
            path.write_text(json.dumps({
                "version": 1,
                "traits": [trait("values solitude")],
            }), encoding="utf-8")
            before = path.read_bytes()
            with mock.patch.object(
                    config, "memory_path",
                    side_effect=lambda *parts: os.path.join(tmp, *parts)), \
                    mock.patch.object(memory, "inspect_user_model",
                                      wraps=self.inspect_user_model):
                self.view.refresh()

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(self.view.detail_text.text(), "values solitude")


class MemoryNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_memory_navigation_uses_one_page_and_returns_to_chat(self):
        with mock.patch.object(
                memory, "inspect_user_model", return_value=snapshot(status="empty")):
            window = g.AgentWindow()
        window.resize(1480, 820)
        window.show()
        self.app.processEvents()
        self.addCleanup(window.deleteLater)

        window.nav_rail.buttons["Memory"].click()
        self.app.processEvents()

        self.assertIs(window.page_stack.currentWidget(), window.memory_view)
        self.assertFalse(window.composer.isVisible())
        self.assertFalse(window.findChildren(QSplitter))
        self.assertEqual(window.findChildren(QMainWindow), [])

        window.memory_view.back_button.click()
        self.app.processEvents()

        self.assertIs(window.page_stack.currentWidget(), window.chat)
        self.assertTrue(window.composer.isVisible())
        window.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
