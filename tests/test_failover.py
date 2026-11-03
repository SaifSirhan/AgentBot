"""
Regression tests for the single-client call path and empty-reply handling.

These were module-level functions for a long time, which meant
`python -m unittest discover` never collected them — they only ran when this
file was executed directly. They are a TestCase now so the suite counts them.

Run: python -m unittest tests.test_failover -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Keep tool-result mood writes out of the user's real AgentMemory.
os.environ.setdefault(
    "AGENTBOT_MOOD_FILE",
    os.path.join(tempfile.gettempdir(), "agentbot_test_mood.json"))
try:
    os.remove(os.environ["AGENTBOT_MOOD_FILE"])
except OSError:
    pass

import agent  # noqa: E402


class _DispatchSwap(unittest.TestCase):
    """Replace the client call, record which names were tried.

    Brain cooldown state is module-global, so a test that simulates a 5xx
    marks deepseek as cooling for 30s and every later test is skipped by the
    admission gate. Snapshot and clear it, then restore.
    """

    def setUp(self):
        self.original = agent._dispatch_call
        self.seen = []
        saved_state = dict(agent._brain_state)
        saved_last = (agent.LAST_BRAIN, agent._LAST_PROVIDER,
                      agent._LAST_FAILURE_REASON)

        def restore():
            agent._dispatch_call = self.original
            agent._brain_state.clear()
            agent._brain_state.update(saved_state)
            (agent.LAST_BRAIN, agent._LAST_PROVIDER,
             agent._LAST_FAILURE_REASON) = saved_last

        self.addCleanup(restore)
        agent._brain_state.clear()
        agent.LAST_BRAIN = agent._LAST_PROVIDER = agent._LAST_FAILURE_REASON = None

    def install(self, replies):
        """`replies` is the value (or per-call list of values) to return."""
        def fake_dispatch(name, prompt, force_json, max_tokens):
            self.seen.append(name)
            index = len(self.seen) - 1
            if isinstance(replies, list):
                return replies[index] if index < len(replies) else replies[-1]
            return replies

        agent._dispatch_call = fake_dispatch
        return fake_dispatch


class SingleClientCallTests(_DispatchSwap):
    def test_only_deepseek_is_tried(self):
        self.install("ok")
        self.assertEqual(agent._call_llm("test"), "ok")
        self.assertEqual(self.seen, ["deepseek"])
        self.assertEqual(agent._LAST_PROVIDER, "deepseek")

    def test_empty_reply_is_not_success(self):
        """A 200 with content == '' must surface as an error, never as ''.

        deepseek-flash reasons before answering; when reasoning consumes the
        whole max_tokens budget it returns HTTP 200 with an empty content.
        """
        self.install("")
        result = agent._call_llm("test")
        self.assertTrue(result.startswith("Error:"), result)
        self.assertNotEqual(result, "")

    def test_whitespace_reply_is_not_success(self):
        self.install("   \n\t")
        result = agent._call_llm("test")
        self.assertTrue(result.startswith("Error:"), result)

    def test_empty_reply_records_failure_reason(self):
        self.install("")
        agent._call_llm("test")
        self.assertEqual(agent._LAST_FAILURE_REASON, "empty")

    def test_error_reply_records_failure_reason(self):
        self.install("Error: HTTP 503")
        result = agent._call_llm("test")
        self.assertTrue(result.startswith("Error:"), result)
        self.assertEqual(agent._LAST_FAILURE_REASON, "error")

    def test_force_provider_cannot_select_another_client(self):
        """The kwarg is kept for existing callers; every value uses the one client."""
        self.install("ok")
        self.assertEqual(agent._call_llm("test", force_provider="groq"), "ok")
        self.assertEqual(self.seen, ["deepseek"],
                         "a stale provider name must not be dispatched to")

    def test_no_second_client_exists_to_fall_back_to(self):
        self.install(["", "should never be reached"])
        agent._call_llm("test")
        self.assertEqual(self.seen, ["deepseek"])


class ParsingTests(unittest.TestCase):
    def test_malformed_json_action_handled(self):
        """parse_action on garbage must not raise. It returns (action, error)."""
        result = agent.parse_action("this is not json and not an action")
        # Real contract: parse_action returns a (action, error) tuple and falls
        # back to a chat action on plain text rather than raising.
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        action, error = result
        self.assertTrue(action is None or isinstance(action, dict))

    def test_unknown_tool_name_rejected(self):
        """execute_tool with a tool not in VALID_TOOLS must return an error
        string, not raise."""
        result = agent.execute_tool({"tool": "definitely_not_a_real_tool",
                                     "input": ""})
        self.assertIsInstance(result, str)
        self.assertTrue("error" in result.lower() or "unknown" in result.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
