"""Persistent Task Master ledger storage tests.

Run: python -m unittest tests.test_task_ledger -v
"""
import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import task_ledger


class TaskLedgerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "task_ledger.json"

    def load(self):
        return task_ledger.TaskLedger(self.path)

    def test_missing_file_is_empty_and_not_created_by_loading(self):
        ledger = self.load()

        self.assertEqual(ledger.list_tasks(), [])
        self.assertIsNone(ledger.focused_task_id)
        self.assertFalse(self.path.exists())

    def test_added_task_persists_with_identity_status_and_timestamps(self):
        ledger = self.load()

        task = ledger.add_task("  Implement Task Master ledger  ")

        self.assertTrue(task["id"])
        self.assertEqual(task["title"], "Implement Task Master ledger")
        self.assertEqual(task["status"], "planned")
        self.assertIsNone(task["completed_at"])
        created = datetime.datetime.fromisoformat(task["created_at"])
        updated = datetime.datetime.fromisoformat(task["updated_at"])
        self.assertIsNotNone(created.tzinfo)
        self.assertIsNotNone(updated.tzinfo)
        self.assertEqual(created, updated)

        reloaded = self.load().list_tasks()
        self.assertEqual(reloaded, [task])
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["tasks"], [task])

    def test_task_ids_are_unique(self):
        ledger = self.load()

        first = ledger.add_task("First task")
        second = ledger.add_task("Second task")

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(ledger.list_tasks(), [first, second])

    def test_activating_one_task_returns_previous_active_to_planned(self):
        ledger = self.load()
        first = ledger.add_task("First task")
        second = ledger.add_task("Second task")

        ledger.activate_task(first["id"])
        active_second = ledger.activate_task(second["id"])

        tasks = {task["id"]: task for task in ledger.list_tasks()}
        self.assertEqual(tasks[first["id"]]["status"], "planned")
        self.assertEqual(tasks[second["id"]]["status"], "active")
        self.assertEqual(active_second["status"], "active")
        self.assertEqual(sum(task["status"] == "active"
                             for task in tasks.values()), 1)

    def test_complete_and_reopen_update_lifecycle_timestamps(self):
        ledger = self.load()
        task = ledger.add_task("Finish storage")
        ledger.activate_task(task["id"])

        completed = ledger.complete_task(task["id"])

        self.assertEqual(completed["status"], "done")
        self.assertIsNotNone(completed["completed_at"])
        completed_at = datetime.datetime.fromisoformat(completed["completed_at"])
        self.assertIsNotNone(completed_at.tzinfo)

        reopened = ledger.reopen_task(task["id"])

        self.assertEqual(reopened["status"], "planned")
        self.assertIsNone(reopened["completed_at"])
        self.assertEqual(self.load().list_tasks(), [reopened])

    def test_focus_is_independent_of_status_and_persists(self):
        ledger = self.load()
        task = ledger.add_task("Focused task")
        ledger.complete_task(task["id"])

        focused = ledger.focus_task(task["id"])

        self.assertEqual(focused["id"], task["id"])
        self.assertEqual(ledger.focused_task_id, task["id"])
        self.assertEqual(self.load().focused_task_id, task["id"])

    def test_delete_removes_task_and_clears_deleted_focus(self):
        ledger = self.load()
        kept = ledger.add_task("Keep")
        removed = ledger.add_task("Remove")
        ledger.focus_task(removed["id"])

        ledger.delete_task(removed["id"])

        self.assertEqual(ledger.list_tasks(), [kept])
        self.assertIsNone(ledger.focused_task_id)
        self.assertEqual(self.load().list_tasks(), [kept])

    def test_returned_tasks_are_copies(self):
        ledger = self.load()
        task = ledger.add_task("Immutable copy")

        task["title"] = "Changed outside the ledger"
        listed = ledger.list_tasks()
        listed[0]["status"] = "done"

        self.assertEqual(ledger.list_tasks()[0]["title"], "Immutable copy")
        self.assertEqual(ledger.list_tasks()[0]["status"], "planned")

    def test_invalid_titles_are_rejected_without_writing(self):
        ledger = self.load()
        original = b'{"version": 1, "focused_task_id": null, "tasks": []}\n'
        self.path.write_bytes(original)

        for title in ("", "   ", "x" * 201, None):
            with self.subTest(title=title):
                with self.assertRaises(ValueError):
                    ledger.add_task(title)
                self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(ledger.list_tasks(), [])

    def test_unknown_task_ids_are_rejected_without_writing(self):
        ledger = self.load()
        task = ledger.add_task("Existing")
        original = self.path.read_bytes()

        for operation in (
            ledger.focus_task,
            ledger.activate_task,
            ledger.complete_task,
            ledger.reopen_task,
            ledger.delete_task,
        ):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(KeyError):
                    operation("missing")
                self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(ledger.list_tasks(), [task])

    def test_invalid_status_transitions_are_rejected(self):
        ledger = self.load()
        task = ledger.add_task("Lifecycle")
        completed = ledger.complete_task(task["id"])

        with self.assertRaises(ValueError):
            ledger.activate_task(task["id"])
        with self.assertRaises(ValueError):
            ledger.complete_task(task["id"])

        self.assertEqual(ledger.list_tasks(), [completed])
        reopened = ledger.reopen_task(task["id"])
        self.assertEqual(reopened["status"], "planned")
        with self.assertRaises(ValueError):
            ledger.reopen_task(task["id"])

    def test_corrupt_json_is_reported_without_overwriting_the_file(self):
        original = b'{"version": 1, "tasks": ['
        self.path.write_bytes(original)

        with self.assertRaises(task_ledger.TaskLedgerError):
            self.load()

        self.assertEqual(self.path.read_bytes(), original)

    def test_unsupported_schema_version_is_reported_without_overwriting(self):
        original = b'{"version": 99, "focused_task_id": null, "tasks": []}\n'
        self.path.write_bytes(original)

        with self.assertRaises(task_ledger.TaskLedgerError):
            self.load()

        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_persisted_state_is_reported_without_overwriting(self):
        timestamp = "2026-11-27T12:00:00+00:00"
        payloads = [
            {
                "version": 1,
                "focused_task_id": "missing",
                "tasks": [],
            },
            {
                "version": 1,
                "focused_task_id": None,
                "tasks": [
                    {
                        "id": "one",
                        "title": "One",
                        "status": "active",
                        "created_at": timestamp,
                        "updated_at": timestamp,
                        "completed_at": None,
                    },
                    {
                        "id": "two",
                        "title": "Two",
                        "status": "active",
                        "created_at": timestamp,
                        "updated_at": timestamp,
                        "completed_at": None,
                    },
                ],
            },
            {
                "version": 1,
                "focused_task_id": None,
                "tasks": [
                    {
                        "id": "bad",
                        "title": "Bad",
                        "status": "unknown",
                        "created_at": timestamp,
                        "updated_at": timestamp,
                        "completed_at": None,
                    },
                ],
            },
            {
                "version": 1,
                "focused_task_id": None,
                "tasks": [
                    {
                        "id": "bad-shape",
                        "title": "Bad shape",
                        "status": ["active"],
                        "created_at": timestamp,
                        "updated_at": timestamp,
                        "completed_at": None,
                    },
                ],
            },
        ]

        for payload in payloads:
            with self.subTest(payload=payload):
                original = json.dumps(payload).encode("utf-8")
                self.path.write_bytes(original)
                with self.assertRaises(task_ledger.TaskLedgerError):
                    self.load()
                self.assertEqual(self.path.read_bytes(), original)

    def test_failed_atomic_replace_rolls_back_memory_and_removes_temp_file(self):
        ledger = self.load()
        existing = ledger.add_task("Existing")
        original = self.path.read_bytes()

        with mock.patch.object(task_ledger.os, "replace",
                               side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                ledger.add_task("Not saved")

        self.assertEqual(ledger.list_tasks(), [existing])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob("task_ledger.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
