"""Regression tests for the full toolset across turns.

Routing no longer carries a keyword-selected subset between turns, so every
turn exposes the same catalog. The old carryover tests are replaced by this
invariant.
Run: python -m unittest tests.test_sticky_tools -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent  # noqa: E402


class FullToolsetTurnTests(unittest.TestCase):
    def setUp(self):
        agent._clear_turn_route()
        self.addCleanup(agent._clear_turn_route)
        patchers = [
            mock.patch.object(agent, "GROUP_MODE", False),
            mock.patch.object(
                agent, "_generate_skill_from_turn", return_value=""),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_turn(self, request, actions, history, allowed):
        sequence = iter(actions)

        def planner(_input, _context):
            allowed.append(set(getattr(agent._ROUTED, "allowed", None) or ()))
            return next(sequence), None

        def execute(action):
            return f"AI: {action.get('input', '')}"

        with mock.patch.object(
                agent, "get_valid_action", side_effect=planner), \
                mock.patch.object(
                    agent, "execute_tool", side_effect=execute), \
                mock.patch("rag_tool.search_documents", return_value=""):
            return agent._run_agent_turn_impl(request, history)

    def test_follow_up_turn_exposes_the_same_full_catalog(self):
        history = []
        first_allowed = []
        self.run_turn(
            "use rag_search to find the telegram messages",
            [
                {"tool": "rag_search", "input": "love you"},
                {"tool": "chat", "input": "found it"},
            ],
            history,
            first_allowed,
        )
        self.assertEqual(first_allowed[0], set(agent.VALID_TOOLS))

        second_allowed = []
        self.run_turn(
            "yeah",
            [{"tool": "chat", "input": "ok"}],
            history,
            second_allowed,
        )
        self.assertEqual(second_allowed[0], set(agent.VALID_TOOLS))

    def test_a_tool_used_before_an_aborted_turn_is_still_available(self):
        history = []
        sequence = iter([
            {"tool": "screenshot_background", "input": ""},
            RuntimeError("provider died"),
        ])

        def planner(_input, _context):
            item = next(sequence)
            if isinstance(item, Exception):
                raise item
            return item, None

        with mock.patch.object(
                agent, "get_valid_action", side_effect=planner), \
                mock.patch.object(
                    agent, "execute_tool",
                    side_effect=lambda action: "Saved: shot.png"), \
                mock.patch("rag_tool.search_documents", return_value=""):
            with self.assertRaises(RuntimeError):
                agent._run_agent_turn_impl(
                    "take a screenshot of the screen", history)

        followup_allowed = []
        self.run_turn(
            "yeah",
            [{"tool": "chat", "input": "ok"}],
            history,
            followup_allowed,
        )
        self.assertIn("screenshot_background", followup_allowed[0])

    def test_group_turns_do_not_publish_a_turn_allowlist(self):
        with mock.patch.object(agent, "GROUP_MODE", True):
            agent._set_turn_route("hello")
            self.assertIsNone(getattr(agent._ROUTED, "allowed", None))


if __name__ == "__main__":
    unittest.main()
