"""Option C: the autonomous engine's LLM seam speaks native tool calling.

AutonomousLoop takes a string back from its `llm` callable — "DONE" or JSON
{"tool", "args"} — and all of its authorize/execute/verify/scratchpad machinery
is built around that. The native adapter therefore lives in _autonomous_llm and
returns the same shapes, so the loop itself does not change.
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402

MESSAGES = [
    {"role": "system", "content": "TOOLS: [{\"name\": \"search_web\"}]"},
    {"role": "user", "content": "You are at step 1. Your goal is x."},
]


def _call(name="search_web", arguments='{"input": "q"}', call_id="c0"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


class AutonomousNativeAdapterTests(unittest.TestCase):
    def setUp(self):
        agent._clear_turn_route()
        agent._ROUTED.allowed = {"search_web", "web_fetch", "chat", "done"}
        self.addCleanup(agent._clear_turn_route)

    def test_one_tool_call_becomes_a_single_action_object(self):
        message = {"role": "assistant", "content": "", "tool_calls": [_call()]}
        with mock.patch.object(agent, "_call_deepseek_native",
                               return_value=(message, None)), \
             mock.patch.object(agent, "ask_llm_direct") as text_path:
            reply = agent._autonomous_llm(MESSAGES)
        self.assertEqual(json.loads(reply),
                         {"tool": "search_web", "args": {"input": "q"}})
        text_path.assert_not_called()

    def test_parallel_calls_become_the_batch_list_the_loop_accepts(self):
        message = {"role": "assistant", "content": "", "tool_calls": [
            _call("search_web", '{"input": "a"}', "c0"),
            _call("web_fetch", '{"input": "https://x"}', "c1"),
        ]}
        with mock.patch.object(agent, "_call_deepseek_native",
                               return_value=(message, None)):
            reply = agent._autonomous_llm(MESSAGES)
        parsed = json.loads(reply)
        self.assertIsInstance(parsed, list)
        self.assertEqual([item["tool"] for item in parsed],
                         ["search_web", "web_fetch"])
        self.assertTrue(all(set(item) == {"tool", "args"} for item in parsed))

    def test_args_are_the_dict_the_authorizer_expects(self):
        message = {"role": "assistant", "content": "",
                   "tool_calls": [_call("search_web", '{"input": "q"}')]}
        with mock.patch.object(agent, "_call_deepseek_native",
                               return_value=(message, None)):
            parsed = json.loads(agent._autonomous_llm(MESSAGES))
        self.assertIsInstance(parsed["args"], dict)
        self.assertEqual(set(parsed["args"]), {"input"})

    def test_plain_text_done_is_returned_as_done(self):
        message = {"role": "assistant", "content": "DONE", "tool_calls": []}
        with mock.patch.object(agent, "_call_deepseek_native",
                               return_value=(message, None)):
            self.assertEqual(agent._autonomous_llm(MESSAGES), "DONE")

    def test_finish_tool_is_advertised(self):
        """A natively-called model answers in prose, not the literal string
        DONE, and the loop counts prose as an invalid reply. So completion
        needs a schema the model can actually call."""
        seen = {}

        def capture(messages, tools, *a, **k):
            seen["names"] = [t["function"]["name"] for t in tools]
            return ({"role": "assistant", "content": "hi", "tool_calls": []}, None)

        with mock.patch.object(agent, "_call_deepseek_native", side_effect=capture):
            agent._autonomous_llm(MESSAGES)
        self.assertIn("finish", seen["names"])

    def test_a_finish_call_becomes_done_for_the_loop(self):
        message = {"role": "assistant", "content": "The headline is X.",
                   "tool_calls": [_call("finish", '{"answer": "here it is"}')]}
        with mock.patch.object(agent, "_call_deepseek_native",
                               return_value=(message, None)):
            self.assertEqual(agent._autonomous_llm(MESSAGES), "DONE")

    def test_finish_alongside_other_calls_still_finishes(self):
        message = {"role": "assistant", "content": "", "tool_calls": [
            _call("search_web", '{"input": "q"}', "c0"),
            _call("finish", "{}", "c1"),
        ]}
        with mock.patch.object(agent, "_call_deepseek_native",
                               return_value=(message, None)):
            self.assertEqual(agent._autonomous_llm(MESSAGES), "DONE")

    def test_advertised_tools_are_this_turns_allowlist_plus_request_tool(self):
        seen = {}

        def capture(messages, tools, *a, **k):
            seen["tools"] = [t["function"]["name"] for t in tools]
            seen["messages"] = messages
            return ({"role": "assistant", "content": "DONE", "tool_calls": []}, None)

        with mock.patch.object(agent, "_call_deepseek_native", side_effect=capture):
            agent._autonomous_llm(MESSAGES)
        self.assertEqual(seen["tools"],
                         ["finish", "request_tool", "search_web", "web_fetch"])
        self.assertNotIn("chat", seen["tools"])
        self.assertNotIn("done", seen["tools"])
        # The loop's own messages survive the hop, so its scratchpad and grant
        # notes are not flattened away.
        self.assertEqual(seen["messages"][0]["role"], "system")
        self.assertIn("step 1", seen["messages"][1]["content"])

    def test_transport_failure_falls_back_to_the_text_protocol(self):
        with mock.patch.object(agent, "_call_deepseek_native",
                               return_value=(None, "Error: HTTP 503")), \
             mock.patch.object(agent, "ask_llm_direct",
                               return_value='{"tool": "search_web", "input": "q"}') as text_path:
            reply = agent._autonomous_llm(MESSAGES)
        text_path.assert_called()
        # The legacy {"tool","input"} shape is normalised for the loop as before.
        self.assertEqual(json.loads(reply),
                         {"tool": "search_web", "args": {"input": "q"}})

    def test_a_name_outside_the_allowlist_is_not_turned_into_an_action(self):
        """The engine's own authorize() is the backstop, but the adapter should
        not hand it a call for a tool that was never advertised."""
        agent._ROUTED.allowed = {"search_web", "chat", "done"}
        message = {"role": "assistant", "content": "",
                   "tool_calls": [_call("execute_python", '{"input": "1"}')]}
        with mock.patch.object(agent, "_call_deepseek_native",
                               return_value=(message, None)), \
             mock.patch.object(agent, "ask_llm_direct",
                               return_value="retry please"):
            reply = agent._autonomous_llm(MESSAGES)
        self.assertNotIn("execute_python", reply)

    def test_context_prompt_still_wraps_the_native_turn(self):
        """Protected goal text and follow-up constraints must reach the model
        the same way they do on the text path."""
        agent._ROUTED.protected_user_message = "User: build the thing"
        seen = {}

        def capture(messages, tools, *a, **k):
            seen["system"] = messages[0]["content"]
            return ({"role": "assistant", "content": "DONE", "tool_calls": []}, None)

        with mock.patch.object(agent, "_call_deepseek_native", side_effect=capture):
            agent._autonomous_llm(MESSAGES)
        self.assertIn("build the thing", seen["system"])


    def test_prose_with_no_call_is_a_completion_claim_not_an_invalid_reply(self):
        """The loop's repair budget is one step. Counting a finished-looking
        prose answer as invalid burns it and reports an unmet goal after work
        that succeeded, so prose becomes DONE and the verifier judges it."""
        message = {"role": "assistant",
                   "content": "The headline is: DeepSeek ships Super-agent.",
                   "tool_calls": []}
        with mock.patch.object(agent, "_call_deepseek_native",
                               return_value=(message, None)):
            self.assertEqual(agent._autonomous_llm(MESSAGES), "DONE")

    def test_an_empty_native_reply_falls_back_to_the_text_protocol(self):
        with mock.patch.object(agent, "_call_deepseek_native",
                               return_value=({"role": "assistant", "content": "",
                                              "tool_calls": []}, None)), \
             mock.patch.object(agent, "ask_llm_direct",
                               return_value="DONE") as text_path:
            self.assertEqual(agent._autonomous_llm(MESSAGES), "DONE")
        text_path.assert_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
