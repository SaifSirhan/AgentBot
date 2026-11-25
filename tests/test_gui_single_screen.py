"""Single-screen Kestrel workspace contract (offscreen Qt).

Run: python -m unittest tests.test_gui_single_screen -v
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QFrame,
    QMainWindow,
    QSplitter,
    QToolButton,
)

import agent_gui_qt as g  # noqa: E402


class SingleScreenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = g.AgentWindow()
        self.window.resize(1480, 820)
        self.window.show()
        for _ in range(10):
            self.app.processEvents()
        self.addCleanup(self.window.deleteLater)

    def tearDown(self):
        self.window.close()
        self.app.processEvents()

    def test_one_window_has_no_utility_panel_or_splitter(self):
        self.assertFalse(self.window.findChildren(QSplitter))
        self.assertFalse(self.window.findChildren(g.RightPanel))
        self.assertIsInstance(self.window, QMainWindow)
        self.assertEqual(len(self.window.findChildren(QMainWindow)), 0)
        for name in ("nav_rail", "workspace", "owl_stage", "task_workspace",
                     "composer", "state_bar"):
            with self.subTest(widget=name):
                self.assertIsNotNone(getattr(self.window, name, None))
                self.assertTrue(getattr(self.window, name).isVisible())

    def test_navigation_has_the_six_approved_destinations(self):
        rail = getattr(self.window, "nav_rail", None)
        self.assertIsNotNone(rail)
        self.assertEqual(
            list(rail.buttons),
            ["Kestrel", "Tasks", "Files", "Voice", "Memory", "Settings"],
        )
        for label, button in rail.buttons.items():
            with self.subTest(label=label):
                self.assertIsInstance(button, QToolButton)
                self.assertEqual(button.text(), label)
                self.assertTrue(button.isVisible())

    def test_primary_text_column_is_exactly_twenty_pixels_wider(self):
        column = getattr(self.window, "conversation_column", None)
        self.assertIsNotNone(column)
        self.assertEqual(g.TEXT_COLUMN_BASELINE_W, 900)
        self.assertEqual(g.TEXT_COLUMN_W, 920)
        self.assertEqual(column.width(), g.TEXT_COLUMN_W)
        self.assertGreaterEqual(
            self.window.chat.viewport().width(), g.TEXT_COLUMN_BASELINE_W)

    def test_large_owl_stage_is_beside_not_over_the_chat(self):
        stage = getattr(self.window, "owl_stage", None)
        self.assertIsNotNone(stage)
        self.assertGreaterEqual(stage.width(), 300)
        self.assertGreaterEqual(stage.height(), 400)
        chat_right = self.window.chat.mapTo(
            self.window, QPoint(self.window.chat.width(), 0)).x()
        stage_left = stage.mapTo(self.window, QPoint(0, 0)).x()
        self.assertLessEqual(chat_right, stage_left)
        self.assertIsNotNone(stage.pet.sprite)
        self.assertTrue(stage.pet.testAttribute(
            g.Qt.WidgetAttribute.WA_TransparentForMouseEvents))

    def test_live_task_steps_reach_the_integrated_workspace(self):
        task = getattr(self.window, "task_workspace", None)
        self.assertIsNotNone(task)
        task.set_task_title("Organize Downloads")
        self.window._on_worker_step(
            "Action: read_file(Downloads) -> Result: 12 files found")
        self.app.processEvents()
        self.assertEqual(task.task_label.text(), "Organize Downloads")
        self.assertIn("12 files found", task.steps_text())

    def test_state_bar_and_owl_share_the_real_agent_state(self):
        bar = getattr(self.window, "state_bar", None)
        stage = getattr(self.window, "owl_stage", None)
        self.assertIsNotNone(bar)
        self.assertIsNotNone(stage)
        self.window._recording = False
        self.window._continuous_listening = False
        with mock.patch.object(g, "_tts_is_speaking", return_value=False):
            self.window.set_status("Ready", busy=False)
            self.assertEqual(bar.state, "IDLE")

            self.window._processing = True
            self.window.set_status("Thinking…", busy=True)
            self.assertEqual(bar.state, "THINKING")

            self.window._recording = True
            self.window.set_status("Listening…", busy=True)
            self.assertEqual(bar.state, "LISTENING")

        self.window._recording = False
        self.window._processing = False
        with mock.patch.object(g, "_tts_is_speaking", return_value=True):
            self.window.set_status("Speaking…", busy=False)
            self.assertEqual(bar.state, "SPEAKING")
        self.assertEqual(stage.hud.state, bar.state)

    def test_composer_spans_the_workspace_and_exposes_send_and_voice(self):
        composer = getattr(self.window, "composer", None)
        self.assertIsNotNone(composer)
        self.assertIsNotNone(getattr(self.window, "send_button", None))
        self.assertIsNotNone(getattr(self.window, "voice_button", None))
        self.assertTrue(self.window.send_button.isVisible())
        self.assertTrue(self.window.voice_button.isVisible())
        self.assertEqual(self.window.voice_button.text(), "F9 push-to-talk")
        workspace_left = self.window.workspace.mapTo(
            self.window, QPoint(0, 0)).x()
        composer_left = composer.mapTo(self.window, QPoint(0, 0)).x()
        composer_right = composer.mapTo(
            self.window, QPoint(composer.width(), 0)).x()
        workspace_right = self.window.workspace.mapTo(
            self.window, QPoint(self.window.workspace.width(), 0)).x()
        self.assertEqual(composer_left, workspace_left)
        self.assertEqual(composer_right, workspace_right)

    def test_composer_buttons_use_the_existing_send_and_voice_paths(self):
        self.assertIsNotNone(getattr(self.window, "send_button", None))
        self.assertIsNotNone(getattr(self.window, "voice_button", None))
        with mock.patch.object(self.window, "send") as send:
            self.window.send_button.click()
            send.assert_called_once_with()

        self.window._voice_ready = True

        def start():
            self.window._recording = True

        def stop():
            self.window._recording = False

        with mock.patch.object(self.window, "_start_recording",
                               side_effect=start) as start_mock, \
                mock.patch.object(self.window, "_stop_recording",
                                  side_effect=stop) as stop_mock:
            self.window.voice_button.click()
            start_mock.assert_called_once_with()
            self.window.voice_button.click()
            stop_mock.assert_called_once_with()

    def test_telegram_clear_removes_widgets_and_transcript_entries(self):
        self.window.chat.add_message(g.UserMessage("keep this line"), fade=False)
        self.window._render_telegram_result((
            "summary", 10, 20, "private summary",
            {"sender": "Alice", "file_name": "report.pdf"},
        ))
        self.window._clear_telegram_messages()
        self.app.processEvents()
        self.assertEqual(self.window._telegram_messages, [])
        self.assertEqual(
            self.window.chat.chat_log, [("user", "keep this line")])
        self.assertFalse(self.window.chat._body.findChildren(
            g.UntrustedTextMessage))

    def test_new_chat_discards_telegram_widget_references(self):
        self.window._render_telegram_result((
            "summary", 10, 20, "private summary",
            {"sender": "Alice", "file_name": "report.pdf"},
        ))
        agent = mock.Mock()
        with mock.patch.object(g, "get_agent", return_value=agent):
            self.window.new_chat()
        self.app.processEvents()
        self.assertEqual(self.window._telegram_messages, [])
        self.assertEqual(self.window.chat.chat_log, [])
        agent.save_conversation_history.assert_called_once_with([])

    def test_settings_navigation_opens_the_existing_settings_overlay(self):
        rail = getattr(self.window, "nav_rail", None)
        self.assertIsNotNone(rail)
        rail.buttons["Settings"].click()
        self.app.processEvents()
        panel = self.window.findChild(QFrame, "settingsPanel")
        self.assertIsNotNone(panel)
        self.assertTrue(panel.isVisible())


if __name__ == "__main__":
    unittest.main(verbosity=2)
