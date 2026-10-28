"""Tests for dynamic tool requests when the router allowlist is too small.

Run: python -m unittest tests.test_request_tool -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent


class RequestToolTests(unittest.TestCase):
    def setUp(self):
        agent._clear_turn_route()
        self.addCleanup(agent._clear_turn_route)
        patcher = mock.patch.object(agent, "GROUP_MODE", False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("_generate_skill_from_turn",):
            p = mock.patch.object(agent, name, return_value="")
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("rag_tool.search_documents", return_value="")
        p.start()
        self.addCleanup(p.stop)

    def run_turn(self, steps, execute=None):
        sequence = iter(steps)
        observed = {}

        def planner(_input, _context):
            action, error = next(sequence)
            return action, error

        def default_execute(action):
            observed["allowed"] = set(
                getattr(agent._ROUTED, "allowed", None) or ())
            observed["prompt"] = getattr(agent._ROUTED, "prompt", None) or ""
            return f"AI: {action.get('input', '')}"

        with mock.patch.object(agent, "get_valid_action", side_effect=planner), \
                mock.patch.object(agent, "execute_tool",
                                  side_effect=execute or default_execute):
            step_log = agent._run_agent_turn_impl("watch a movie", [])
        return step_log, observed

    def test_parse_action_accepts_request_tool_outside_the_allowlist(self):
        agent._ROUTED.allowed = {"chat", "done", "open_app"}
        action, error = agent.parse_action(
            '{"tool": "request_tool", "input": "type_text"}')
        self.assertIsNone(error)
        self.assertEqual(action, {"tool": "request_tool", "input": "type_text"})
        blocked, error = agent.parse_action(
            '{"tool": "read_file", "input": "notes.txt"}')
        self.assertIsNone(blocked)
        self.assertIn("not available this turn", error)

    def test_request_adds_the_tool_for_the_rest_of_the_turn(self):
        step_log, observed = self.run_turn([
            ({"tool": "request_tool", "input": "type_text"}, None),
            ({"tool": "chat", "input": "ok"}, None),
        ])
        self.assertIn("System: Tool 'type_text' is now available", "\n".join(step_log))
        self.assertIn("type_text", observed["allowed"])
        self.assertIn("- type_text:", observed["prompt"])

    def test_request_tool_does_not_execute_a_tool(self):
        calls = []

        def execute(action):
            calls.append(action["tool"])
            return f"AI: {action.get('input', '')}"

        self.run_turn([
            ({"tool": "request_tool", "input": "type_text"}, None),
            ({"tool": "chat", "input": "ok"}, None),
        ], execute=execute)
        self.assertEqual(calls, ["chat"])

    def test_unknown_or_blocked_tool_is_not_added(self):
        step_log, observed = self.run_turn([
            ({"tool": "request_tool", "input": "no_such_tool"}, None),
            ({"tool": "chat", "input": "ok"}, None),
        ])
        self.assertIn("Cannot add unknown tool 'no_such_tool'", "\n".join(step_log))
        self.assertNotIn("no_such_tool", observed["allowed"])

    def test_group_mode_cannot_request_an_unsafe_tool(self):
        with mock.patch.object(agent, "GROUP_MODE", True):
            step_log, _ = self.run_turn([
                ({"tool": "request_tool", "input": "read_file"}, None),
                ({"tool": "chat", "input": "ok"}, None),
            ])
        self.assertIn("blocked in group chats", "\n".join(step_log))

    def test_granted_tool_is_then_parseable(self):
        agent._set_turn_route("watch a movie")
        ok, message = agent._grant_requested_tool("type_text")
        self.assertTrue(ok, message)
        self.assertTrue(agent._tool_allowed("type_text"))
        action, error = agent.parse_action(
            '{"tool": "type_text", "input": "hello"}')
        self.assertIsNone(error)
        self.assertEqual(action["tool"], "type_text")


if __name__ == "__main__":
    unittest.main()
