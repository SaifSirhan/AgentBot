"""GUI Stop mechanism tests (offscreen Qt).

Run: python -m unittest tests.test_gui_stop -v
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import agent_gui_qt as g  # noqa: E402


class _FakeSignal:
    def connect(self, slot):
        pass

    def emit(self, *args):
        pass


class _FakeWorker:
    last = None

    def __init__(self, user_input, history, attached_paths=None,
                 stop_event=None):
        self.user_input = user_input
        self.history = history
        self.attached_paths = list(attached_paths or [])
        self._stop_event = stop_event
        self.started = False
        self.completed = _FakeSignal()
        self.failed = _FakeSignal()
        self.step = _FakeSignal()
        _FakeWorker.last = self

    def start(self):
        self.started = True


class _FakeAgent:
    def __init__(self):
        self.received = None

    def run_agent_turn(self, text, history, on_step=None, stop_event=None):
        self.received = {"text": text, "stop_event": stop_event}
        return ["Done: ok"]

    def save_conversation_history(self, history):
        pass


class GuiStopTests(unittest.TestCase):
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

    def _start_processing(self, cancellable=True):
        self.window._processing = True
        self.window._turn_cancellable = cancellable
        self.window._update_send_button()

    def test_button_starts_as_send(self):
        btn = self.window.send_button
        self.assertEqual(btn._icon_name, "send")
        self.assertEqual(btn.toolTip(), "Send (Enter)")
        self.assertFalse(self.window._stop_requested)

    def test_button_switches_to_stop_while_a_turn_runs(self):
        self._start_processing()
        btn = self.window.send_button
        self.assertEqual(btn._icon_name, "stop")
        self.assertEqual(btn._icon_color, g.COLOR_DANGER)
        self.assertEqual(btn.toolTip(), "Stop (Esc)")

    def test_clicking_stop_sets_flag_and_event(self):
        self._start_processing()
        self.window.send_button.click()
        self.assertTrue(self.window._stop_requested)
        self.assertTrue(self.window._stop_event.is_set())
        self.assertIn("Stopping", self.window.windowTitle())

    def test_second_click_is_a_no_op(self):
        called = []
        self._start_processing()
        self.window._stop_requested = True
        self.window._stop_event.set()
        with mock.patch.object(self.window, "set_status",
                               side_effect=lambda *a, **k: called.append(a)):
            self.window.send_button.click()
        self.assertEqual(called, [])

    def test_escape_on_the_window_requests_stop(self):
        self._start_processing()
        QTest.keyPress(self.window, Qt.Key.Key_Escape)
        self.assertTrue(self.window._stop_requested)
        self.assertTrue(self.window._stop_event.is_set())

    def test_escape_in_the_input_field_requests_stop(self):
        self._start_processing()
        event = QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
            Qt.KeyboardModifier.NoModifier)
        consumed = self.window.eventFilter(self.window.input_field, event)
        self.assertTrue(consumed)
        self.assertTrue(self.window._stop_event.is_set())

    def test_non_cancellable_work_keeps_the_send_icon(self):
        self._start_processing(cancellable=False)
        self.assertEqual(self.window.send_button._icon_name, "send")
        self.window.send_button.click()
        self.assertFalse(self.window._stop_requested)
        self.assertFalse(self.window._stop_event.is_set())

    def test_new_turn_resets_stop_and_installs_a_fresh_event(self):
        old_event = self.window._stop_event
        self.window._stop_requested = True
        old_event.set()
        with mock.patch.object(g, "AgentWorker", _FakeWorker), \
                mock.patch.object(self.window, "_reset_task_context_if_new"):
            self.window._start_agent_turn("hello")
        worker = _FakeWorker.last
        self.assertIs(worker._stop_event, self.window._stop_event)
        self.assertIsNot(worker._stop_event, old_event)
        self.assertFalse(worker._stop_event.is_set())
        self.assertFalse(self.window._stop_requested)
        self.assertTrue(self.window._turn_cancellable)
        self.assertTrue(worker.started)
        self.assertEqual(self.window.send_button._icon_name, "stop")

        self.window._end_turn()
        self.assertFalse(self.window._processing)
        self.assertFalse(self.window._turn_cancellable)
        self.assertEqual(self.window.send_button._icon_name, "send")

    def test_worker_forwards_the_stop_event_to_the_agent(self):
        event = g.threading.Event()
        fake_agent = _FakeAgent()
        worker = g.AgentWorker("hi", [], stop_event=event)
        with mock.patch.object(g, "get_agent", return_value=fake_agent):
            worker._run()
        self.assertEqual(fake_agent.received["text"], "hi")
        self.assertIs(fake_agent.received["stop_event"], event)


if __name__ == "__main__":
    unittest.main()
