"""Tests for migrate_memory's copy/verify/delete ordering and safety checks.

Run: python -m unittest tests.test_migrate_memory -v
"""
import json
import tempfile
import unittest
from pathlib import Path

import migrate_memory


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="migrate_memory_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        self.source = self.root / "AgentMemory"
        (self.source / "skills" / "demo").mkdir(parents=True)
        (self.source / "facts.json").write_text('{"facts": []}', encoding="utf-8")
        (self.source / "conversation.json").write_text('{"history": []}', encoding="utf-8")
        (self.source / "skills" / "demo" / "SKILL.md").write_text("body", encoding="utf-8")

        self.rag_dir = self.root / "appdata" / "rag_db"
        self.rag_dir.mkdir(parents=True)
        (self.rag_dir / "chroma.sqlite3").write_text("db", encoding="utf-8")
        (self.rag_dir / "rag_fts.sqlite3").write_text("fts", encoding="utf-8")
        self.rag_state = self.root / "appdata" / "rag_state.json"
        self.rag_state.write_text('{"files": []}', encoding="utf-8")

        self.dest = self.root / "D_drive" / "AgentBrain"
        self.config_path = self.root / "config.json"

    def run_migration(self, **overrides):
        kwargs = dict(
            source=self.source,
            rag_dir=self.rag_dir,
            rag_state=self.rag_state,
            config_path=self.config_path,
        )
        kwargs.update(overrides)
        return migrate_memory.migrate(self.dest, **kwargs)

    def test_moves_memory_and_rag_then_removes_sources(self):
        report = self.run_migration()

        self.assertEqual(
            (self.dest / "facts.json").read_text(encoding="utf-8"),
            '{"facts": []}')
        self.assertTrue((self.dest / "skills" / "demo" / "SKILL.md").is_file())
        self.assertTrue((self.dest / "rag_db" / "chroma.sqlite3").is_file())
        self.assertTrue((self.dest / "rag_db" / "rag_fts.sqlite3").is_file())
        self.assertTrue((self.dest / "rag_state.json").is_file())
        self.assertFalse(self.source.exists())
        self.assertFalse(self.rag_dir.exists())
        self.assertFalse(self.rag_state.exists())
        self.assertTrue(any("copied 3 files" in line for line in report))

    def test_sets_the_config_key_and_keeps_other_keys(self):
        self.config_path.write_text(
            json.dumps({"GROQ_API_KEY": "keep-me"}), encoding="utf-8")

        self.run_migration()

        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["GROQ_API_KEY"], "keep-me")
        self.assertEqual(cfg["AGENT_MEMORY_DIR"], str(self.dest.resolve()))

    def test_no_set_config_leaves_config_untouched(self):
        self.run_migration(set_config=False)

        self.assertFalse(self.config_path.exists())

    def test_keep_source_copies_without_deleting(self):
        self.run_migration(keep_source=True)

        self.assertTrue((self.dest / "facts.json").is_file())
        self.assertTrue(self.source.exists())
        self.assertTrue(self.rag_dir.exists())
        self.assertTrue(self.rag_state.exists())

    def test_dry_run_changes_nothing(self):
        report = self.run_migration(dry_run=True)

        self.assertFalse(self.dest.exists())
        self.assertTrue(self.source.exists())
        self.assertTrue(any("dry run" in line for line in report))

    def test_refuses_non_empty_destination(self):
        self.dest.mkdir(parents=True)
        (self.dest / "already-here.txt").write_text("x", encoding="utf-8")

        with self.assertRaises(migrate_memory.MigrationError):
            self.run_migration()
        self.assertTrue(self.source.exists())

    def test_refuses_destination_inside_source(self):
        with self.assertRaises(migrate_memory.MigrationError):
            migrate_memory.migrate(self.source / "inner", source=self.source,
                                   rag_dir=self.rag_dir, rag_state=self.rag_state,
                                   config_path=self.config_path)

    def test_empty_destination_is_used(self):
        self.dest.mkdir(parents=True)

        self.run_migration()

        self.assertTrue((self.dest / "facts.json").is_file())

    def test_empty_index_dir_at_destination_is_replaced(self):
        (self.dest / "rag_db").mkdir(parents=True)

        self.run_migration()

        self.assertTrue((self.dest / "rag_db" / "chroma.sqlite3").is_file())


if __name__ == "__main__":
    unittest.main()
