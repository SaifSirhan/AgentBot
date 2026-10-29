"""Tests for dynamic tool requests when the router allowlist is too small.

Run: python -m unittest tests.test_request_tool -v
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
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

    def test_misspelled_tool_is_granted_with_the_canonical_name(self):
        agent._ROUTED.allowed = {"chat", "done"}
        ok, message = agent._grant_requested_tool("web_search")
        self.assertTrue(ok, message)
        self.assertEqual(
            message,
            "ERROR: Tool 'web_search' not found. "
            "Did you mean 'search_web'? I have granted it.")
        self.assertTrue(agent._tool_allowed("search_web"))
        self.assertFalse(agent._tool_allowed("web_search"))

    def test_typo_and_module_names_map_to_real_catalog_names(self):
        for requested, canonical in (("type_txt", "type_text"),
                                     ("serach_web", "search_web"),
                                     ("weather_news", "weather")):
            with self.subTest(requested=requested):
                agent._clear_turn_route()
                agent._ROUTED.allowed = {"chat", "done"}
                ok, message = agent._grant_requested_tool(requested)
                self.assertTrue(ok, message)
                self.assertIn(f"Did you mean '{canonical}'?", message)
                self.assertTrue(agent._tool_allowed(canonical))

    def test_blocked_suggestion_does_not_claim_a_grant(self):
        with mock.patch.object(agent, "GROUP_MODE", True):
            agent._ROUTED.allowed = {"chat", "done"}
            ok, message = agent._grant_requested_tool("execute_pythn")
        self.assertFalse(ok, message)
        self.assertIn("blocked in group chats", message)
        self.assertNotIn("I have granted it", message)
        self.assertFalse(agent._tool_allowed("execute_python"))

    def test_misspelled_request_continues_the_turn_with_the_real_tool(self):
        step_log, observed = self.run_turn([
            ({"tool": "request_tool", "input": "web_search"}, None),
            ({"tool": "chat", "input": "ok"}, None),
        ])
        log = "\n".join(step_log)
        self.assertIn("Did you mean 'search_web'? I have granted it.", log)
        self.assertIn("search_web", observed["allowed"])
        self.assertNotIn("web_search", observed["allowed"])
        self.assertIn("- search_web:", observed["prompt"])

    def test_misspelled_grant_updates_the_autonomous_catalog(self):
        agent._ROUTED.allowed = {"chat", "done"}
        agent._ROUTED.autonomous_loop = SimpleNamespace(tools={})
        ok, message = agent._grant_requested_tool("web_search")
        self.assertTrue(ok, message)
        self.assertIn("search_web", agent._ROUTED.autonomous_loop.tools)
        self.assertNotIn("web_search", agent._ROUTED.autonomous_loop.tools)

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
