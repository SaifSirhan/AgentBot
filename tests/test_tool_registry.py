"""ToolSpec validation and registry dispatch tests.

Run: python -m unittest tests.test_tool_registry -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from tool_registry import (
    InputValidationError,
    ToolRegistry,
    ToolSpec,
    validate_input,
)


def make_spec(name="demo", func=None, schema=None, effect="read_only", timeout=5):
    return ToolSpec(
        name=name,
        description=f"The {name} tool.",
        input_schema=schema or {"type": "string"},
        effect_class=effect,
        timeout=timeout,
        func=func or (lambda value: f"ok:{value}"),
    )


class ValidateInputTests(unittest.TestCase):
    def test_accepts_a_string_and_rejects_a_list(self):
        schema = {"type": "string"}
        validate_input(schema, "print(1)")
        with self.assertRaises(InputValidationError) as caught:
            validate_input(schema, ["print(1)"])
        self.assertIn("string", str(caught.exception))
        self.assertIn("array", str(caught.exception))

    def test_string_length_and_pattern_constraints(self):
        schema = {
            "type": "string",
            "minLength": 3,
            "maxLength": 5,
            "pattern": r"^a",
        }
        validate_input(schema, "abc")
        for value in ("a", "zzz", "abcdef"):
            with self.subTest(value=value):
                with self.assertRaises(InputValidationError):
                    validate_input(schema, value)

    def test_object_required_properties_and_nested_types(self):
        schema = {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["path"],
        }
        validate_input(schema, {"path": "a.txt"})
        validate_input(schema, {"path": "a.txt", "count": 2})
        for value in ({"count": 2}, {"path": "a.txt", "count": "2"}):
            with self.subTest(value=value):
                with self.assertRaises(InputValidationError):
                    validate_input(schema, value)

    def test_boolean_is_not_accepted_as_an_integer(self):
        with self.assertRaises(InputValidationError):
            validate_input({"type": "integer"}, True)


class ToolSpecTests(unittest.TestCase):
    def test_rejects_unknown_effect_class(self):
        with self.assertRaises(ValueError):
            make_spec(effect="destructive")

    def test_rejects_non_positive_timeout(self):
        with self.assertRaises(ValueError):
            make_spec(timeout=0)

    def test_validate_delegates_to_the_input_schema(self):
        spec = make_spec(schema={"type": "string", "minLength": 2})
        spec.validate("ok")
        with self.assertRaises(InputValidationError):
            spec.validate("x")

    def test_polling_defaults_to_false(self):
        self.assertFalse(make_spec().is_polling)


class ToolRegistryTests(unittest.TestCase):
    def test_catalog_text_is_sorted_and_shows_metadata(self):
        registry = ToolRegistry()
        registry.register(make_spec(name="zeta", effect="write", timeout=9))
        registry.register(make_spec(name="alpha", timeout=3))

        text = registry.catalog_text()

        self.assertLess(text.index("alpha"), text.index("zeta"))
        self.assertIn("alpha", text)
        self.assertIn("zeta", text)
        self.assertIn("write", text)
        self.assertIn("9s", text)
        self.assertIn('{"type": "string"}', text)

    def test_duplicate_names_are_rejected(self):
        registry = ToolRegistry()
        registry.register(make_spec(name="demo"))
        with self.assertRaises(ValueError):
            registry.register(make_spec(name="demo"))

    def test_unknown_tool_is_absent(self):
        registry = ToolRegistry()
        self.assertIsNone(registry.get("missing"))
        self.assertNotIn("missing", registry)
        self.assertEqual(registry.names, ())


class AgentRegistryIntegrationTests(unittest.TestCase):
    CORE_TOOLS = {
        "execute_python",
        "open_app",
        "search_web",
        "read_file",
        "write_file",
        "click_coordinates",
        "type_text",
        "screenshot_background",
        "find_files",
        "patch_file",
        "weather",
        "news",
        "current_time",
        "remember",
        "telegram_user_send",
    }

    def setUp(self):
        import agent

        self.agent = agent
        agent._clear_turn_route()
        agent.reset_python_failures()
        self.addCleanup(agent._clear_turn_route)
        for name, value in (("GROUP_MODE", False),
                            ("note_tool_result", mock.Mock())):
            patcher = mock.patch.object(agent, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_fifteen_core_tools_are_registered(self):
        self.assertEqual(set(self.agent.TOOL_REGISTRY.names), self.CORE_TOOLS)

    def test_polling_tools_are_flagged_in_the_registry(self):
        registry = self.agent.TOOL_REGISTRY
        for name in ("read_file", "find_files", "screenshot_background"):
            with self.subTest(name=name):
                self.assertTrue(registry.get(name).is_polling)
        self.assertFalse(registry.get("execute_python").is_polling)

    def test_registry_catalog_mentions_every_core_tool(self):
        text = self.agent.TOOL_REGISTRY.catalog_text()
        for name in self.CORE_TOOLS:
            self.assertIn(name, text)

    def test_execute_python_rejects_a_list_before_running(self):
        with mock.patch.object(self.agent, "_execute_python_once") as run:
            result = self.agent.execute_tool(
                {"tool": "execute_python", "input": ["print(1)"]})

        run.assert_not_called()
        self.assertTrue(result.startswith(
            "ERROR: Invalid input for execute_python. Expected "))
        self.assertIn('"type": "string"', result)

    def test_valid_execute_python_input_reaches_the_function(self):
        with mock.patch.object(
                self.agent, "_execute_python_once", return_value="ran"):
            result = self.agent.execute_tool(
                {"tool": "execute_python", "input": "print(1)"})

        self.assertEqual(result, "ran")

    def test_unregistered_tool_falls_back_to_the_legacy_chain(self):
        result = self.agent.execute_tool({"tool": "chat", "input": "hello"})
        self.assertEqual(result, "AI: hello")

    def test_write_file_still_receives_the_action_folder(self):
        with mock.patch.object(
                self.agent, "write_file", return_value="written") as write:
            result = self.agent.execute_tool({
                "tool": "write_file",
                "input": "note.txt|hello",
                "folder": "reports",
            })

        self.assertEqual(result, "written")
        write.assert_called_once_with("note.txt", "hello", "reports")

    def test_current_time_accepts_empty_input(self):
        with mock.patch.object(
                self.agent, "current_time", return_value="now"):
            result = self.agent.execute_tool(
                {"tool": "current_time", "input": ""})

        self.assertEqual(result, "now")


class PromptCatalogIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import agent
        import prompt_catalog
        import prompt_router

        cls.agent = agent
        cls.prompt_router = prompt_router
        cls.tools = prompt_catalog.build_tools(frozenset(agent.VALID_TOOLS))

    def route(self, request):
        return self.prompt_router.build_dynamic_prompt(
            request,
            self.tools,
            count_tokens=lambda text: len(text) // 4,
            max_tokens=20000,
            tool_registry=self.agent.TOOL_REGISTRY,
        )

    def line_for(self, prompt, name):
        return next(
            line for line in prompt.splitlines()
            if line.startswith(f"- {name}:"))

    def test_execute_python_prompt_line_includes_the_registry_schema(self):
        result = self.route("use execute_python to run this")
        line = self.line_for(result.system_prompt, "execute_python")
        self.assertIn('"minLength": 1', line)
        self.assertIn('"type": "string"', line)

    def test_unregistered_tools_keep_their_old_description(self):
        result = self.route("use make_folder to create a folder")
        line = self.line_for(result.system_prompt, "make_folder")
        self.assertNotIn("input:", line)
        self.assertIn("Create a folder.", line)

    def test_agent_route_prompt_uses_the_registry_and_stays_in_budget(self):
        prompt, _selected = self.agent._route_prompt(
            "use execute_python to write a python script")
        self.assertIn("- execute_python:", prompt)
        self.assertIn('"type": "string"', prompt)
        self.assertLessEqual(
            len(prompt) // 4, self.agent.MAX_PROMPT_TOKENS)


if __name__ == "__main__":
    unittest.main()
