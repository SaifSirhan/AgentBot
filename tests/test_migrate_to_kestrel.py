import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "migrate_to_kestrel.py"


class KestrelMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kestrel_migration_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.roaming = self.root / "roaming"
        self.local = self.root / "local"
        self.roaming.mkdir()
        self.local.mkdir()
        self.env = dict(os.environ, APPDATA=str(self.roaming),
                        LOCALAPPDATA=str(self.local))

    def run_migration(self):
        return subprocess.run([sys.executable, str(SCRIPT)], env=self.env,
                              cwd=self.root, capture_output=True, text=True,
                              timeout=15)

    def put(self, path, content, mtime=1_700_000_000):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        os.utime(path, (mtime, mtime))

    def test_missing_sources_are_a_successful_noop(self):
        result = self.run_migration()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0 files copied", result.stdout)
        self.assertFalse((self.roaming / "Kestrel").exists())
        self.assertFalse((self.local / "Kestrel").exists())

    def test_copies_both_trees_and_preserves_sources_and_timestamps(self):
        for base in (self.roaming, self.local):
            self.put(base / "AgentBot" / "nested" / "data.bin", b"\x00\xffdata")
            (base / "AgentBot" / "empty").mkdir()
        result = self.run_migration()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2 files copied", result.stdout)
        for base in (self.roaming, self.local):
            source = base / "AgentBot" / "nested" / "data.bin"
            target = base / "Kestrel" / "nested" / "data.bin"
            self.assertEqual(source.read_bytes(), b"\x00\xffdata")
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(target.stat().st_mtime_ns, source.stat().st_mtime_ns)
            self.assertTrue((base / "Kestrel" / "empty").is_dir())

    def test_merge_keeps_newer_equal_and_destination_only_files(self):
        source = self.roaming / "AgentBot"
        target = self.roaming / "Kestrel"
        for name, stamp in (("newer", 1_700_000_001), ("equal", 1_700_000_000),
                            ("older", 1_699_999_999)):
            self.put(source / name, b"source")
            self.put(target / name, b"destination", stamp)
        self.put(target / "only", b"keep")
        result = self.run_migration()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1 files copied", result.stdout)
        self.assertEqual((target / "older").read_bytes(), b"source")
        for name in ("newer", "equal"):
            self.assertEqual((target / name).read_bytes(), b"destination")
        self.assertEqual((target / "only").read_bytes(), b"keep")

    def test_second_run_copies_nothing(self):
        self.put(self.roaming / "AgentBot" / "config.json", b"{}")
        first = self.run_migration()
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_migration()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("0 files copied", second.stdout)

    def test_actual_legacy_memory_moves_under_kestrel_without_deletion(self):
        source = self.local / "AgentMemory" / "rag_db" / "chroma.sqlite3"
        self.put(source, b"database fixture")
        result = self.run_migration()
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.local / "Kestrel" / "AgentMemory" / "rag_db" / "chroma.sqlite3"
        self.assertEqual(target.read_bytes(), b"database fixture")
        self.assertTrue(source.exists())

    def test_conflicting_directory_is_reported_and_not_removed(self):
        self.put(self.roaming / "AgentBot" / "conflict", b"source")
        target = self.roaming / "Kestrel" / "conflict"
        target.mkdir(parents=True)
        result = self.run_migration()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ERROR", result.stdout)
        self.assertTrue(target.is_dir())
        self.assertEqual((self.roaming / "AgentBot" / "conflict").read_bytes(), b"source")

    def test_failed_copy_preserves_existing_file_and_cleans_temporary(self):
        import runpy
        from unittest import mock

        migration = runpy.run_path(str(SCRIPT))
        source = self.roaming / "AgentBot" / "data"
        target = self.roaming / "Kestrel" / "data"
        self.put(source, b"source", 1_700_000_001)
        self.put(target, b"destination")
        counts = {"copied": 0, "skipped": 0, "errors": 0}

        def fail_copy(src, dst):
            Path(dst).write_bytes(b"partial")
            raise OSError("copy interrupted")

        with mock.patch("shutil.copy2", side_effect=fail_copy):
            migration["merge_tree"](source, target, counts)
        self.assertEqual(target.read_bytes(), b"destination")
        self.assertEqual(source.read_bytes(), b"source")
        self.assertEqual(counts, {"copied": 0, "skipped": 0, "errors": 1})
        self.assertEqual(list(target.parent.iterdir()), [target])

    def test_linked_destination_parent_cannot_modify_outside_files(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.local / "Kestrel"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Symlink creation unavailable: {exc}")
        self.put(self.local / "AgentMemory" / "facts.json", b"source")
        result = self.run_migration()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ERROR", result.stdout)
        self.assertEqual(list(outside.iterdir()), [])

    def test_conflicting_chroma_snapshots_are_not_merged(self):
        source = self.local / "AgentMemory" / "rag_db"
        target = self.local / "Kestrel" / "AgentMemory" / "rag_db"
        self.put(source / "chroma.sqlite3", b"source metadata", 1_700_000_002)
        self.put(source / "segment" / "data.bin", b"source vectors")
        self.put(target / "chroma.sqlite3", b"target metadata")
        self.put(target / "segment" / "data.bin", b"target vectors", 1_700_000_003)
        result = self.run_migration()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("database snapshots", result.stdout)
        self.assertEqual((target / "chroma.sqlite3").read_bytes(), b"target metadata")
        self.assertEqual((target / "segment" / "data.bin").read_bytes(), b"target vectors")
        self.assertEqual((source / "chroma.sqlite3").read_bytes(), b"source metadata")

    def test_identical_chroma_snapshot_is_a_successful_noop(self):
        source = self.local / "AgentMemory" / "rag_db"
        self.put(source / "chroma.sqlite3", b"metadata")
        self.put(source / "segment" / "data.bin", b"vectors")
        first = self.run_migration()
        self.assertEqual(first.returncode, 0, first.stdout)
        second = self.run_migration()
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertIn("0 files copied", second.stdout)

    def test_failed_database_copy_does_not_publish_partial_snapshot(self):
        import runpy
        from unittest import mock

        migration = runpy.run_path(str(SCRIPT))
        source = self.local / "AgentMemory" / "rag_db"
        target = self.local / "Kestrel" / "AgentMemory" / "rag_db"
        self.put(source / "chroma.sqlite3", b"metadata")
        self.put(source / "segment" / "data.bin", b"vectors")
        counts = {"copied": 0, "skipped": 0, "errors": 0}

        def fail_copytree(src, dst):
            Path(dst).mkdir()
            (Path(dst) / "chroma.sqlite3").write_bytes(b"partial")
            raise OSError("database copy interrupted")

        with mock.patch("shutil.copytree", side_effect=fail_copytree):
            migration["merge_tree"](source, target, counts)
        self.assertFalse(target.exists())
        self.assertEqual(counts, {"copied": 0, "skipped": 0, "errors": 1})
        self.assertEqual(list(target.parent.iterdir()), [])

    def test_linked_environment_ancestor_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.root / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Symlink creation unavailable: {exc}")
        self.put(outside / "nested" / "AgentBot" / "data", b"source")
        self.env["LOCALAPPDATA"] = str(link / "nested")
        result = self.run_migration()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ERROR", result.stdout)
        self.assertFalse((outside / "nested" / "Kestrel").exists())

    def test_unset_environment_does_not_use_working_directory(self):
        self.env.pop("APPDATA", None)
        self.env.pop("LOCALAPPDATA", None)
        self.put(self.root / "AgentBot" / "data", b"do not copy")
        result = self.run_migration()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "Kestrel").exists())
        self.assertIn("0 files copied", result.stdout)


if __name__ == "__main__":
    unittest.main()
