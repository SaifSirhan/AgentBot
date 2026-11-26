"""Turn-loop tests for verification-engine enforcement.

Run: python -m unittest tests.test_verification_turn -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent
import python_sandbox


def final_message(step_log):
    for line in reversed(step_log or []):
        if line.startswith("Action: chat(") and "-> Result: " in line:
            text = line.split("-> Result: ", 1)[-1]
            return text[4:] if text.startswith("AI: ") else text
        if line.startswith("Done:"):
            return line[5:].strip()
    return None


class VerificationTurnTests(unittest.TestCase):
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
        cls.identity_patch = mock.patch.object(
            python_sandbox, "_profile_identity",
            return_value=("Kestrel.PythonSandbox.Tests.VerificationTurn",
                          "S-1-test", sandbox_state))
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
        patches = [
            mock.patch("rag_tool.search_documents", return_value=""),
            mock.patch.object(agent, "_generate_skill_from_turn", return_value=""),
            mock.patch.object(agent, "ask_llm_direct",
                              return_value="I couldn't verify that."),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_turn(self, steps, execute=None):
        sequence = iter(steps)
        calls = []

        def fake_planner(_input, _context):
            action, error = next(sequence)
            calls.append(action)
            return action, error

        execute = execute or (lambda action: f"AI: {action.get('input', '')}")
        with mock.patch.object(agent, "get_valid_action", side_effect=fake_planner), \
                mock.patch.object(agent, "execute_tool", side_effect=execute):
            step_log = agent._run_agent_turn_impl("do the thing", [])
        return step_log, calls

    def test_chat_without_tools_is_not_sent_to_the_verifier(self):
        """A turn that ran no tool is conversation, not a task claim."""
        step_log, calls = self.run_turn([
            ({"tool": "chat", "input": "I translated the logs."}, None),
        ])
        self.assertEqual(len(calls), 1)
        self.assertNotIn(
            "System: " + agent.verification_adapter.RETRY_MESSAGE, step_log)
        self.assertEqual(final_message(step_log), "I translated the logs.")

    def test_verified_claim_is_sent_without_retry(self):
        def execute(action):
            if action["tool"] == "write_file":
                return "Wrote 12 bytes"
            return f"AI: {action['input']}"

        step_log, calls = self.run_turn([
            ({"tool": "write_file", "input": "notes.txt|hello"}, None),
            ({"tool": "chat", "input": "Done — saved notes.txt."}, None),
        ], execute=execute)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("System: " + agent.verification_adapter.RETRY_MESSAGE, step_log)
        self.assertEqual(final_message(step_log), "Done — saved notes.txt.")

    def test_python_file_edit_claim_is_accepted_with_receipt(self):
        with tempfile.TemporaryDirectory(prefix="python_turn_") as directory, \
                mock.patch.dict(os.environ, {"PYTHON_SANDBOX_ROOTS": directory}):
            path = Path(directory) / "report.txt"
            path.write_text("before", encoding="utf-8")
            code = (
                "from pathlib import Path\n"
                f"Path({str(path)!r}).write_text('after', encoding='utf-8')"
            )
            output = agent.execute_python(code)

            def execute(action):
                if action["tool"] == "execute_python":
                    return output
                return f"AI: {action['input']}"

            step_log, calls = self.run_turn([
                ({"tool": "execute_python", "input": code}, None),
                ({"tool": "chat", "input": "I edited the file."}, None),
            ], execute=execute)

            self.assertEqual(len(calls), 2)
            self.assertNotIn(
                "System: " + agent.verification_adapter.RETRY_MESSAGE, step_log)
            self.assertEqual(final_message(step_log), "I edited the file.")
            self.assertEqual(path.read_text(encoding="utf-8"), "after")

    def test_python_docx_edit_claim_is_accepted_with_receipt(self):
        from docx import Document

        with tempfile.TemporaryDirectory(prefix="python_docx_turn_") as directory, \
                mock.patch.dict(os.environ, {"PYTHON_SANDBOX_ROOTS": directory}):
            path = Path(directory) / "report.docx"
            document = Document()
            document.add_paragraph("before")
            document.save(path)
            code = (
                "from docx import Document\n"
                f"document = Document({str(path)!r})\n"
                "for paragraph in document.paragraphs:\n"
                "    if paragraph.text == 'before':\n"
                "        paragraph.text = 'after'\n"
                f"document.save({str(path)!r})"
            )
            output = agent.execute_python(code)

            def execute(action):
                if action["tool"] == "execute_python":
                    return output
                return f"AI: {action['input']}"

            step_log, calls = self.run_turn([
                ({"tool": "execute_python", "input": code}, None),
                ({"tool": "chat", "input": "I edited the document."}, None),
            ], execute=execute)

            self.assertEqual(len(calls), 2)
            self.assertNotIn(
                "System: " + agent.verification_adapter.RETRY_MESSAGE, step_log)
            self.assertEqual(final_message(step_log), "I edited the document.")
            self.assertEqual(Document(path).paragraphs[0].text, "after")

    def test_second_rejection_after_a_failed_tool_is_blocked(self):
        def execute(action):
            if action["tool"] == "write_file":
                return "ERROR: access denied"
            return f"AI: {action['input']}"

        step_log, calls = self.run_turn([
            ({"tool": "write_file", "input": "notes.txt|hello"}, None),
            ({"tool": "chat", "input": "I saved the file."}, None),
            ({"tool": "chat", "input": "I saved the document."}, None),
            ({"tool": "chat", "input": "never reached"}, None),
        ], execute=execute)
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            final_message(step_log), agent.verification_adapter.ERROR_REPLY)
        self.assertNotIn("I saved the file.", "\n".join(step_log))

    def test_failed_tool_cannot_support_the_claim(self):
        def execute(action):
            if action["tool"] == "write_file":
                return "ERROR: access denied"
            return f"AI: {action['input']}"

        step_log, _ = self.run_turn([
            ({"tool": "write_file", "input": "notes.txt|hello"}, None),
            ({"tool": "chat", "input": "Done — saved notes.txt."}, None),
            ({"tool": "chat", "input": "The save failed."}, None),
        ], execute=execute)
        self.assertIn("System: " + agent.verification_adapter.RETRY_MESSAGE, step_log)
        self.assertEqual(final_message(step_log), "The save failed.")

    def test_adapter_fault_fails_closed_after_a_tool_ran(self):
        def execute(action):
            if action["tool"] == "write_file":
                return "Wrote 12 bytes"
            return f"AI: {action['input']}"

        with mock.patch.object(agent.verification_adapter, "build_engine",
                               side_effect=RuntimeError("boom")), \
                mock.patch.object(agent.traceback, "print_exc"):
            step_log, calls = self.run_turn([
                ({"tool": "write_file", "input": "notes.txt|hello"}, None),
                ({"tool": "chat", "input": "I saved the file."}, None),
                ({"tool": "chat", "input": "I couldn't verify the save."}, None),
            ], execute=execute)
        self.assertEqual(len(calls), 3)
        self.assertNotIn("I saved the file.", "\n".join(step_log))
        self.assertEqual(
            final_message(step_log), agent.verification_adapter.ERROR_REPLY)

    def test_done_tool_claim_is_also_verified(self):
        step_log, calls = self.run_turn([
            ({"tool": "write_file", "input": "a.txt|hi"}, None),
            ({"tool": "done", "input": "Done — wrote a.txt."}, None),
        ])
        self.assertEqual(len(calls), 2)
        self.assertEqual(final_message(step_log), "Done — wrote a.txt.")

        step_log, calls = self.run_turn([
            ({"tool": "write_file", "input": "a.txt|hi"}, None),
            ({"tool": "done", "input": "Done — translated the file."}, None),
            ({"tool": "chat", "input": "I only wrote the file."}, None),
        ])
        self.assertIn("System: " + agent.verification_adapter.RETRY_MESSAGE, step_log)
        self.assertEqual(final_message(step_log), "I only wrote the file.")


if __name__ == "__main__":
    unittest.main()
