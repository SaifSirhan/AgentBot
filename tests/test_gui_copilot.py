"""Single-window screen copilot GUI contract (offscreen Qt)."""
import contextlib
import os
import sys
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtCore import QEvent, QThread, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QSplitter,
)

import agent_gui_qt as gui  # noqa: E402
import screen_copilot as sc  # noqa: E402


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
        self._refresh_patch = mock.patch.object(
            self.window, "_refresh_copilot_targets")
        self._refresh_patch.start()

    def tearDown(self):
        self._refresh_patch.stop()
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

    def show_approval(self, timeout_seconds=60):
        payload = self.approval_payload(timeout_seconds)
        future = self.window._copilot_bridge(payload)
        self.app.processEvents()
        card = self.window.copilot_view.approval_area.layout().itemAt(0).widget()
        self.assertIsInstance(card, gui.CopilotApprovalCard)
        return payload, future, card

    def test_copilot_is_a_page_in_the_same_main_window(self):
        self.assertIn("Copilot", self.window.nav_rail.buttons)
        self.window._show_copilot_page()
        self.assertEqual(
            self.window.page_stack.currentWidget(), self.window.copilot_view)
        self.assertFalse(self.window.composer.isVisible())
        self.assertEqual(
            sum(isinstance(w, QMainWindow) for w in self.app.topLevelWidgets()), 1)
        self.assertFalse(any(
            isinstance(w, QDialog) for w in self.app.topLevelWidgets()))
        self.assertFalse(any(
            isinstance(w, QSplitter) for w in self.window.findChildren(QSplitter)))
        self.assertIsNotNone(self.window.owl_stage)
        self.assertIsNotNone(self.window.task_workspace)

        self.window._show_chat_page()
        self.assertEqual(self.window.page_stack.currentWidget(), self.window.chat)
        self.assertTrue(self.window.composer.isVisible())

    def test_goal_is_capped_and_run_emits_target_hwnd_and_goal(self):
        self.window._show_copilot_page()
        view = self.window.copilot_view
        self.assertIsInstance(view.goal_input, QPlainTextEdit)
        view.target_combo.addItem("Fixture — fixture.exe", 101)
        view.goal_input.setPlainText("x" * 501)
        self.assertEqual(len(view.goal_input.toPlainText()), 500)
        view.goal_input.setPlainText("Search for Dune and open it")
        requests = []
        view.run_requested.connect(lambda hwnd, goal: requests.append((hwnd, goal)))
        view.run_btn.click()
        self.assertEqual(requests, [(101, "Search for Dune and open it")])

    def test_approve_plan_resolves_true_once_and_removes_the_card(self):
        payload, future, card = self.show_approval()
        text = "\n".join(label.text() for label in card.findChildren(QLabel))
        for expected in (
            payload["goal"], "Fixture", "fixture.exe", "202", "101",
            "5", "click", "type_text", "vision_click",
            payload["typing_rule"], payload["screenshot_disclosure"],
        ):
            self.assertIn(expected, text)
        card.approve_btn.click()
        self.assertIs(future.result(timeout=1), True)
        card.resolve(False)
        self.assertIs(future.result(timeout=1), True)
        self.app.processEvents()
        self.assertEqual(self.window.copilot_view.approval_area.layout().count(), 0)

    def test_deny_and_expiry_resolve_false_once(self):
        for expire in (False, True):
            with self.subTest(expire=expire):
                payload, future, card = self.show_approval()
                if expire:
                    card._expire()
                else:
                    card.deny_btn.click()
                self.assertIs(future.result(timeout=1), False)
                card.resolve(True)
                self.assertIs(future.result(timeout=1), False)
                self.app.processEvents()
                self.assertEqual(
                    self.window.copilot_view.approval_area.layout().count(), 0)

    def test_stop_and_escape_cancel_the_worker_event(self):
        self.window._show_copilot_page()
        self.window._copilot_active = True
        self.window.copilot_view.set_running(True)
        self.window._copilot_stop_event = threading.Event()
        self.window._copilot_worker = mock.Mock()
        with mock.patch.object(sc, "cancel_current_task") as cancel:
            self.window.copilot_view.stop_btn.click()
            self.assertTrue(self.window._copilot_stop_event.is_set())
            self.window._copilot_worker.stop.assert_called_once_with()
            cancel.assert_called_once_with()

            self.window._copilot_stop_event.clear()
            QTest.keyPress(self.window, Qt.Key.Key_Escape)
            self.assertTrue(self.window._copilot_stop_event.is_set())
            self.assertEqual(cancel.call_count, 2)

    def test_new_chat_cancels_approval_and_returns_to_chat(self):
        self.window._show_copilot_page()
        self.window._copilot_active = True
        self.window._copilot_stop_event = threading.Event()
        payload, future, _ = self.show_approval()
        agent = mock.Mock()
        with mock.patch.object(gui, "get_agent", return_value=agent), \
                mock.patch.object(sc, "cancel_current_task") as cancel:
            self.window.new_chat()
        self.assertTrue(self.window._copilot_stop_event.is_set())
        self.assertIs(future.result(timeout=1), False)
        cancel.assert_called_once_with()
        self.assertEqual(self.window.page_stack.currentWidget(), self.window.chat)
        self.assertTrue(self.window.composer.isVisible())
        self.assertEqual(self.window.copilot_view.approval_area.layout().count(), 0)
        agent.save_conversation_history.assert_called_once_with([])

    def test_cancel_resolves_visible_and_queued_approvals(self):
        self.window._show_copilot_page()
        self.window._copilot_active = True
        self.window._copilot_stop_event = threading.Event()
        _, visible_future, _ = self.show_approval()
        queued = self.approval_payload()
        queued["request_id"] = "request-2"
        self.window._copilot_bridge.requested.disconnect()
        queued_future = self.window._copilot_bridge(queued)

        with mock.patch.object(sc, "cancel_current_task"):
            self.window._cancel_copilot()

        self.assertTrue(visible_future.done())
        self.assertTrue(queued_future.done(),
                        "cancellation left an undisplayed approval pending")
        self.assertIs(visible_future.result(timeout=1), False)
        self.assertIs(queued_future.result(timeout=1), False)
        self.assertEqual(
            self.window.copilot_view.approval_area.layout().count(), 0)

    def test_approval_arriving_after_stop_is_denied_without_a_card(self):
        self.window._show_copilot_page()
        self.window._copilot_active = True
        self.window._copilot_stop_event = threading.Event()
        self.window._copilot_stop_event.set()

        future = self.window._copilot_bridge(self.approval_payload())
        self.app.processEvents()

        self.assertTrue(future.done(),
                        "a late approval request must be denied immediately")
        self.assertIs(future.result(timeout=1), False)
        self.assertEqual(
            self.window.copilot_view.approval_area.layout().count(), 0)

    def test_shutdown_resolves_pending_approvals_and_detaches_bridge(self):
        self.window._show_copilot_page()
        self.window._copilot_active = True
        self.window._copilot_stop_event = threading.Event()
        _, visible_future, _ = self.show_approval()
        queued = self.approval_payload()
        queued["request_id"] = "request-2"
        self.window._copilot_bridge.requested.disconnect()
        queued_future = self.window._copilot_bridge(queued)

        with mock.patch.object(sc, "shutdown"), \
                mock.patch.object(self.window, "_stop_event_bus"):
            self.window._on_about_to_quit()

        self.assertTrue(visible_future.done())
        self.assertTrue(queued_future.done())
        self.assertIs(visible_future.result(timeout=1), False)
        self.assertIs(queued_future.result(timeout=1), False)
        self.assertEqual(
            self.window.copilot_view.approval_area.layout().count(), 0)
        self.assertIsNone(self.window._copilot_bridge)

    def test_stale_target_results_cannot_overwrite_a_newer_scan(self):
        old = gui.CopilotWorker(mode="list")
        current = gui.CopilotWorker(mode="list")
        self.window._copilot_target_worker = current
        self.window.copilot_view.target_combo.addItem("Current", 202)
        old.targets_ready.connect(self.window._on_copilot_targets)

        old.targets_ready.emit([])
        self.app.processEvents()

        self.assertIs(self.window._copilot_target_worker, current)
        self.assertEqual(self.window.copilot_view.target_combo.count(), 1)
        self.assertEqual(self.window.copilot_view.target_combo.itemData(0), 202)

    def test_stale_target_failure_cannot_clear_a_newer_scan(self):
        old = gui.CopilotWorker(mode="list")
        current = gui.CopilotWorker(mode="list")
        self.window._copilot_target_worker = current
        self.window.copilot_view.status_label.setText("Current scan")
        old.failed.connect(self.window._on_copilot_targets_failed)

        old.failed.emit("Window scan failed.")
        self.app.processEvents()

        self.assertIs(self.window._copilot_target_worker, current)
        self.assertEqual(self.window.copilot_view.status_label.text(),
                         "Current scan")

    def test_target_scan_callbacks_are_ignored_during_a_run(self):
        scan = gui.CopilotWorker(mode="list")
        self.window._copilot_target_worker = scan
        self.window._copilot_active = True
        self.window.copilot_view.target_combo.addItem("Current", 202)
        self.window.copilot_view.status_label.setText("Current run")
        scan.targets_ready.connect(self.window._on_copilot_targets)
        scan.failed.connect(self.window._on_copilot_targets_failed)

        scan.targets_ready.emit([])
        self.assertEqual(self.window.copilot_view.target_combo.count(), 1)
        self.assertEqual(self.window.copilot_view.target_combo.itemData(0), 202)
        self.assertEqual(self.window.copilot_view.status_label.text(),
                         "Current run")
        scan.failed.emit("Window scan failed.")
        self.assertEqual(self.window.copilot_view.status_label.text(),
                         "Current run")

    def test_stale_scan_status_cannot_overwrite_run_status(self):
        scan = gui.CopilotWorker(mode="list")
        run = gui.CopilotWorker(mode="run")
        self.window._copilot_target_worker = scan
        self.window._copilot_worker = run
        self.window._copilot_active = True
        self.window.copilot_view.status_label.setText("Current run")
        scan.status.connect(self.window._on_copilot_status)

        scan.status.emit("Scanning eligible windows…")
        self.assertEqual(self.window.copilot_view.status_label.text(),
                         "Current run")

    def test_worker_is_owned_by_app_and_deleted_on_gui_thread(self):
        worker = gui.CopilotWorker(mode="list")
        self.assertIs(worker.parent(), self.app)
        destroyed_on = []
        worker.destroyed.connect(
            lambda *_args: destroyed_on.append(QThread.currentThread()))
        with mock.patch.object(sc, "list_copilot_targets", return_value=[]):
            worker._run()
        self.app.processEvents()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.assertEqual(destroyed_on, [self.app.thread()])

    def test_background_worker_starts_nonblocking_and_queues_results(self):
        entered = threading.Event()
        release = threading.Event()
        captured_stop_events = []

        def gated_scan(*, stop_event=None):
            captured_stop_events.append(stop_event)
            entered.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test never released the target scan")
            return []

        worker = gui.CopilotWorker(mode="list")
        targets = []
        received_on = []

        def receive(result):
            received_on.append(QThread.currentThread())
            targets.append(result)

        worker.targets_ready.connect(receive)
        scan_patch = mock.patch.object(
            sc, "list_copilot_targets", side_effect=gated_scan)
        scan_patch.start()
        self.addCleanup(scan_patch.stop)
        thread = None
        try:
            worker.start()
            thread = worker._thread
            self.assertTrue(entered.wait(timeout=1))
            self.assertTrue(thread.is_alive())
            self.assertTrue(thread.daemon)
            self.assertEqual(targets, [],
                             "worker signals must wait for the GUI event loop")
        finally:
            release.set()
            if thread is not None:
                thread.join(timeout=2)
        self.assertFalse(thread.is_alive(), "target scan did not terminate")
        self.assertEqual(captured_stop_events, [worker.stop_event])
        self.app.processEvents()
        self.assertEqual(targets, [[]])
        self.assertEqual(received_on, [self.app.thread()])

    def test_shutdown_blocks_new_copilot_workers(self):
        self.window._shutting_down = True
        with mock.patch.object(gui, "CopilotWorker") as worker_type:
            gui.AgentWindow._refresh_copilot_targets(self.window)
            self.window._start_copilot_run(101, "Open Dune")
        worker_type.assert_not_called()
        self.assertEqual(self.window._copilot_workers, [])
        self.assertIsNone(self.window._copilot_target_worker)
        self.assertFalse(self.window._copilot_active)

    def test_quit_waits_for_blocked_copilot_workers(self):
        scan_entered = threading.Event()
        scan_stopped = threading.Event()
        scan_release = threading.Event()
        captured_scan_stop_events = []
        entered = threading.Event()
        release = threading.Event()

        def gated_scan(*, stop_event=None):
            captured_scan_stop_events.append(stop_event)
            scan_entered.set()
            if stop_event is None or not stop_event.wait(timeout=2):
                raise RuntimeError("test never stopped the target scan")
            scan_stopped.set()
            if not scan_release.wait(timeout=2):
                raise RuntimeError("test never released the target scan")
            return []

        def gated_run(**_kwargs):
            entered.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test never released the copilot task")
            return sc.CopilotResult(
                status="cancelled", summary="Stopped.", task_id="task-1")

        self.window._show_copilot_page()
        self.window.copilot_view.target_combo.addItem("Fixture", 101)
        self.window.copilot_view.goal_input.setPlainText("Open Dune")
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                sc, "list_copilot_targets", side_effect=gated_scan))
            stack.enter_context(mock.patch.object(
                sc, "run_copilot_task", side_effect=gated_run))
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

            gui.AgentWindow._refresh_copilot_targets(self.window)
            self.assertTrue(scan_entered.wait(timeout=1))
            target_worker = self.window._copilot_target_worker
            target_thread = target_worker._thread
            self.assertEqual(
                captured_scan_stop_events, [target_worker.stop_event])
            self.window._start_copilot_run(101, "Open Dune")
            self.assertTrue(entered.wait(timeout=1))
            worker = self.window._copilot_worker
            thread = worker._thread
            try:
                self.window.quit_app()
                self.assertTrue(worker.stop_event.is_set())
                self.assertTrue(target_worker.stop_event.is_set())
                self.assertTrue(scan_stopped.wait(timeout=1))
                self.assertTrue(thread.is_alive())
                self.assertTrue(target_thread.is_alive())
                quit_app.assert_not_called()
            finally:
                release.set()
                scan_release.set()
                target_worker.stop_event.set()
                thread.join(timeout=2)
                target_thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), "copilot task did not terminate")
            self.assertFalse(target_thread.is_alive(),
                             "target scan did not terminate")
            QTest.qWait(150)
            quit_app.assert_called_once_with()

    def test_progress_updates_only_the_safe_status_label(self):
        self.window._show_copilot_page()
        view = self.window.copilot_view
        view.goal_input.setPlainText("Search for Dune and open it")
        self.window._update_copilot_progress({
            "event": "action",
            "step": 2,
            "action": "type_text",
            "status": "started",
            "goal": "private raw goal",
            "text": "private typed text",
        })
        self.assertIn("2", view.status_label.text())
        self.assertIn("type_text", view.status_label.text())
        self.assertNotIn("private", view.status_label.text())
        self.assertEqual(view.goal_input.toPlainText(), "Search for Dune and open it")

    def test_worker_passes_one_approval_bridge_and_redacts_failures(self):
        stop_event = threading.Event()
        approval_handler = mock.Mock()
        captured = {}

        def run(**kwargs):
            captured.update(kwargs)
            kwargs["progress_handler"]({
                "event": "planning", "step": 1, "action": "", "status": "started"})
            return sc.CopilotResult(
                status="completed", summary="Planner marked the goal complete.",
                task_id="task-1", steps=1, changed=True, last_action="click")

        worker = gui.CopilotWorker(
            mode="run", hwnd=101, goal="Search for Dune and open it",
            stop_event=stop_event, approval_handler=approval_handler)
        progress = []
        finished = []
        worker.progress.connect(progress.append)
        worker.finished.connect(finished.append)
        with mock.patch.object(sc, "run_copilot_task", side_effect=run):
            worker._run()
        self.assertIs(captured["approval_handler"], approval_handler)
        self.assertIs(captured["stop_event"], stop_event)
        self.assertIs(captured["vision_locator"], sc.deepseek_vision_locator)
        self.assertEqual(len(progress), 1)
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].status, "completed")

        failed = []
        worker.failed.connect(failed.append)

        def fail(**kwargs):
            raise RuntimeError("private provider response")

        with mock.patch.object(sc, "run_copilot_task", side_effect=fail):
            worker._run()
        self.assertEqual(len(failed), 1)
        self.assertNotIn("private", failed[0])

    def test_target_results_populate_hwnd_item_data(self):
        self.window._show_copilot_page()
        target = sc.TargetWindow(
            hwnd=101,
            title="Fixture",
            process=sc.ProcessIdentity(
                pid=202, executable=r"C:\Apps\fixture.exe", started_at=3,
                elevated=False),
            visible=True,
            bounds=sc.WindowBounds(10, 20, 310, 220),
        )
        self.window._on_copilot_targets([target])
        combo = self.window.copilot_view.target_combo
        self.assertEqual(combo.count(), 1)
        self.assertEqual(combo.itemData(0), 101)
        self.assertIn("Fixture", combo.itemText(0))
        self.assertIn("fixture.exe", combo.itemText(0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
