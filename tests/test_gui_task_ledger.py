"""Task Master ledger UI contract (offscreen Qt).

Run: python -m unittest tests.test_gui_task_ledger -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtCore import QEvent, QPoint  # noqa: E402
from PySide6.QtTest import QSignalSpy  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

import agent_gui_qt as g  # noqa: E402


class TaskLedgerGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "task_ledger.json"
        self.window = None

    def tearDown(self):
        if self.window is not None:
            self.window.close()
            self.window.deleteLater()
            self.app.processEvents()
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()

    def open_window(self):
        self.window = g.AgentWindow(task_ledger_path=self.path)
        self.window.resize(1480, 820)
        self.window.show()
        self.window.show_greeting()
        for _ in range(10):
            self.app.processEvents()
        return self.window

    def add_task(self, title):
        task = self.window.task_ledger.add_task(title)
        self.window._refresh_task_ledger()
        self.app.processEvents()
        return task

    def card(self):
        cards = self.window.chat._body.findChildren(g.TaskLedgerCard)
        self.assertEqual(len(cards), 1)
        return cards[0]

    @staticmethod
    def button_with(buttons, *, action=None, task_id=None):
        return next(
            button for button in buttons
            if (action is None or button.property("action") == action)
            and (task_id is None or button.property("taskId") == task_id)
        )

    def test_chat_card_is_visible_and_excluded_from_copy_all(self):
        window = self.open_window()
        card = self.card()
        window.chat.add_message(g.UserMessage("hello"), fade=False)
        self.add_task("Private ledger title")

        self.assertTrue(card.isVisible())
        self.assertFalse(hasattr(card, "raw_text"))
        self.assertEqual(window._chat_as_text(), "You: hello")
        self.assertIn("1 open", card.summary_label.text())

    def test_add_from_chat_updates_storage_card_and_workspace_immediately(self):
        window = self.open_window()
        card = self.card()

        with mock.patch.object(g.QInputDialog, "getText",
                               return_value=("Ship the ledger", True)):
            card.add_button.click()
        self.app.processEvents()

        self.assertEqual(window.task_ledger.list_tasks()[0]["title"],
                         "Ship the ledger")
        self.assertIn("1 open", card.summary_label.text())
        self.assertIn("1 open", window.task_workspace.ledger_summary.text())
        self.assertTrue(any(
            "Ship the ledger" in button.text()
            for button in window.task_workspace.ledger_buttons))

    def test_workspace_click_emits_qt_signal_and_updates_chat_focus(self):
        window = self.open_window()
        first = self.add_task("First")
        second = self.add_task("Second")
        spy = QSignalSpy(window.task_ledger_changed)
        button = self.button_with(
            window.task_workspace.ledger_buttons, task_id=second["id"])

        button.click()
        self.app.processEvents()

        self.assertGreaterEqual(spy.count(), 1)
        self.assertEqual(window.task_ledger.focused_task_id, second["id"])
        self.assertEqual(self.card().focused_task_id, second["id"])
        self.assertNotEqual(first["id"], second["id"])

    def test_chat_click_updates_workspace_and_keeps_focused_task_visible(self):
        window = self.open_window()
        self.add_task("First")
        self.add_task("Second")
        third = self.add_task("Third")
        button = self.button_with(
            self.card().task_buttons, task_id=third["id"])

        button.click()
        self.app.processEvents()

        self.assertEqual(window.task_ledger.focused_task_id, third["id"])
        self.assertEqual(window.task_workspace.focused_task_id, third["id"])
        self.assertEqual(len(window.task_workspace.ledger_buttons), 2)
        self.assertEqual(
            window.task_workspace.ledger_buttons[0].property("taskId"),
            third["id"])

    def test_card_lifecycle_controls_persist_each_transition(self):
        window = self.open_window()
        task = self.add_task("Lifecycle task")
        card = self.card()

        self.button_with(card.findChildren(QPushButton),
                         action="start", task_id=task["id"]).click()
        self.assertEqual(window.task_ledger.list_tasks()[0]["status"], "active")

        self.button_with(card.findChildren(QPushButton),
                         action="done", task_id=task["id"]).click()
        completed = window.task_ledger.list_tasks()[0]
        self.assertEqual(completed["status"], "done")
        self.assertIsNotNone(completed["completed_at"])

        self.button_with(card.findChildren(QPushButton),
                         action="reopen", task_id=task["id"]).click()
        reopened = window.task_ledger.list_tasks()[0]
        self.assertEqual(reopened["status"], "planned")
        self.assertIsNone(reopened["completed_at"])

        with mock.patch.object(
                g.QMessageBox, "question",
                return_value=g.QMessageBox.StandardButton.Yes):
            self.button_with(card.findChildren(QPushButton),
                             action="delete", task_id=task["id"]).click()
        self.assertEqual(window.task_ledger.list_tasks(), [])

    def test_task_titles_render_as_literal_plain_text(self):
        self.open_window()
        title = '<b>literal</b> & "quoted"'
        task = self.add_task(title)
        button = self.button_with(
            self.card().task_buttons, task_id=task["id"])

        self.assertIn(title, button.text())
        self.assertNotIn("<b>literal</b>", self.card().summary_label.text())

    def test_workspace_keeps_live_activity_visible_beside_two_tasks(self):
        window = self.open_window()
        self.add_task("First ledger task")
        self.add_task("Second ledger task")
        workspace = window.task_workspace
        workspace.set_task_title("Organize Downloads")
        workspace.set_activity([
            "Action: read_file(Downloads) -> Result: 12 files found",
            "Action: inspect_files -> Result: three archives detected",
            "Action: verify_output -> Result: destination is writable",
            "Waiting for the next approved step",
        ])
        self.app.processEvents()

        self.assertEqual(workspace.height(), g.TASK_WORKSPACE_H)
        self.assertEqual(len(workspace.ledger_buttons), 2)
        self.assertTrue(workspace.task_label.isVisible())
        self.assertTrue(workspace.steps_label.isVisible())
        self.assertGreaterEqual(workspace.steps_label.height(), 44)
        bottom = workspace.steps_label.mapTo(
            workspace, QPoint(0, workspace.steps_label.height())).y()
        self.assertLessEqual(bottom, workspace.height() - 10)
        self.assertIn("12 files found", workspace.steps_text())

    def test_new_chat_recreates_the_card_without_losing_ledger_state(self):
        window = self.open_window()
        self.add_task("Persistent across chats")
        agent = mock.Mock()

        with mock.patch.object(g, "get_agent", return_value=agent):
            window.new_chat()
        self.app.processEvents()

        self.assertEqual(len(window.chat._body.findChildren(g.TaskLedgerCard)), 1)
        self.assertEqual(window.task_ledger.list_tasks()[0]["title"],
                         "Persistent across chats")
        self.assertIn("1 open", window.task_workspace.ledger_summary.text())
        agent.save_conversation_history.assert_called_once_with([])

    def test_replacing_card_does_not_accumulate_signal_receivers(self):
        window = self.open_window()

        def receiver_count():
            return window.receivers("2task_ledger_changed(PyObject)")

        initial = receiver_count()
        for _ in range(3):
            with mock.patch.object(g, "get_agent", return_value=mock.Mock()):
                window.new_chat()
            self.app.processEvents()
            self.assertEqual(receiver_count(), initial)
        self.assertEqual(len(window.chat._body.findChildren(g.TaskLedgerCard)), 1)
        self.add_task("Still synchronized after resets")
        self.assertIn("1 open", self.card().summary_label.text())
        self.assertIn("1 open", window.task_workspace.ledger_summary.text())

    def test_workspace_shows_at_most_two_open_tasks(self):
        window = self.open_window()
        self.add_task("One")
        self.add_task("Two")
        self.add_task("Three")

        self.assertEqual(len(window.task_workspace.ledger_buttons), 2)
        self.assertIn("3 open", window.task_workspace.ledger_summary.text())

    def test_corrupt_ledger_is_visible_and_never_overwritten(self):
        original = b'{"version": 1, "tasks": ['
        self.path.write_bytes(original)
        window = self.open_window()
        card = self.card()

        self.assertIsNone(window.task_ledger)
        self.assertTrue(card.error_label.isVisible())
        self.assertTrue(card.error_label.text())
        self.assertIn("unavailable", window.task_workspace.ledger_summary.text())
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
