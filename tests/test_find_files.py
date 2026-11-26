"""find_files pattern matching.

Run: python -m unittest tests.test_find_files -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent


class FindFilesPatternTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="find_files_")
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        (self.folder / "Log Intern.docx").write_text("log", encoding="utf-8")
        (self.folder / "notes.txt").write_text("notes", encoding="utf-8")
        nested = self.folder / "logs"
        nested.mkdir()
        (nested / "debug.log").write_text("debug", encoding="utf-8")

    def test_glob_wildcards_match_the_way_the_model_writes_them(self):
        output = agent.find_files(f"{self.folder}|*log*.docx")
        self.assertIn("Log Intern.docx", output)

    def test_plain_substrings_still_match(self):
        output = agent.find_files(f"{self.folder}|intern")
        self.assertIn("Log Intern.docx", output)

    def test_glob_extension_matches_nested_files(self):
        output = agent.find_files(f"{self.folder}|*.log")
        self.assertIn("debug.log", output)


if __name__ == "__main__":
    unittest.main()
