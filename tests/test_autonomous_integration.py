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
        run.assert_called_once_with("save hello", on_step=None, stop_event=None)

    def test_task_prefix_forwards_the_stop_event(self):
        import threading
        event = threading.Event()
        with mock.patch.object(agent, "_run_autonomous_goal",
                               return_value=["ok"]) as run:
            agent.run_agent_turn("/task save hello", [], stop_event=event)
        run.assert_called_once_with("save hello", on_step=None, stop_event=event)

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

    def test_missing_tool_then_grant_refreshes_prompt_and_continues(self):
        replies = iter([
            '{"tool":"screenshot_background","input":""}',
            '{"tool":"request_tool","input":" screenshot_background "}',
            '{"tool":"screenshot_background","input":""}',
            "DONE",
            '{"achieved":true,"evidence":"captured screen"}',
        ])
        prompts = []

        def llm(prompt):
            prompts.append(prompt)
            return next(replies)

        with mock.patch.object(agent, "ask_llm_direct", side_effect=llm), \
                mock.patch.object(agent, "execute_tool", return_value="captured screen") as execute:
            step_log = agent._run_autonomous_goal("check my battery health")
        self.assertIn("not available", prompts[1])
        self.assertIn("request_tool", prompts[1])
        system = prompts[2].split("\n\nUSER:", 1)[0]
        catalog = json.loads(system.split("Allowed tools: ", 1)[1])
        self.assertIn("screenshot_background", catalog)
        execute.assert_called_once_with({"tool": "screenshot_background", "input": ""})
        self.assertIn("Goal independently verified", "\n".join(step_log))

    def test_done_after_grant_does_not_verify_or_finish_the_goal(self):
        replies = iter([
            '{"tool":"request_tool","input":"screenshot_background"}',
            "DONE",
            '{"tool":"screenshot_background","input":""}',
            "DONE",
        ])
        with mock.patch.object(agent, "ask_llm_direct", side_effect=lambda _: next(replies)), \
                mock.patch.object(agent, "execute_tool", return_value="captured screen") as execute, \
                mock.patch.object(agent, "_autonomous_judge", return_value=
                                  agent.autonomous_engine.Verification(True, "captured screen")) as judge:
            step_log = agent._run_autonomous_goal("check my battery health")
        execute.assert_called_once()
        judge.assert_called_once()
        self.assertIn("Goal independently verified", "\n".join(step_log))

    def test_grant_followed_by_failed_action_cannot_reach_verification(self):
        replies = iter([
            '{"tool":"request_tool","input":"check_battery_health"}',
            '{"tool":"check_battery_health","input":""}',
            "DONE",
        ])
        with mock.patch.object(agent, "MAX_STEPS_PER_TURN", 3), \
                mock.patch.object(agent, "ask_llm_direct", side_effect=lambda _: next(replies)), \
                mock.patch.object(agent, "execute_tool", return_value="ERROR: powercfg failed"), \
                mock.patch.object(agent, "_autonomous_judge", return_value=
                                  agent.autonomous_engine.Verification(True, "incorrect")) as judge:
            step_log = agent._run_autonomous_goal("check battery health")
        judge.assert_not_called()
        self.assertNotIn("Goal independently verified", "\n".join(step_log))

    def test_judge_does_not_count_grants_as_successful_work(self):
        history = (
            RecordedAction(1, "request_tool", True, "Tool enabled"),
            RecordedAction(2, "check_battery_health", False, "ERROR: powercfg failed"),
        )
        with mock.patch.object(agent, "ask_llm_direct", return_value=
                               '{"achieved":true,"evidence":"incorrect"}') as llm:
            verdict = agent._autonomous_judge("check battery health", history)
        self.assertFalse(verdict.achieved)
        llm.assert_not_called()

    def test_unrequestable_tool_reports_exact_error_without_json(self):
        with mock.patch.object(agent, "ask_llm_direct", return_value=
                               '{"tool":"request_tool","input":"no_such_tool"}') as llm, \
                mock.patch.object(agent, "execute_tool") as execute:
            step_log = agent._run_autonomous_goal("check battery health")
        self.assertEqual(llm.call_count, 1)
        execute.assert_not_called()
        self.assertIn("Cannot add unknown tool 'no_such_tool'.", step_log[-1])
        self.assertNotIn('{"tool":', step_log[-1])

    def test_llm_failure_is_reported_not_retried_forever(self):
        with mock.patch.object(agent, "ask_llm_direct",
                               return_value="Error: provider down"):
            step_log = agent._run_autonomous_goal("do the task")
        self.assertIn("LLM failed", "\n".join(step_log))


class AutonomousPermissionTests(unittest.TestCase):
    def run_loop(self, reply, *, tools=None, authorize=None, execute=None):
        self.execute = execute or mock.Mock()
        self.judge = mock.Mock()
        loop = agent.autonomous_engine.AutonomousLoop(
            llm=lambda _: reply,
            execute_tool=self.execute,
            verify_goal=self.judge,
            authorize=authorize or (lambda *_: True),
            tool_descriptions=tools or {"read_file": "Read a file"},
            max_steps=2,
        )
        return loop.run("read a file")

    def test_missing_tool_without_request_mechanism_reports_its_name(self):
        result = self.run_loop('{"tool":"no_such_tool","args":{}}')
        self.assertEqual(result.status, "error")
        self.assertIn("no_such_tool", result.message)
        self.assertIn("request_tool", result.message)
        self.execute.assert_not_called()

    def test_actual_authorization_denial_still_stops_without_retry(self):
        result = self.run_loop('{"tool":"read_file","args":{}}',
                               authorize=lambda *_: False)
        self.assertEqual(result.status, "denied")
        self.assertEqual(result.steps, 1)
        self.execute.assert_not_called()
        self.judge.assert_not_called()

    def test_dispatcher_denial_reports_exact_reason_without_retry(self):
        result = self.run_loop('{"tool":"read_file","args":{}}',
                               execute=mock.Mock(side_effect=PermissionError("Policy blocked private folder")))
        self.assertEqual(result.status, "denied")
        self.assertEqual(result.steps, 1)
        self.assertIn("Policy blocked private folder", result.message)
        self.execute.assert_called_once()

    def test_failed_request_reports_dispatcher_exception(self):
        result = self.run_loop('{"tool":"request_tool","args":{"input":"read_file"}}',
                               tools={"request_tool": "Request a tool"},
                               execute=mock.Mock(side_effect=RuntimeError("Tool registry unavailable")))
        self.assertEqual(result.status, "error")
        self.assertEqual(result.steps, 1)
        self.assertIn("Tool registry unavailable", result.message)


if __name__ == "__main__":
    unittest.main()
