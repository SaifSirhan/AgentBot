"""GUI tests for the opt-in proactive-thought timer (offscreen).

No real screenshot or vision call is made.

Run: python -m unittest tests.test_spontaneous_ui -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

import agent_gui_qt as g  # noqa: E402


class FakeControl:
    def __init__(self, result):
        self.result = result

    def screenshot_background(self):
        return self.result


class FakeAgent:
    def __init__(self, text):
        self.text = text
        self.paths = []

    def spontaneous_thought(self, path):
        self.paths.append(path)
        return self.text


class SpontaneousUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = g.AgentWindow()
        self.window.show()
        for _ in range(10):
            self.app.processEvents()

    def tearDown(self):
        timer = self.window._spontaneous_timer
        self.window._spontaneous_timer = None
        if timer is not None:
            timer.stop()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def enable(self):
        with mock.patch.object(
            g, "read_config_file_raw", return_value={
                "SPONTANEOUS_THOUGHTS_ENABLED": True,
            },
        ):
            self.window._start_spontaneous_thoughts()

    def test_disabled_by_default(self):
        with mock.patch.object(g, "read_config_file_raw", return_value={}):
            self.window._start_spontaneous_thoughts()
        self.assertIsNone(self.window._spontaneous_timer)

    def test_enabled_starts_two_hour_timer(self):
        self.window._os_control = object()
        self.enable()
        timer = self.window._spontaneous_timer
        self.assertIsNotNone(timer)
        self.assertTrue(timer.isActive())
        self.assertEqual(timer.interval(), g.SPONTANEOUS_INTERVAL_MS)

    def test_enabled_without_os_control_is_skipped(self):
        self.window._os_control = None
        self.enable()
        self.assertIsNone(self.window._spontaneous_timer)

    def test_starting_twice_keeps_one_timer(self):
        self.window._os_control = object()
        self.enable()
        first = self.window._spontaneous_timer
        self.enable()
        self.assertIs(self.window._spontaneous_timer, first)

    def test_message_posts_to_chat(self):
        before = len(self.window.chat.chat_log)
        self.window._on_spontaneous_message("You have 50 unread emails.")
        added = self.window.chat.chat_log[before:]
        self.assertEqual(len(added), 1)
        self.assertIn("[proactive]", added[0][1])
        self.assertIn("50 unread", added[0][1])

    def test_worker_posts_and_deletes_screenshot(self):
        shot = Path(tempfile.gettempdir()) / "agentbot_thought_shot.png"
        shot.write_bytes(b"fake-png")
        self.window._os_control = FakeControl(
            f"Screenshot saved: {shot}\nImage: 100x100 pixels")
        fake_agent = FakeAgent("Your download has finished.")
        self.window._spontaneous_busy = True

        before = len(self.window.chat.chat_log)
        with mock.patch.object(g, "get_agent", return_value=fake_agent):
            self.window._spontaneous_work()

        added = self.window.chat.chat_log[before:]
        self.assertEqual(len(added), 1)
        self.assertIn("Your download has finished.", added[0][1])
        self.assertEqual(fake_agent.paths, [str(shot)])
        self.assertFalse(shot.exists())
        self.assertFalse(self.window._spontaneous_busy)

    def test_worker_is_silent_when_screenshot_fails(self):
        self.window._os_control = FakeControl("ERROR: mss unavailable")
        fake_agent = FakeAgent("should not appear")
        before = len(self.window.chat.chat_log)

        with mock.patch.object(g, "get_agent", return_value=fake_agent):
            self.window._spontaneous_work()

        self.assertEqual(len(self.window.chat.chat_log), before)
        self.assertEqual(fake_agent.paths, [])
        self.assertFalse(self.window._spontaneous_busy)

    def test_tick_skips_while_processing(self):
        self.window._processing = True
        with mock.patch.object(g.threading, "Thread") as thread:
            self.window._spontaneous_tick()
        thread.assert_not_called()
        self.assertFalse(self.window._spontaneous_busy)

    def test_tick_starts_a_daemon_worker(self):
        self.window._os_control = object()
        with mock.patch.object(g.threading, "Thread") as thread:
            self.window._spontaneous_tick()
        thread.assert_called_once()
        self.assertTrue(thread.call_args.kwargs["daemon"])
        self.assertEqual(
            thread.call_args.kwargs["target"],
            self.window._spontaneous_work)
        thread.return_value.start.assert_called_once()
        self.assertTrue(self.window._spontaneous_busy)


if __name__ == "__main__":
    unittest.main(verbosity=2)
