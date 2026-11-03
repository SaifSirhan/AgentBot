"""Turn-loop tests for verification-engine enforcement.

Run: python -m unittest tests.test_verification_turn -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent


def final_message(step_log):
    for line in reversed(step_log or []):
        if line.startswith("Action: chat(") and "-> Result: " in line:
            text = line.split("-> Result: ", 1)[-1]
            return text[4:] if text.startswith("AI: ") else text
        if line.startswith("Done:"):
            return line[5:].strip()
    return None


class VerificationTurnTests(unittest.TestCase):
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
