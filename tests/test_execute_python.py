"""Tests for execute_python, which runs real snippets in a child process.

Run: python -m unittest tests.test_execute_python -v
"""
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent
import python_sandbox
import verification_adapter


class ExecutePythonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox_temp = tempfile.TemporaryDirectory()
        roots = Path(cls.sandbox_temp.name) / "roots"
        roots.mkdir()
        cls.sandbox_env = mock.patch.dict(os.environ, {
            "LOCALAPPDATA": str(Path(cls.sandbox_temp.name) / "local"),
            "PYTHON_SANDBOX_ROOTS": str(roots),
        })
        cls.sandbox_env.start()
        _, _, sandbox_state = python_sandbox._profile_identity()
        state_digest = hashlib.sha256(sandbox_state.encode("utf-8")).hexdigest()[:12]
        profile_name = f"Kestrel.PythonSandbox.Tests.ExecutePython.{state_digest}"
        cls.identity_patch = mock.patch.object(
            python_sandbox, "_profile_identity",
            return_value=(profile_name, "S-1-test", sandbox_state))
        cls.identity_patch.start()
        import config
        cls.config_patch = mock.patch.object(
            config, "CONFIG_PATH", str(Path(cls.sandbox_temp.name) / "config.json"))
        cls.memory_patch = mock.patch.object(
            config, "memory_dir", return_value=str(Path(cls.sandbox_temp.name) / "memory"))
        cls.config_patch.start()
        cls.memory_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.memory_patch.stop()
        cls.config_patch.stop()
        cls.identity_patch.stop()
        cls.sandbox_env.stop()
        cls.sandbox_temp.cleanup()

    def setUp(self):
        agent.reset_python_failures()

    def test_stdout_is_returned(self):
        self.assertEqual(agent.execute_python("print(2 + 2)"), "4")

    def test_execution_is_routed_through_the_sandbox(self):
        with tempfile.TemporaryDirectory() as directory:
            result = python_sandbox.SandboxResult(returncode=0, stdout="sandboxed")
            with mock.patch.object(
                    agent._cfg, "load_config",
                    return_value={"PYTHON_SANDBOX_ROOTS": directory}), \
                    mock.patch.object(python_sandbox, "run_python",
                                      return_value=result) as run:
                output = agent.execute_python("print('host')")
            self.assertEqual(output, "sandboxed")
            self.assertEqual(run.call_args.kwargs["writable_roots"], [directory])
            self.assertEqual(
                Path(run.call_args.kwargs["cwd"]), Path(agent.__file__).parent)

    def test_sandbox_failure_never_falls_back_to_host_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "must-not-exist.txt"
            code = f"from pathlib import Path; Path({str(target)!r}).write_text('bad')"
            with mock.patch.object(
                    python_sandbox, "run_python",
                    return_value=python_sandbox.SandboxResult(error="setup denied")):
                output = agent.execute_python(code)
            self.assertTrue(output.startswith("ERROR:"))
            self.assertIn("setup denied", output)
            self.assertFalse(target.exists())

    def test_python_receipts_are_serialized_with_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "shared.txt"
            target.write_text("before", encoding="utf-8")
            code_a = (
                "from pathlib import Path\n"
                f"Path({str(target)!r}).write_text('A', encoding='utf-8')")
            code_b = (
                "from pathlib import Path\n"
                f"Path({str(target)!r}).write_text('B', encoding='utf-8')")
            receipt_started = threading.Event()
            allow_receipt = threading.Event()
            second_snapshot = threading.Event()
            outputs = {}
            errors = []
            original_snapshot = verification_adapter.snapshot_python_file
            original_append = verification_adapter.append_python_receipts
            key = os.path.normcase(str(target))

            def fake_run(code, **kwargs):
                before = {key: original_snapshot(str(target))}
                if code == code_b:
                    second_snapshot.set()
                value = "A" if code == code_a else "B"
                target.write_text(value, encoding="utf-8")
                return python_sandbox.SandboxResult(
                    returncode=0, file_snapshots=before)

            def append(output, before):
                if threading.current_thread().name == "python-a":
                    receipt_started.set()
                    if not allow_receipt.wait(2):
                        raise AssertionError("receipt release was not signalled")
                return original_append(output, before)

            def execute(name, code):
                try:
                    outputs[name] = agent._execute_python_once(code)
                except Exception as exc:
                    errors.append(exc)

            with mock.patch.object(python_sandbox, "run_python", side_effect=fake_run), \
                    mock.patch.object(verification_adapter, "append_python_receipts",
                                      side_effect=append):
                first = threading.Thread(
                    target=execute, args=("python-a", code_a), name="python-a")
                second = threading.Thread(
                    target=execute, args=("python-b", code_b), name="python-b")
                first.start()
                self.assertTrue(receipt_started.wait(2))
                second.start()
                snapshot_during_receipt = second_snapshot.wait(0.5)
                allow_receipt.set()
                first.join(timeout=5)
                second.join(timeout=5)

            self.assertFalse(errors)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertFalse(snapshot_during_receipt)
            marker = verification_adapter._PYTHON_RECEIPT_MARKER
            envelope = json.loads(outputs["python-a"].split(marker, 1)[1])
            after = envelope["payload"]["receipts"][0]["after"]
            self.assertEqual(after["sha256"], hashlib.sha256(b"A").hexdigest())

    def test_failed_runs_keep_receipts_without_supporting_success_claims(self):
        results = (
            python_sandbox.SandboxResult(returncode=1, stderr="failed"),
            python_sandbox.SandboxResult(timed_out=True),
            python_sandbox.SandboxResult(cancelled=True),
            python_sandbox.SandboxResult(error="cleanup failed"),
        )
        for sandbox_result in results:
            with self.subTest(result=sandbox_result), \
                    tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "partial.txt"
                target.write_text("before", encoding="utf-8")
                code = (
                    "from pathlib import Path\n"
                    f"Path({str(target)!r}).write_text('partial', encoding='utf-8')")

                def fake_run(code, **kwargs):
                    key = os.path.normcase(str(target))
                    sandbox_result.file_snapshots = {
                        key: verification_adapter.snapshot_python_file(str(target)),
                    }
                    target.write_text("partial", encoding="utf-8")
                    return sandbox_result

                with mock.patch.object(python_sandbox, "run_python", side_effect=fake_run):
                    output = agent._execute_python_once(code)
                self.assertTrue(output.startswith("ERROR:"))
                self.assertIn(verification_adapter._PYTHON_RECEIPT_MARKER, output)
                self.assertFalse(verification_adapter.reply_is_supported(
                    verification_adapter.build_engine([("execute_python", code, output)]),
                    "I edited the file."))

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
        with mock.patch.object(agent, "PY_TIMEOUT", 0.3):
            out = agent.execute_python("import time; time.sleep(5)")
        self.assertIn("longer than", out)
        self.assertIn("was stopped", out)

    def test_third_failure_blocks_further_execution(self):
        for _ in range(agent.PY_FAILURE_LIMIT):
            self.assertTrue(agent.execute_python("1 / 0").startswith("ERROR:"))
        self.assertEqual(agent.python_failure_count(), agent.PY_FAILURE_LIMIT)

        with mock.patch.object(python_sandbox, "run_python") as run:
            out = agent.execute_python("print(2 + 2)")

        run.assert_not_called()
        self.assertEqual(out, agent.PY_DISABLED_MESSAGE)

    def test_success_does_not_count_as_a_failure(self):
        self.assertEqual(agent.execute_python("print(2 + 2)"), "4")
        self.assertEqual(agent.python_failure_count(), 0)

    def test_reset_reenables_execution(self):
        for _ in range(agent.PY_FAILURE_LIMIT):
            agent.execute_python("1 / 0")
        self.assertEqual(agent.execute_python("print(1)"), agent.PY_DISABLED_MESSAGE)

        agent.reset_python_failures()

        self.assertEqual(agent.execute_python("print(1)"), "1")

    def test_new_turn_resets_the_failure_counter(self):
        for _ in range(agent.PY_FAILURE_LIMIT):
            agent.execute_python("1 / 0")
        self.assertEqual(agent.python_failure_count(), agent.PY_FAILURE_LIMIT)

        def fake_planner(next_input, context):
            return {"tool": "chat", "input": "ok"}, None

        with mock.patch.object(agent, "get_valid_action", side_effect=fake_planner), \
                mock.patch.object(agent, "execute_tool", return_value="ok"), \
                mock.patch("rag_tool.search_documents", return_value=""):
            agent._run_agent_turn_impl("hello", [])

        self.assertEqual(agent.python_failure_count(), 0)


if __name__ == "__main__":
    unittest.main()
