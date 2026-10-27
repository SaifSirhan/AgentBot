"""Tests for execute_python, which runs real snippets in a child process.

Run: python -m unittest tests.test_execute_python -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent


class ExecutePythonTests(unittest.TestCase):
    def test_stdout_is_returned(self):
        self.assertEqual(agent.execute_python("print(2 + 2)"), "4")

    def test_unicode_survives_the_pipe(self):
        out = agent.execute_python('print("caf\u00e9 \u0639\u0631\u0628\u0649")')
        self.assertEqual(out, "caf\u00e9 \u0639\u0631\u0628\u0649")

    def test_project_modules_are_importable(self):
        out = agent.execute_python("import file_tools; print('ok')")
        self.assertEqual(out, "ok")

    def test_runs_from_the_project_directory(self):
        out = agent.execute_python("import os; print(os.getcwd())")
        self.assertEqual(Path(out), Path(agent.__file__).parent)

    def test_traceback_becomes_an_error(self):
        out = agent.execute_python("1 / 0")
        self.assertTrue(out.startswith("ERROR: exit code 1"))
        self.assertIn("ZeroDivisionError", out)

    def test_stderr_without_stdout_is_reported(self):
        out = agent.execute_python("import sys; print('careful', file=sys.stderr)")
        self.assertTrue(out.startswith("OK (no stdout)"))
        self.assertIn("careful", out)

    def test_no_output_is_reported(self):
        self.assertEqual(agent.execute_python("x = 1"), "OK (no output)")

    def test_empty_code_is_rejected(self):
        self.assertTrue(agent.execute_python("   ").startswith("ERROR: execute_python needs code"))

    def test_long_output_is_truncated(self):
        out = agent.execute_python("print('x' * 15000)")
        self.assertIn("truncated", out)
        self.assertLessEqual(len(out), agent.MAX_PYTHON_OUTPUT + 80)

    def test_timeout_is_reported(self):
        expired = agent.subprocess.TimeoutExpired("python", 60)
        with mock.patch.object(agent.subprocess, "run", side_effect=expired):
            out = agent.execute_python("print('slow')")
        self.assertIn("longer than 60 seconds", out)


if __name__ == "__main__":
    unittest.main()
