"""GUI /task and /autonomous routing tests (offscreen Qt).

Run: python -m unittest tests.test_gui_task -v
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

import agent_gui_qt as g  # noqa: E402


class GuiTaskCommandTests(unittest.TestCase):
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

    def test_task_and_autonomous_are_advertised(self):
        self.assertIn("/task", g.SLASH_COMMANDS)
        self.assertIn("/autonomous", g.SLASH_COMMANDS)
        self.assertIn("/task", g.HELP_TEXT)

    def test_slash_task_starts_an_agent_turn(self):
        with mock.patch.object(self.window, "_start_agent_turn") as start:
            handled = self.window._handle_slash_command(
                "/task open notepad")
        self.assertTrue(handled)
        start.assert_called_once_with("/task open notepad")

    def test_slash_autonomous_starts_an_agent_turn(self):
        with mock.patch.object(self.window, "_start_agent_turn") as start:
            handled = self.window._handle_slash_command(
                "/autonomous save hello")
        self.assertTrue(handled)
        start.assert_called_once_with("/autonomous save hello")

    def test_empty_task_goal_does_not_start_a_turn(self):
        for text in ("/task", "/autonomous"):
            with mock.patch.object(self.window,
                                   "_start_agent_turn") as start:
                self.assertTrue(self.window._handle_slash_command(text))
            start.assert_not_called()

    def test_send_routes_a_task_command_to_the_agent(self):
        self.window.input_field.setPlainText("/task save hello")
        with mock.patch.object(self.window, "_interrupt_speech"), \
                mock.patch.object(self.window,
                                  "_cancel_continuous_listening"), \
                mock.patch.object(self.window,
                                  "_start_agent_turn") as start:
            self.window.send()
        start.assert_called_once_with("/task save hello")
        self.assertEqual(self.window.input_field.toPlainText(), "")


if __name__ == "__main__":
    unittest.main()
