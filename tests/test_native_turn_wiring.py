"""Native tool calling wired into the turn loop.

`get_valid_action` asks deepseek-flash with a real `tools` array and reads
`tool_calls` back. The text protocol stays as the path taken when the native
call itself fails, so a transport problem cannot end a turn with nothing.
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402
import native_tools  # noqa: E402


def _tool_call_message(name="search_web", arguments='{"input": "news"}',
                       call_id="call_0", content=""):
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        }],
    }


class NativeCallShapeTests(unittest.TestCase):
    def setUp(self):
        saved_key = agent.DEEPSEEK_API_KEY
        self.addCleanup(setattr, agent, "DEEPSEEK_API_KEY", saved_key)
        self.post = mock.Mock()
        self.response = mock.Mock(status_code=200, text="")
        self.response.json.return_value = {"choices": [{
            "message": _tool_call_message(), "finish_reason": "tool_calls"}]}
        self.post.return_value = self.response
        self._real_post = agent.requests.post
        agent.requests.post = self.post
        self.addCleanup(setattr, agent.requests, "post", self._real_post)

    def test_body_carries_messages_tools_and_auto_choice(self):
        agent.DEEPSEEK_API_KEY = "k"
        messages = [{"role": "system", "content": "instructions"}]
        tools = native_tools.build_specs(["search_web"], None, lambda n: "d")
        message, error = agent._call_deepseek_native(messages, tools)
        self.assertIsNone(error)
        self.assertEqual(message["tool_calls"][0]["function"]["name"],
                         "search_web")
        body = self.post.call_args.kwargs["json"]
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["messages"], messages)
        self.assertEqual(body["tools"], tools)
        self.assertEqual(body["tool_choice"], "auto")
        self.assertNotEqual(body["tool_choice"], "required")

    def test_thinking_is_off_so_the_budget_is_not_spent_reasoning(self):
        agent.DEEPSEEK_API_KEY = "k"
        agent._call_deepseek_native([{"role": "system", "content": "x"}],
                                    native_tools.build_specs(["a"], None,
                                                             lambda n: "d"))
        body = self.post.call_args.kwargs["json"]
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertEqual(body["thinking"], {"type": "disabled"})

    def test_missing_key_reports_an_error_instead_of_raising(self):
        agent.DEEPSEEK_API_KEY = ""
        message, error = agent._call_deepseek_native(
            [{"role": "system", "content": "x"}], [])
        self.assertIsNone(message)
        self.assertIn("DEEPSEEK_API_KEY", error)
        self.post.assert_not_called()

    def test_http_error_is_reported_not_raised(self):
        agent.DEEPSEEK_API_KEY = "k"
        self.response.status_code = 400
        self.response.text = "bad schema"
        message, error = agent._call_deepseek_native(
            [{"role": "system", "content": "x"}], [])
        self.assertIsNone(message)
        self.assertIn("400", error)

    def test_malformed_json_is_reported_not_raised(self):
        agent.DEEPSEEK_API_KEY = "k"
        self.response.json.side_effect = ValueError("no json")
        message, error = agent._call_deepseek_native(
            [{"role": "system", "content": "x"}], [])
        self.assertIsNone(message)
        self.assertIn("malformed", error)


class GetValidActionNativeTests(unittest.TestCase):
    def setUp(self):
        self.original = dict(agent._brain_state)
        agent._brain_state.clear()
        self.addCleanup(lambda: (agent._brain_state.clear(),
                                 agent._brain_state.update(self.original)))
        agent._set_turn_route("search the web for news")

    def tearDown(self):
        agent._clear_turn_route()

    def drive(self, request="search the web", context="context", transcript=()):
        """The loop hands the native inputs over as turn state, not arguments."""
        agent._ROUTED.native_context = context
        agent._ROUTED.transcript = list(transcript)
        return agent.get_valid_action(request, context)

    def test_native_tool_calls_become_an_action_without_the_text_path(self):
        with mock.patch.object(agent, "_call_deepseek_native",
                               return_value=(_tool_call_message(), None)), \
             mock.patch.object(agent, "ask_ai_for_plan") as text_path:
            action, error = self.drive("what's the news")
        self.assertEqual(action, {"tool": "search_web", "input": "news"})
        self.assertIsNone(error)
        text_path.assert_not_called()

    def test_plain_native_text_becomes_a_chat_action(self):
        message = {"role": "assistant", "content": "It is raining.",
                   "tool_calls": []}
        with mock.patch.object(agent, "_call_deepseek_native",
                               return_value=(message, None)):
            action, error = self.drive("how's the weather")
        self.assertEqual(action["tool"], "chat")
        self.assertEqual(action["input"], "It is raining.")

    def test_transport_failure_falls_back_to_the_text_protocol(self):
        with mock.patch.object(agent, "_call_deepseek_native",
                               return_value=(None, "Error: HTTP 503")), \
             mock.patch.object(agent, "ask_ai_for_plan",
                               return_value='{"tool": "search_web", "input": "x"}') as text_path:
            action, error = self.drive("search the web")
        text_path.assert_called()
        self.assertEqual(action["tool"], "search_web")

    def test_parallel_native_calls_become_a_batch_list(self):
        agent._ROUTED.allowed = {"search_web", "web_fetch", "chat", "done"}
        message = {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c0", "type": "function", "function": {
                "name": "search_web", "arguments": '{"input": "a"}'}},
            {"id": "c1", "type": "function", "function": {
                "name": "web_fetch", "arguments": '{"input": "https://x"}'}},
        ]}
        with mock.patch.object(agent, "_call_deepseek_native",
                               return_value=(message, None)):
            action, error = self.drive("search the web")
        self.assertIsNone(error)
        self.assertIsInstance(action, list)
        self.assertEqual([a["tool"] for a in action],
                         ["search_web", "web_fetch"])

    def test_a_name_outside_this_turns_allowlist_is_not_executed(self):
        """deepseek-flash echoes names we never advertised. Rejecting is not
        the same as a transport failure, so the turn reports the rejection
        instead of executing it or silently switching protocols."""
        agent._ROUTED.allowed = {"search_web", "chat", "done"}
        message = _tool_call_message(name="execute_python",
                                     arguments='{"input": "os.system(1)"}')
        with mock.patch.object(agent, "_call_deepseek_native",
                               return_value=(message, None)) as native, \
             mock.patch.object(agent, "ask_ai_for_plan") as text_path:
            action, error = self.drive("search the web")
        self.assertIsNone(action)
        self.assertTrue(error)
        self.assertEqual(native.call_count, 3,
                         "rejection should be retried with the error attached")
        text_path.assert_not_called()

    def test_prior_steps_travel_as_tool_messages(self):
        transcript = ["Action: search_web(deepseek) -> Result: 3 results"]
        seen = {}

        def capture(messages, tools, *args, **kwargs):
            seen["messages"] = messages
            return ({"role": "assistant", "content": "done", "tool_calls": []},
                    None)

        with mock.patch.object(agent, "_call_deepseek_native", side_effect=capture):
            self.drive("what did it say", context="history only",
                       transcript=transcript)
        roles = [m["role"] for m in seen["messages"]]
        self.assertEqual(roles, ["system", "assistant", "tool"])
        # The observation is carried natively, not re-printed into the prompt.
        self.assertNotIn("3 results", seen["messages"][0]["content"])
        self.assertEqual(seen["messages"][2]["content"], "3 results")

    def test_the_loop_still_works_when_native_state_is_absent(self):
        """Callers that stub get_valid_action, and paths that never set turn
        state, must keep using the text protocol."""
        agent._ROUTED.native_context = None
        with mock.patch.object(agent, "_call_deepseek_native") as native,              mock.patch.object(agent, "ask_ai_for_plan",
                               return_value='{"tool": "search_web", "input": "x"}'):
            action, error = agent.get_valid_action("q", "context")
        native.assert_not_called()
        self.assertEqual(action["tool"], "search_web")


class PromptInstructionTests(unittest.TestCase):
    def test_native_prompt_keeps_the_native_tool_call_instruction(self):
        prompt = agent._build_plan_prompt("hello", "ctx", native=True)
        self.assertNotIn('output ONLY a JSON object', prompt)
        self.assertIn("Use native tool calls", prompt)

    def test_text_prompt_still_asks_for_a_json_object(self):
        prompt = agent._build_plan_prompt("hello", "ctx")
        self.assertIn('output ONLY a JSON object', prompt)

    def test_native_prompt_is_not_larger_than_the_text_one(self):
        """The tools array costs tokens; the prompt must not grow on top."""
        native = agent._build_plan_prompt("hello", "ctx", native=True)
        text = agent._build_plan_prompt("hello", "ctx")
        self.assertLessEqual(len(native), len(text))


class BudgetTests(unittest.TestCase):
    def test_tools_array_fits_the_prompt_budget(self):
        """Routed catalog + schemas must stay inside the static-plan guard."""
        agent._set_turn_route("open spotify and play jazz")
        tools = native_tools.build_specs(
            sorted(agent._ROUTED.allowed - {"chat", "done"}),
            agent.TOOL_REGISTRY, agent._router_tool_description)
        serialized = json.dumps(tools)
        agent._clear_turn_route()
        # chars/4 is the project's token estimate.
        self.assertLess(len(serialized) / 4, 1200,
                        f"tools array is {len(serialized)/4:.0f} tokens")


if __name__ == "__main__":
    unittest.main(verbosity=2)
