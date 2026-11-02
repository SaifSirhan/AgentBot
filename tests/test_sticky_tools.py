"""Regression tests for tool carryover across follow-up turns.

Run: python -m unittest tests.test_sticky_tools -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent  # noqa: E402


class StickyToolTests(unittest.TestCase):
    def setUp(self):
        agent._clear_turn_route()
        agent._clear_sticky_tools()
        self.addCleanup(agent._clear_turn_route)
        self.addCleanup(agent._clear_sticky_tools)
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

    def test_rag_search_carries_for_two_followups_then_expires(self):
        history = []
        first_allowed = []
        log = self.run_turn(
            "use rag_search to find the telegram messages",
            [
                {"tool": "rag_search", "input": "love you"},
                {"tool": "chat", "input": "found it"},
            ],
            history,
            first_allowed,
        )
        self.assertIn("rag_search", first_allowed[0])
        self.assertTrue(
            any("Action: rag_search" in line for line in log), log)

        second_allowed = []
        self.run_turn(
            "yeah pull the context of if someone look liked you",
            [{"tool": "chat", "input": "here is the context"}],
            history,
            second_allowed,
        )
        self.assertIn("rag_search", second_allowed[0])

        third_allowed = []
        self.run_turn(
            "yeah",
            [{"tool": "chat", "input": "ok"}],
            history,
            third_allowed,
        )
        self.assertIn("rag_search", third_allowed[0])

        fourth_allowed = []
        self.run_turn(
            "ok",
            [{"tool": "chat", "input": "ok"}],
            history,
            fourth_allowed,
        )
        self.assertNotIn("rag_search", fourth_allowed[0])

    def test_tool_used_before_an_aborted_turn_stays_pinned(self):
        """A chain that dies mid-way must not lose the tools it already used.

        Sticky tools were only recorded on the normal exit path, so a turn that
        raised left the follow-up with nothing carried. The tool is deliberately
        outside FALLBACK_TOOLS, which would otherwise hide the regression.
        """
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

    def test_requested_tool_carries_into_the_followup(self):
        history = []
        self.run_turn(
            "open the weather report",
            [
                {"tool": "request_tool", "input": "search_web"},
                {"tool": "chat", "input": "ok"},
            ],
            history,
            [],
        )
        followup_allowed = []
        self.run_turn(
            "yeah",
            [{"tool": "chat", "input": "ok"}],
            history,
            followup_allowed,
        )
        self.assertIn("search_web", followup_allowed[0])

    def test_auto_retrieved_context_marks_rag_search_sticky(self):
        history = []
        allowed = []
        sequence = iter([({"tool": "chat", "input": "Moss is a contact"})])

        def planner(_input, _context):
            allowed.append(set(getattr(agent._ROUTED, "allowed", None) or ()))
            return next(sequence), None

        with mock.patch.object(
                agent, "get_valid_action", side_effect=planner), \
                mock.patch.object(
                    agent, "execute_tool",
                    side_effect=lambda action: "AI: Moss is a contact"), \
                mock.patch(
                    "rag_tool.search_documents",
                    return_value="[From: messages.html]\nMoss said hi"):
            agent._run_agent_turn_impl(
                "who is moss in the telegram chat", history)

        followup_allowed = []
        self.run_turn(
            "yeah",
            [{"tool": "chat", "input": "ok"}],
            history,
            followup_allowed,
        )
        self.assertIn("rag_search", followup_allowed[0])

    def test_group_turns_do_not_store_or_read_sticky_tools(self):
        history = []
        with mock.patch.object(agent, "GROUP_MODE", True):
            self.assertEqual(agent._begin_sticky_turn(history), ())
            agent._remember_sticky_tools(history, ["rag_search"])
            self.assertEqual(agent._begin_sticky_turn(history), ())


if __name__ == "__main__":
    unittest.main()
