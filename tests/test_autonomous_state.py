import json
import sys
import unittest
from pathlib import Path
from threading import Event
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autonomous_loop import AutonomousLoop, RecordedAction, ToolResult, Verification


class AutonomousStateTests(unittest.TestCase):
    def loop(self, replies, execute=None, verify=None, **kwargs):
        self.prompts = []
        replies = iter(replies)

        def llm(messages):
            self.prompts.append(messages)
            return next(replies)

        self.execute = mock.Mock(side_effect=execute or (
            lambda tool, args, stop: ToolResult(True, "Universiti Malaya source")
        ))
        self.verify = mock.Mock(side_effect=verify or (
            lambda goal, history: Verification(True, "All requirements supported")
        ))
        return AutonomousLoop(
            llm, self.execute, self.verify, lambda tool, args: True,
            {"search_web": "Search sources", "execute_python": "Write a document",
             "request_tool": "Request a capability"},
            **kwargs,
        )

    def test_scratchpad_retrieves_results_beyond_history_excerpt(self):
        source = "x" * 1500 + "Universiti Malaya event on 10 November"
        loop = self.loop([
            '{"tool":"search_web","args":{"query":"UM"}}',
            '{"tool":"scratchpad_read","args":{"id":"obs-1","offset":1200}}',
            "DONE",
        ], execute=lambda *args: ToolResult(True, source), max_steps=3)
        result = loop.run("Research Universiti Malaya")
        self.assertEqual(result.status, "success", result.message)
        self.assertEqual(self.execute.call_count, 1)
        self.assertIn("Universiti Malaya event on 10 November",
                      json.dumps(self.prompts[2]))
        self.assertEqual(result.history[0].observation, source)

    def test_notes_are_injected_but_do_not_count_as_work(self):
        loop = self.loop([
            '{"tool":"scratchpad_write","args":{"key":"plan","text":"Use official sources"}}',
            "DONE", "DONE",
        ], max_steps=3)
        result = loop.run("Research Universiti Malaya")
        self.assertEqual(result.status, "step_limit", result.message)
        self.verify.assert_not_called()
        self.execute.assert_not_called()
        self.assertIn("Use official sources", json.dumps(self.prompts[1]))
        self.assertIn("unverified", json.dumps(self.prompts[1]).lower())

    def test_no_work_done_never_calls_verifier(self):
        loop = self.loop(["DONE", "DONE"], max_steps=2)
        result = loop.run("Write a report")
        self.assertEqual(result.status, "step_limit")
        self.verify.assert_not_called()

    def test_failed_verification_allows_only_one_repair_action(self):
        loop = self.loop([
            '{"tool":"search_web","args":{}}', "DONE",
            '{"tool":"execute_python","args":{"input":"repair"}}',
            '{"tool":"execute_python","args":{"input":"must not run"}}',
        ], verify=lambda *args: Verification(False, "Wrong university"), max_steps=5)
        result = loop.run("Research Malaya and write a document")
        self.assertEqual(result.status, "incomplete", result.message)
        self.assertEqual(result.steps, 3)
        self.assertEqual(self.execute.call_count, 2)
        self.assertEqual(self.verify.call_count, 2)
        self.assertIn("Wrong university", json.dumps(self.prompts[2]))

    def test_repair_grant_or_failure_cannot_reverify_old_success(self):
        for reply in (
            '{"tool":"request_tool","args":{"input":"execute_python"}}',
            '{"tool":"scratchpad_write","args":{"key":"claim","text":"done"}}',
            '{"tool":"execute_python","args":{"input":"fail"}}',
            "DONE",
        ):
            with self.subTest(reply=reply):
                loop = self.loop([
                    '{"tool":"search_web","args":{}}', "DONE", reply, "DONE",
                ], execute=lambda tool, args, stop: ToolResult(
                    tool != "execute_python", "source or failure"),
                    verify=lambda *args: Verification(False, "Missing document"),
                    max_steps=5)
                result = loop.run("Research and write")
                self.assertEqual(result.status, "incomplete", result.message)
                self.assertEqual(self.verify.call_count, 1)

    def test_repair_is_verified_without_an_extra_done(self):
        verdicts = iter([
            Verification(False, "Missing document"),
            Verification(True, "Document inspected"),
        ])
        loop = self.loop([
            '{"tool":"search_web","args":{}}', "DONE",
            '{"tool":"execute_python","args":{"input":"write"}}',
        ], verify=lambda *args: next(verdicts), max_steps=5)
        result = loop.run("Research and write")
        self.assertEqual(result.status, "success", result.message)
        self.assertEqual(result.steps, 3)
        self.assertEqual(self.verify.call_count, 2)

    def test_step_budget_checks_goal_and_reserves_one_repair(self):
        verdicts = iter([
            Verification(False, "Missing document"),
            Verification(True, "Document inspected"),
        ])
        loop = self.loop([
            '{"tool":"search_web","args":{}}',
            '{"tool":"execute_python","args":{"input":"write"}}',
        ], verify=lambda *args: next(verdicts), max_steps=1)
        result = loop.run("Research and write")
        self.assertEqual(result.status, "success", result.message)
        self.assertEqual(result.steps, 2)
        self.assertEqual(self.execute.call_count, 2)

    def test_cancellation_during_failed_verification_prevents_repair(self):
        stop = Event()

        def verify(*args):
            stop.set()
            return Verification(False, "Missing document")

        loop = self.loop([
            '{"tool":"search_web","args":{}}', "DONE",
            '{"tool":"execute_python","args":{}}',
        ], verify=verify, max_steps=3, stop_event=stop)
        result = loop.run("Research and write")
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(self.execute.call_count, 1)

    def test_clarification_is_not_returned_as_completion(self):
        loop = self.loop([
            "What exactly should go in the docx?",
            '{"tool":"search_web","args":{}}', "DONE",
        ], max_steps=3)
        result = loop.run("Research student parties at Universiti Malaya")
        self.assertEqual(result.status, "success", result.message)
        self.assertNotIn("What exactly", result.message)
        self.assertIn("NEVER ask", self.prompts[0][0]["content"])

    def test_resumed_task_seeds_actual_results_into_scratchpad(self):
        prior = RecordedAction(4, "search_web", True, "Universiti Malaya source URL")
        loop = self.loop([
            '{"tool":"execute_python","args":{"input":"write report"}}',
            "DONE",
        ], max_steps=2)
        result = loop.run("Research and write", initial_history=(prior,))
        self.assertEqual(result.status, "success", result.message)
        self.assertIn("Universiti Malaya source URL", json.dumps(self.prompts[0]))
        self.assertEqual([record.step for record in result.history], [1, 2])
        self.assertEqual(self.execute.call_count, 1)

    def test_resumed_delegation_still_requires_terminal_inspection(self):
        prior = RecordedAction(1, "delegate_to_qoder", True, "Prompt sent")
        loop = self.loop(["DONE"], max_steps=1)
        result = loop.run("Build a server", initial_history=(prior,))
        self.assertEqual(result.status, "step_limit")
        self.verify.assert_not_called()

    def test_grant_cannot_remove_initial_catalog_entries(self):
        def execute(tool, args, stop):
            if tool == "request_tool":
                loop.tools = {"new_tool": "Newly granted tool"}
            return ToolResult(True, "ok")

        loop = self.loop([
            '{"tool":"request_tool","args":{"input":"new_tool"}}',
            '{"tool":"search_web","args":{}}', "DONE",
        ], execute=execute, max_steps=3)
        result = loop.run("Search and write")
        self.assertEqual(result.status, "success", result.message)
        catalog = json.loads(self.prompts[1][0]["content"].split("Allowed tools: ")[1])
        self.assertIn("search_web", catalog)
        self.assertIn("execute_python", catalog)
        self.assertIn("new_tool", catalog)

    def test_new_run_does_not_inherit_granted_tools(self):
        def execute(tool, args, stop):
            loop.tools["private_tool"] = "Only selected for the first run"
            return ToolResult(True, "granted")

        loop = self.loop([
            '{"tool":"request_tool","args":{"input":"private_tool"}}',
            "DONE", "DONE",
        ], execute=execute, max_steps=1)
        loop.run("First task")
        second_run_start = len(self.prompts)
        loop.run("Unrelated second task")
        self.assertNotIn("private_tool", self.prompts[second_run_start][0]["content"])

    def test_scratchpad_write_cannot_overwrite_observations(self):
        loop = self.loop([
            '{"tool":"search_web","args":{}}',
            '{"tool":"scratchpad_write","args":{"key":"obs-1","text":"FAKE VERIFIED"}}',
            '{"tool":"scratchpad_read","args":{"id":"obs-1"}}', "DONE",
        ], max_steps=4)
        result = loop.run("Research")
        self.assertEqual(result.status, "success", result.message)
        read = json.loads(result.history[2].observation)
        self.assertEqual(read["text"], "Universiti Malaya source")

    def test_scratchpad_permission_denial_is_terminal(self):
        loop = self.loop([
            '{"tool":"scratchpad_write","args":{"key":"note","text":"value"}}',
        ], max_steps=2)
        loop.authorize = lambda *args: False
        result = loop.run("Research")
        self.assertEqual(result.status, "denied")
        self.assertEqual(loop.scratchpad.view()["unverified_notes"], {})
        self.verify.assert_not_called()

    def test_grant_at_budget_end_never_triggers_verification(self):
        loop = self.loop([
            '{"tool":"search_web","args":{}}',
            '{"tool":"request_tool","args":{"input":"execute_python"}}',
            "DONE",  # Grant is free; a completion proposal still costs a turn.
        ], max_steps=2)
        result = loop.run("Search and write")
        self.assertEqual(result.status, "step_limit")
        self.verify.assert_not_called()

    def test_new_run_has_no_previous_scratchpad(self):
        loop = self.loop([
            '{"tool":"search_web","args":{}}', "DONE",
            '{"tool":"search_web","args":{}}', "DONE",
        ], max_steps=2)
        loop.run("First task")
        loop.run("Second unrelated task")
        self.assertNotIn("Universiti Malaya source", json.dumps(self.prompts[2]))


if __name__ == "__main__":
    unittest.main()
