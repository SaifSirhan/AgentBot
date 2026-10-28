"""Execution-only AutonomousLoop integration tests.

Run: python -m unittest tests.test_autonomous_integration -v
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent
from autonomous_loop import RecordedAction


class AutonomousIntegrationTests(unittest.TestCase):
    def setUp(self):
        agent._clear_turn_route()
        self.addCleanup(agent._clear_turn_route)
        p = mock.patch.object(agent, "GROUP_MODE", False)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch("rag_tool.search_documents", return_value="")
        p.start()
        self.addCleanup(p.stop)

    def test_task_prefix_routes_to_the_autonomous_engine(self):
        with mock.patch.object(agent, "_run_autonomous_goal",
                               return_value=["ok"]) as run:
            self.assertEqual(agent.run_agent_turn("/task save hello", []), ["ok"])
        run.assert_called_once_with("save hello", on_step=None)

    def test_empty_task_goal_is_rejected(self):
        result = agent.run_agent_turn("/task", [])
        self.assertEqual(result, ["System: /task needs a goal."])

    def test_llm_adapter_coerces_input_to_args(self):
        with mock.patch.object(agent, "ask_llm_direct",
                               return_value='{"tool": "type_text", "input": "hi"}'):
            reply = agent._autonomous_llm([{"role": "user", "content": "next"}])
        self.assertEqual(json.loads(reply),
                         {"tool": "type_text", "args": {"input": "hi"}})

    def test_judge_fails_closed_without_history_or_json(self):
        self.assertFalse(agent._autonomous_judge("goal", ()).achieved)
        history = (RecordedAction(1, "type_text", True, "typed hello"),)
        with mock.patch.object(agent, "ask_llm_direct", return_value="not json"):
            self.assertFalse(agent._autonomous_judge("goal", history).achieved)
        with mock.patch.object(
                agent, "ask_llm_direct",
                return_value='{"achieved": true, "evidence": "typed hello"}'):
            verdict = agent._autonomous_judge("goal", history)
        self.assertTrue(verdict.achieved)
        self.assertEqual(verdict.evidence, "typed hello")

    def test_request_tool_is_granted_then_done_is_verified(self):
        replies = iter([
            '{"tool": "request_tool", "input": "type_text"}',
            '{"tool": "type_text", "args": {"input": "hello"}}',
            "DONE",
            '{"achieved": true, "evidence": "typed hello"}',
        ])
        calls = []

        def fake_execute(action):
            calls.append((action["tool"], action["input"]))
            return "typed hello"

        with mock.patch.object(agent, "ask_llm_direct",
                               side_effect=lambda _prompt: next(replies)), \
                mock.patch.object(agent, "execute_tool", side_effect=fake_execute):
            step_log = agent._run_autonomous_goal("watch a movie")

        self.assertIn(("type_text", "hello"), calls)
        joined = "\n".join(step_log)
        self.assertIn("Goal independently verified", joined)
        self.assertIn("typed hello", joined)

    def test_group_mode_blocks_an_unsafe_requested_tool(self):
        replies = iter([
            '{"tool": "request_tool", "args": {"input": "read_file"}}',
            "DONE",
        ])
        with mock.patch.object(agent, "GROUP_MODE", True), \
                mock.patch.object(agent, "MAX_STEPS_PER_TURN", 2), \
                mock.patch.object(agent, "ask_llm_direct",
                                  side_effect=lambda _prompt: next(replies)):
            step_log = agent._run_autonomous_goal("read a file")
        joined = "\n".join(step_log)
        self.assertIn("blocked in group chats", joined)
        self.assertNotIn("read_file(autonomous)", joined)

    def test_llm_failure_is_reported_not_retried_forever(self):
        with mock.patch.object(agent, "ask_llm_direct",
                               return_value="Error: provider down"):
            step_log = agent._run_autonomous_goal("do the task")
        self.assertIn("LLM failed", "\n".join(step_log))


if __name__ == "__main__":
    unittest.main()
