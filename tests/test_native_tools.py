"""Native function-calling adapter: schemas, tool_calls parsing, transcript.

The agent loop keeps its text contracts (step_log lines, list[str] history);
this module is the translation layer between those and the OpenAI-compatible
shape deepseek-flash expects.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import native_tools  # noqa: E402


class _FakeSpec:
    def __init__(self, name, description, schema):
        self.name = name
        self.description = description
        self.input_schema = schema


class _FakeRegistry:
    def __init__(self, specs=()):
        self._by_name = {s.name: s for s in specs}

    def get(self, name):
        return self._by_name.get(name)


def describe(name):
    return f"{name} does the thing"


class SchemaTests(unittest.TestCase):
    def test_legacy_tools_get_a_single_string_input_property(self):
        specs = native_tools.build_specs(["search_web"], registry=None,
                                         describe=describe)
        self.assertEqual(len(specs), 1)
        fn = specs[0]["function"]
        self.assertEqual(specs[0]["type"], "function")
        self.assertEqual(fn["name"], "search_web")
        params = fn["parameters"]
        self.assertEqual(params["type"], "object")
        self.assertEqual(list(params["properties"]), ["input"])
        self.assertEqual(params["properties"]["input"]["type"], "string")
        self.assertEqual(params["required"], ["input"])
        # Strict: the model may not invent extra arguments.
        self.assertIs(params["additionalProperties"], False)

    def test_registry_schema_is_used_when_one_exists(self):
        registry = _FakeRegistry([_FakeSpec(
            "scratchpad_write", "write a note",
            {"type": "object",
             "properties": {"key": {"type": "string"},
                            "text": {"type": "string"}},
             "required": ["key", "text"]})])
        specs = native_tools.build_specs(["scratchpad_write"], registry, describe)
        params = specs[0]["function"]["parameters"]
        self.assertEqual(sorted(params["properties"]), ["key", "text"])
        self.assertEqual(sorted(params["required"]), ["key", "text"])

    def test_delegate_to_qoder_advertises_prompt_and_goal(self):
        specs = native_tools.build_specs(["delegate_to_qoder"], None, describe)
        params = specs[0]["function"]["parameters"]
        self.assertEqual(sorted(params["properties"]), ["goal", "prompt"])

    def test_names_are_sorted_for_a_stable_prefix_cache(self):
        specs = native_tools.build_specs(["web_fetch", "search_web"], None,
                                         describe)
        self.assertEqual([s["function"]["name"] for s in specs],
                         ["search_web", "web_fetch"])

    def test_descriptions_are_never_empty(self):
        specs = native_tools.build_specs(["mystery_tool"], None,
                                         lambda n: "")
        self.assertTrue(specs[0]["function"]["description"].strip())


class ToolCallParsingTests(unittest.TestCase):
    @staticmethod
    def _message(calls):
        return {"role": "assistant", "content": "", "tool_calls": calls}

    @staticmethod
    def _call(call_id, name, arguments):
        return {"id": call_id, "type": "function",
                "function": {"name": name,
                             "arguments": json.dumps(arguments)
                             if isinstance(arguments, dict) else arguments}}

    def test_single_call_becomes_one_action(self):
        message = self._message([self._call("c0", "search_web",
                                            {"input": "deepseek news"})])
        actions, errors = native_tools.actions_from_tool_calls(
            message, valid_names={"search_web"}, allowed={"search_web"})
        self.assertEqual(errors, [])
        self.assertEqual(actions, [{"tool": "search_web",
                                    "input": "deepseek news"}])

    def test_parallel_calls_become_a_batch_list(self):
        message = self._message([
            self._call("c0", "search_web", {"input": "a"}),
            self._call("c1", "web_fetch", {"input": "https://x"}),
        ])
        actions, errors = native_tools.actions_from_tool_calls(
            message, valid_names={"search_web", "web_fetch"},
            allowed={"search_web", "web_fetch"})
        self.assertEqual(errors, [])
        self.assertEqual([a["tool"] for a in actions],
                         ["search_web", "web_fetch"])

    def test_hallucinated_tool_name_is_rejected_not_executed(self):
        """deepseek-flash accepts any name in a tools array we hand it, and
        can invent one that was never registered."""
        message = self._message([self._call("c0", "delete_everything",
                                            {"input": "x"})])
        actions, errors = native_tools.actions_from_tool_calls(
            message, valid_names={"search_web"}, allowed={"search_web"})
        self.assertEqual(actions, [])
        self.assertIn("delete_everything", errors[0])

    def test_tool_outside_this_turn_allowlist_is_rejected(self):
        message = self._message([self._call("c0", "run_command",
                                            {"input": "x"})])
        actions, errors = native_tools.actions_from_tool_calls(
            message, valid_names={"run_command"}, allowed=set())
        self.assertEqual(actions, [])
        self.assertTrue(errors)

    def test_malformed_arguments_json_is_reported_not_raised(self):
        message = self._message([self._call("c0", "search_web", "{not json")])
        actions, errors = native_tools.actions_from_tool_calls(
            message, valid_names={"search_web"}, allowed={"search_web"})
        self.assertEqual(actions, [])
        self.assertTrue(errors)

    def test_missing_input_key_becomes_an_empty_string(self):
        message = self._message([self._call("c0", "system_status", {})])
        actions, errors = native_tools.actions_from_tool_calls(
            message, valid_names={"system_status"}, allowed={"system_status"})
        self.assertEqual(errors, [])
        self.assertEqual(actions, [{"tool": "system_status", "input": ""}])

    def test_object_input_is_preserved_for_envelope_tools(self):
        message = self._message([self._call("c0", "edit_docx_safe",
                                            {"path": "a.docx", "ops": []})])
        actions, errors = native_tools.actions_from_tool_calls(
            message, valid_names={"edit_docx_safe"},
            allowed={"edit_docx_safe"},
            object_input_tools={"edit_docx_safe"})
        self.assertEqual(errors, [])
        self.assertEqual(actions[0]["input"], {"path": "a.docx", "ops": []})

    def test_delegate_prompt_and_goal_are_mapped(self):
        message = self._message([self._call(
            "c0", "delegate_to_qoder", {"prompt": "build it",
                                        "goal": "tests pass"})])
        actions, errors = native_tools.actions_from_tool_calls(
            message, valid_names={"delegate_to_qoder"},
            allowed={"delegate_to_qoder"})
        self.assertEqual(errors, [])
        self.assertEqual(actions[0]["input"], "build it")
        self.assertEqual(actions[0]["goal"], "tests pass")

    def test_no_tool_calls_is_a_plain_reply(self):
        actions, errors = native_tools.actions_from_tool_calls(
            {"role": "assistant", "content": "hello"}, valid_names={"x"},
            allowed={"x"})
        self.assertIsNone(actions)
        self.assertEqual(errors, [])


class TranscriptTests(unittest.TestCase):
    def test_step_log_becomes_assistant_and_tool_messages(self):
        step_log = ["Action: search_web(deepseek) -> Result: 3 results"]
        messages = native_tools.messages_from_transcript(step_log)
        self.assertEqual([m["role"] for m in messages],
                         ["assistant", "tool"])
        assistant = messages[0]
        call = assistant["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "search_web")
        self.assertEqual(json.loads(call["function"]["arguments"]),
                         {"input": "deepseek"})
        tool = messages[1]
        # The API matches a tool message to its call by id.
        self.assertEqual(tool["tool_call_id"], call["id"])
        self.assertEqual(tool["content"], "3 results")

    def test_chat_and_done_lines_become_assistant_text(self):
        step_log = ["Action: chat(hi) -> Result: AI: hello", "Done: bye"]
        messages = native_tools.messages_from_transcript(step_log)
        self.assertEqual([m["role"] for m in messages], ["assistant", "assistant"])
        self.assertEqual(messages[0]["content"], "AI: hello")
        self.assertEqual(messages[1]["content"], "bye")

    def test_system_lines_are_carried_as_user_notes(self):
        step_log = ["System: Tool 'type_text' is now available for this turn."]
        messages = native_tools.messages_from_transcript(step_log)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "user")
        self.assertIn("type_text", messages[0]["content"])

    def test_ids_are_stable_across_rebuilds(self):
        step_log = ["Action: search_web(a) -> Result: one",
                    "Action: web_fetch(b) -> Result: two"]
        first = native_tools.messages_from_transcript(step_log)
        second = native_tools.messages_from_transcript(step_log)
        self.assertEqual([c["id"] for m in first for c in m.get("tool_calls", [])],
                         [c["id"] for m in second for c in m.get("tool_calls", [])])

    def test_empty_transcript_yields_no_messages(self):
        self.assertEqual(native_tools.messages_from_transcript([]), [])

    def test_result_text_is_truncated_to_the_same_budget_as_the_prompt(self):
        long_output = "x" * (native_tools.MAX_TOOL_MESSAGE_CHARS + 5000)
        step_log = [f"Action: read_file(a) -> Result: {long_output}"]
        messages = native_tools.messages_from_transcript(step_log)
        self.assertLessEqual(len(messages[1]["content"]),
                             native_tools.MAX_TOOL_MESSAGE_CHARS + 64)


if __name__ == "__main__":
    unittest.main(verbosity=2)
