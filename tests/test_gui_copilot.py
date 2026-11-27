"""Main-chat Screen Copilot GUI contract (offscreen Qt)."""
import contextlib
import inspect
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QLabel,
    QMainWindow,
    QSplitter,
)

import agent_gui_qt as gui  # noqa: E402
import screen_copilot as sc  # noqa: E402


class _FakeAgent:
    def __init__(self):
        self.received = None

    def run_agent_turn(self, text, history, on_step=None, stop_event=None,
                       copilot_approval_handler=None):
        self.received = {
            "text": text,
            "history": history,
            "stop_event": stop_event,
            "approval_handler": copilot_approval_handler,
        }
        if on_step is not None:
            on_step("Copilot: Performing step 2…")
        return ["Done: ok"]

    def save_conversation_history(self, history):
        self.saved = history


class CopilotGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = gui.AgentWindow()
        self.window.resize(1480, 820)
        self.window.show()
        for _ in range(10):
            self.app.processEvents()

    def tearDown(self):
        self.window._detach_copilot_bridge()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def approval_payload(self, timeout_seconds=60):
        return {
            "task_id": "task-1",
            "request_id": "request-1",
            "goal": "Search for Dune and open it",
            "target": {
                "title": "Fixture",
                "process": "fixture.exe",
                "pid": 202,
                "hwnd": 101,
            },
            "max_steps": 5,
            "allowed_actions": ["click", "type_text", "vision_click"],
            "typing_rule": "Typed text must be part of the approved goal",
            "screenshot_disclosure": "Only the selected window may be captured",
            "timeout_seconds": timeout_seconds,
        }

    def start_turn(self, text="use screen_copilot in Notepad"):
        worker = mock.Mock()
        with mock.patch.object(gui, "AgentWorker", return_value=worker), \
                mock.patch.object(self.window, "_reset_task_context_if_new"):
            self.window._start_agent_turn(text)
        self.app.processEvents()
        return worker

    def show_approval(self, timeout_seconds=60):
        payload = self.approval_payload(timeout_seconds)
        future = self.window._copilot_bridge(payload)
        self.app.processEvents()
        cards = self.window.chat._body.findChildren(gui.CopilotApprovalCard)
        self.assertEqual(len(cards), 1)
        return payload, future, cards[0]

    def test_copilot_has_no_separate_page_worker_or_nav_destination(self):
        self.assertFalse(hasattr(gui, "CopilotView"))
        self.assertFalse(hasattr(gui, "CopilotWorker"))
        self.assertFalse(hasattr(self.window, "copilot_view"))
        self.assertEqual(
            list(self.window.nav_rail.buttons),
            ["Kestrel", "Tasks", "Files", "Voice", "Memory", "Settings"],
        )
        self.assertEqual(
            [self.window.page_stack.widget(index)
             for index in range(self.window.page_stack.count())],
            [self.window.chat, self.window.memory_view],
        )
        self.assertEqual(
            sum(isinstance(w, QMainWindow) for w in self.app.topLevelWidgets()), 1)
        self.assertFalse(any(
            isinstance(w, QDialog) for w in self.app.topLevelWidgets()))
        self.assertFalse(any(
            isinstance(w, QSplitter) for w in self.window.findChildren(QSplitter)))

    def test_each_main_turn_gets_a_fresh_approval_bridge(self):
        self.start_turn("first request")
        first = self.window._copilot_bridge
        self.assertIsNotNone(first)
        self.window._end_turn()
        self.assertIsNone(self.window._copilot_bridge)

        self.start_turn("second request")
        second = self.window._copilot_bridge
        self.assertIsNotNone(second)
        self.assertIsNot(second, first)

    def test_worker_forwards_the_turn_bridge_and_stop_event(self):
        self.assertIn(
            "copilot_approval_handler",
            inspect.signature(gui.AgentWorker).parameters,
        )
        stop_event = threading.Event()
        bridge = mock.Mock()
        fake_agent = _FakeAgent()
        worker = gui.AgentWorker(
            "control Notepad", [], stop_event=stop_event,
            copilot_approval_handler=bridge)
        with mock.patch.object(gui, "get_agent", return_value=fake_agent):
            worker._run()
        self.assertIs(fake_agent.received["approval_handler"], bridge)
        self.assertIs(fake_agent.received["stop_event"], stop_event)

    def test_approval_requested_by_main_chat_appears_inline(self):
        self.start_turn()
        self.window._show_memory_page()
        payload, future, card = self.show_approval()

        self.assertFalse(future.done())
        self.assertIs(self.window.page_stack.currentWidget(), self.window.chat)
        self.assertTrue(self.window.composer.isVisible())
        self.assertIs(card.parentWidget(), self.window.chat._body)
        text = "\n".join(label.text() for label in card.findChildren(QLabel))
        for expected in (
            payload["goal"], "Fixture", "fixture.exe", "202", "101",
            "5", "click", "type_text", "vision_click",
            payload["typing_rule"], payload["screenshot_disclosure"],
        ):
            self.assertIn(expected, text)

    def test_approve_resolves_once_and_removes_card_and_gap(self):
        self.start_turn()
        _payload, future, card = self.show_approval()
        card.approve_btn.click()
        self.assertIs(future.result(timeout=1), True)
        card.resolve(False)
        self.assertIs(future.result(timeout=1), True)
        self.app.processEvents()
        self.assertEqual(
            list(self.window.chat._body.findChildren(gui.CopilotApprovalCard)), [])
        self.assertNotIn(id(card), self.window.chat._gap_items)

    def test_deny_and_expiry_resolve_false_once_and_remove_the_card(self):
        self.start_turn()
        for expire in (False, True):
            with self.subTest(expire=expire):
                _payload, future, card = self.show_approval()
                if expire:
                    card._expire()
                else:
                    card.deny_btn.click()
                self.assertIs(future.result(timeout=1), False)
                card.resolve(True)
                self.assertIs(future.result(timeout=1), False)
                self.app.processEvents()
                self.assertEqual(
                    list(self.window.chat._body.findChildren(
                        gui.CopilotApprovalCard)), [])
                self.assertNotIn(id(card), self.window.chat._gap_items)

    def test_enter_does_not_approve_a_plan(self):
        self.start_turn()
        _payload, future, card = self.show_approval()
        event = QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_Return,
            Qt.KeyboardModifier.NoModifier)
        self.assertTrue(card.eventFilter(card.approve_btn, event))
        self.assertFalse(future.done())

    def test_stop_denies_visible_and_queued_approvals(self):
        self.start_turn()
        _, visible_future, _ = self.show_approval()
        queued = self.approval_payload()
        queued["request_id"] = "request-2"
        bridge = self.window._copilot_bridge
        bridge.requested.disconnect()
        queued_future = bridge(queued)

        with mock.patch.object(sc, "cancel_current_task") as cancel:
            self.window._request_stop()

        self.assertTrue(self.window._stop_event.is_set())
        self.assertIs(visible_future.result(timeout=1), False)
        self.assertIs(queued_future.result(timeout=1), False)
        self.assertEqual(
            list(self.window.chat._body.findChildren(gui.CopilotApprovalCard)), [])
        cancel.assert_called_once_with()

    def test_escape_uses_the_main_turn_cancellation_path(self):
        self.start_turn()
        _payload, future, _card = self.show_approval()
        with mock.patch.object(sc, "cancel_current_task") as cancel:
            QTest.keyPress(self.window, Qt.Key.Key_Escape)
        self.assertTrue(self.window._stop_requested)
        self.assertTrue(self.window._stop_event.is_set())
        self.assertIs(future.result(timeout=1), False)
        cancel.assert_called_once_with()

    def test_new_chat_cancels_the_main_turn_and_pending_approval(self):
        self.start_turn()
        _payload, future, _card = self.show_approval()
        fake_agent = mock.Mock()
        with mock.patch.object(gui, "get_agent", return_value=fake_agent), \
                mock.patch.object(sc, "cancel_current_task") as cancel:
            self.window.new_chat()
        self.assertTrue(self.window._stop_event.is_set())
        self.assertIs(future.result(timeout=1), False)
        cancel.assert_called_once_with()
        self.assertIs(self.window.page_stack.currentWidget(), self.window.chat)
        self.assertTrue(self.window.composer.isVisible())
        self.assertEqual(
            list(self.window.chat._body.findChildren(gui.CopilotApprovalCard)), [])
        fake_agent.save_conversation_history.assert_called_once_with([])

    def test_a_closed_turn_bridge_denies_late_requests_without_a_card(self):
        self.start_turn("first request")
        old_bridge = self.window._copilot_bridge
        self.window._end_turn()
        self.start_turn("second request")

        future = old_bridge(self.approval_payload())
        self.app.processEvents()

        self.assertTrue(future.done())
        self.assertIs(future.result(timeout=1), False)
        self.assertEqual(
            list(self.window.chat._body.findChildren(gui.CopilotApprovalCard)), [])

    def test_approval_arriving_after_stop_is_denied_without_a_card(self):
        self.start_turn()
        bridge = self.window._copilot_bridge
        self.window._request_stop()
        future = bridge(self.approval_payload())
        self.app.processEvents()
        self.assertTrue(future.done())
        self.assertIs(future.result(timeout=1), False)
        self.assertEqual(
            list(self.window.chat._body.findChildren(gui.CopilotApprovalCard)), [])

    def test_safe_worker_progress_reaches_the_main_task_workspace(self):
        fake_agent = _FakeAgent()
        worker = gui.AgentWorker("control Notepad", [])
        worker.step.connect(self.window._on_worker_step)
        self.window.task_workspace.set_task_title("Control Notepad")
        with mock.patch.object(gui, "get_agent", return_value=fake_agent):
            worker._run()
        self.app.processEvents()
        activity = self.window.task_workspace.steps_text()
        self.assertIn("Copilot: Performing step 2", activity)
        self.assertNotIn("private", activity)

    def test_shutdown_denies_pending_approvals_and_detaches_the_bridge(self):
        self.start_turn()
        _, visible_future, _ = self.show_approval()
        queued = self.approval_payload()
        queued["request_id"] = "request-2"
        bridge = self.window._copilot_bridge
        bridge.requested.disconnect()
        queued_future = bridge(queued)

        with mock.patch.object(sc, "shutdown") as shutdown:
            self.window._on_about_to_quit()

        self.assertTrue(self.window._stop_event.is_set())
        self.assertIs(visible_future.result(timeout=1), False)
        self.assertIs(queued_future.result(timeout=1), False)
        self.assertEqual(
            list(self.window.chat._body.findChildren(gui.CopilotApprovalCard)), [])
        self.assertIsNone(self.window._copilot_bridge)
        shutdown.assert_called_once_with()

    def test_quit_waits_for_the_main_worker_and_cancels_its_turn(self):
        entered = threading.Event()
        release = threading.Event()
        worker = gui.AgentWorker("control Notepad", [])
        self.window._worker = worker
        self.window._processing = True
        self.window._turn_cancellable = True

        def gated_turn():
            entered.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test never released the agent turn")

        worker._run_turn = gated_turn
        worker.start()
        self.assertTrue(entered.wait(timeout=1))
        thread = worker._thread
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sc, "shutdown"))
            stack.enter_context(mock.patch.object(
                gui, "get_agent", return_value=mock.Mock()))
            stack.enter_context(mock.patch.object(
                gui, "_voice_output_module", None))
            stack.enter_context(mock.patch.object(
                self.window, "_stop_event_bus"))
            stack.enter_context(mock.patch.object(
                self.window, "_stop_telegram_doc_service"))
            stack.enter_context(mock.patch.object(
                self.window, "_telegram_service", mock.Mock()))
            stack.enter_context(mock.patch.object(
                self.window, "_wake_word_service", mock.Mock()))
            stack.enter_context(mock.patch.object(
                self.window, "_spontaneous_timer", mock.Mock()))
            stack.enter_context(mock.patch.object(
                self.window, "_media_poller", mock.Mock()))
            stack.enter_context(mock.patch.object(
                self.window, "_media_timer", mock.Mock()))
            stack.enter_context(mock.patch("os_control.stop_kill_switch"))
            stack.enter_context(mock.patch(
                "os_control.set_gui_visibility_handlers"))
            stack.enter_context(mock.patch(
                "os_control.set_gui_capture_handler"))
            stack.enter_context(mock.patch("browser_agent.shutdown"))
            stack.enter_context(mock.patch(
                "browser_agent.set_approval_handler"))
            quit_app = stack.enter_context(mock.patch.object(self.app, "quit"))

            try:
                self.window.quit_app()
                self.assertTrue(self.window._stop_event.is_set())
                self.assertTrue(thread.is_alive())
                quit_app.assert_not_called()
            finally:
                release.set()
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            QTest.qWait(150)
            quit_app.assert_called_once_with()


if __name__ == "__main__":
    unittest.main(verbosity=2)
