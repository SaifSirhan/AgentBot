"""Structured memory schema, migration, scoping, and CRUD tests.

Run: python -m unittest tests.test_memory_store -v
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import memory


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="memory_store_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.other = self.root / "other"
        self.project.mkdir()
        self.other.mkdir()

        patcher = mock.patch.object(
            memory._cfg, "memory_path",
            side_effect=lambda *parts: str(self.root.joinpath(*parts)))
        patcher.start()
        self.addCleanup(patcher.stop)
        cwd_patcher = mock.patch.object(
            memory, "current_dir", return_value=str(self.project))
        cwd_patcher.start()
        self.addCleanup(cwd_patcher.stop)

    def raw(self):
        return json.loads((self.root / "facts.json").read_text(encoding="utf-8"))

    def test_add_returns_the_structured_record(self):
        record = memory.add_memory(
            "AgentBot uses PySide6 for its GUI.",
            scope=self.project,
            keywords=["pyside6", "gui"])

        self.assertIsNotNone(record)
        for field in ("id", "title", "scope", "keywords", "content",
                      "created_at", "kind"):
            self.assertIn(field, record)
        self.assertEqual(record["scope"], str(self.project))
        self.assertEqual(record["keywords"], ["pyside6", "gui"])
        self.assertEqual(record["kind"], "fact")

        raw = self.raw()
        self.assertEqual(raw["version"], memory.MEMORY_VERSION)
        self.assertEqual(raw["memories"][0]["content"],
                         "AgentBot uses PySide6 for its GUI.")

    def test_bare_list_of_strings_is_migrated(self):
        (self.root / "facts.json").write_text(
            json.dumps(["I like green tea.", "My cat is named Mochi."]),
            encoding="utf-8")

        records = memory.list_memories(include_all=True)

        self.assertEqual(len(records), 2)
        for record in records:
            self.assertTrue(record["id"])
            self.assertTrue(record["title"])
            self.assertTrue(record["created_at"])
            self.assertEqual(record["scope"], memory.GLOBAL_SCOPE)
            self.assertEqual(record["kind"], "fact")
        self.assertEqual(self.raw()["version"], memory.MEMORY_VERSION)

    def test_old_fact_dicts_migrate_category_and_timestamp(self):
        (self.root / "facts.json").write_text(json.dumps({
            "facts": [{
                "fact": "work: I work at Petronas",
                "category": "work",
                "added": "2026-10-06T12:32:20",
            }]
        }), encoding="utf-8")

        record = memory.list_memories(include_all=True)[0]

        self.assertEqual(record["content"], "work: I work at Petronas")
        self.assertEqual(record["created_at"], "2026-10-06T12:32:20")
        self.assertEqual(record["scope"], memory.GLOBAL_SCOPE)
        self.assertIn("work", record["keywords"])
        self.assertIn("petronas", record["keywords"])

    def test_scope_filter_keeps_global_and_current_project_only(self):
        memory.add_memory("Global fact.")
        memory.add_memory("Project fact.", scope=self.project)
        memory.add_memory("Other project fact.", scope=self.other)

        visible = memory.list_memories()
        everything = memory.list_memories(include_all=True)

        self.assertEqual({m["content"] for m in visible},
                         {"Global fact.", "Project fact."})
        self.assertEqual(len(everything), 3)

        block = memory.get_facts_block()
        self.assertIn("Global fact.", block)
        self.assertIn("Project fact.", block)
        self.assertNotIn("Other project fact.", block)

    def test_scope_matches_nested_directories(self):
        nested = self.project / "src" / "ui"
        nested.mkdir(parents=True)
        memory.add_memory("Nested project fact.", scope=self.project)

        visible = memory.list_memories(cwd=str(nested))

        self.assertEqual([m["content"] for m in visible],
                         ["Nested project fact."])

    def test_file_scope_matches_its_parent_project(self):
        scope = self.project / "agent.py"
        memory.add_memory("Only for agent.py.", scope=scope)

        self.assertEqual(
            [m["content"] for m in memory.list_memories(cwd=str(self.project))],
            ["Only for agent.py."])

    def test_update_and_delete(self):
        record = memory.add_memory("Original content.", scope=self.project)

        updated = memory.update_memory(
            record["id"], title="New title", scope=memory.GLOBAL_SCOPE,
            keywords=["new"], content="Updated content.")

        self.assertTrue(updated)
        current = memory.get_memory(record["id"])
        self.assertEqual(current["title"], "New title")
        self.assertEqual(current["scope"], memory.GLOBAL_SCOPE)
        self.assertEqual(current["keywords"], ["new"])
        self.assertEqual(current["content"], "Updated content.")

        self.assertTrue(memory.delete_memory(record["id"]))
        self.assertIsNone(memory.get_memory(record["id"]))
        self.assertFalse(memory.delete_memory(record["id"]))

    def test_duplicates_are_ignored_within_a_scope(self):
        first = memory.add_memory("I drink coffee every morning.")
        second = memory.add_memory("I drink coffee every morning.")
        other_scope = memory.add_memory(
            "I drink coffee every morning.", scope=self.project)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertIsNotNone(other_scope)
        self.assertEqual(len(memory.list_memories(include_all=True)), 2)

    def test_forget_matches_partial_content(self):
        memory.add_memory("The clock out website is intersys.")
        memory.add_memory("My cat is named Mochi.")

        self.assertTrue(memory.forget("intersys"))
        contents = [m["content"] for m in memory.list_memories(include_all=True)]
        self.assertEqual(contents, ["My cat is named Mochi."])
        self.assertFalse(memory.forget("nothing like this"))

    def test_titles_and_keywords_are_derived(self):
        content = ("When opening the Stremio desktop app, wait three seconds "
                   "after pressing Enter before clicking the poster.")
        record = memory.add_memory(content)

        self.assertTrue(record["title"])
        self.assertLessEqual(len(record["title"]), 72)
        self.assertIn("stremio", record["keywords"])
        self.assertNotIn("the", record["keywords"])

    def test_lessons_are_hidden_from_the_facts_block(self):
        memory.add_memory("A learned lesson.", scope=self.project,
                          kind="lesson")

        self.assertEqual(memory.get_facts_block(), "")
        lessons = memory.list_memories(kind="lesson")
        self.assertEqual([m["content"] for m in lessons],
                         ["A learned lesson."])


if __name__ == "__main__":
    unittest.main(verbosity=2)
