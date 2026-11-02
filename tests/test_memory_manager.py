"""MemoryManager overlay, /memories routing, and edit dialog tests.

Offscreen Qt only: no desktop input, no LLM, no network.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import memory


class MemoryManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        import agent_gui_qt as g
        cls.g = g

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="memory_manager_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()

        patcher = mock.patch.object(
            memory._cfg, "memory_path",
            side_effect=lambda *parts: str(self.root.joinpath(*parts)))
        patcher.start()
        self.addCleanup(patcher.stop)
        cwd_patcher = mock.patch.object(
            memory, "current_dir", return_value=str(self.project))
        cwd_patcher.start()
        self.addCleanup(cwd_patcher.stop)

        self.window = self.g.AgentWindow()
        self.addCleanup(self.window.deleteLater)

    def cards(self, overlay):
        out = []
        for i in range(overlay._list_layout.count()):
            widget = overlay._list_layout.itemAt(i).widget()
            if widget is not None and widget.objectName() == "memoryCard":
                out.append(widget)
        return out

    def test_memories_command_opens_the_overlay(self):
        self.assertTrue(self.window._handle_slash_command("/memories"))
        self.assertIsNotNone(self.window._memory_overlay)
        self.assertIsInstance(self.window._memory_overlay,
                              self.g.MemoryManager)

    def test_overlay_lists_every_scope(self):
        memory.add_memory("Global fact.", keywords=["global"])
        memory.add_memory("Project fact.", scope=self.project)
        memory.add_memory("Other fact.", scope=self.root / "other")

        self.window.open_memory_manager()
        overlay = self.window._memory_overlay

        self.assertEqual(overlay._count_label.text(), "3 memories")
        self.assertEqual(len(self.cards(overlay)), 3)

    def test_edit_dialog_returns_normalized_values(self):
        record = memory.add_memory(
            "Original fact.", scope=self.project, keywords=["old"])
        dialog = self.g.MemoryEditDialog(self.window, record)
        dialog.title_edit.setText("  New title  ")
        dialog.scope_combo.setCurrentText("global")
        dialog.keywords_edit.setText("alpha, beta")
        dialog.content_edit.setPlainText("Updated fact.")

        dialog._accept()

        values = dialog.values()
        self.assertEqual(values["title"], "New title")
        self.assertEqual(values["scope"], "global")
        self.assertEqual(values["keywords"], ["alpha", "beta"])
        self.assertEqual(values["content"], "Updated fact.")
        self.assertTrue(memory.update_memory(record["id"], **values))
        self.assertEqual(memory.get_memory(record["id"])["content"],
                         "Updated fact.")

    def test_close_removes_the_overlay(self):
        self.window.open_memory_manager()
        self.window._close_memory_manager()
        self.assertIsNone(self.window._memory_overlay)

    def test_help_and_autocomplete_include_memories(self):
        self.assertIn("/memories", self.g.HELP_TEXT)
        self.assertIn("/memories", self.g.SLASH_COMMANDS)

    def test_settings_gear_button_is_present(self):
        self.assertEqual(self.window.settings_button.toolTip(), "Settings")


if __name__ == "__main__":
    unittest.main(verbosity=2)
