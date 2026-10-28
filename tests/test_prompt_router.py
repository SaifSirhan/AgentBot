"""Rule and tool routing tests for prompt_router.

Run: python -m unittest tests.test_prompt_router -v
"""
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import prompt_router as pr


def example_tools():
    return [
        pr.Tool(
            name="stremio_play",
            description="Run the approved Stremio playback macro for a title.",
            rule_groups=("stremio",),
            keywords=("stremio", "movie", "film", "episode"),
        ),
        pr.Tool(
            name="os_control",
            description="Inspect and interact with approved desktop applications.",
            rule_groups=("os",),
            keywords=("desktop", "window", "click"),
        ),
        pr.Tool(
            name="python_execute",
            description="Execute Python for authorized workspace file operations.",
            rule_groups=("python",),
            keywords=("file", "script", "python"),
        ),
    ]


def words(text):
    return len(text.split())


class RoutingTests(unittest.TestCase):
    def test_watch_a_movie_injects_stremio_and_os_rules(self):
        result = pr.build_dynamic_prompt(
            "watch a movie",
            example_tools(),
            count_tokens=words,
            max_tokens=2000,
        )
        self.assertIn(pr.RULES["stremio"], result.system_prompt)
        self.assertIn(pr.RULES["os"], result.system_prompt)
        self.assertNotIn(pr.RULES["python"], result.system_prompt)
        self.assertEqual(
            [tool.name for tool in result.tools],
            ["stremio_play", "os_control"],
        )

    def test_edit_a_file_injects_python_rules_only(self):
        result = pr.build_dynamic_prompt(
            "edit a file",
            example_tools(),
            count_tokens=words,
            max_tokens=2000,
        )
        self.assertIn(pr.RULES["python"], result.system_prompt)
        self.assertNotIn(pr.RULES["stremio"], result.system_prompt)
        self.assertNotIn(pr.RULES["os"], result.system_prompt)
        self.assertEqual([tool.name for tool in result.tools], ["python_execute"])
        self.assertIn("- python_execute:", result.system_prompt)

    def test_top_five_tools_are_selected(self):
        tools = [
            pr.Tool(f"file_tool_{i}", f"Edit file {i}.", ("python",), ("file",))
            for i in range(8)
        ]
        result = pr.build_dynamic_prompt(
            "edit a file", tools, count_tokens=words, max_tokens=5000)
        self.assertEqual(len(result.tools), 5)
        self.assertEqual(
            [tool.name for tool in result.tools],
            [f"file_tool_{i}" for i in range(5)],
        )

    def test_budget_drops_tools_but_keeps_required_rules(self):
        tools = [
            pr.Tool(f"file_tool_{i}", f"Edit file {i}.", ("python",), ("file",))
            for i in range(2)
        ]
        one = pr.build_dynamic_prompt(
            "edit a file", tools[:1], count_tokens=len, max_tokens=10_000,
            reserve_tokens=0)
        result = pr.build_dynamic_prompt(
            "edit a file",
            tools,
            count_tokens=len,
            max_tokens=len(one.system_prompt),
            reserve_tokens=0,
        )
        self.assertEqual([tool.name for tool in result.tools], ["file_tool_0"])
        self.assertIn(pr.RULES["python"], result.system_prompt)
        self.assertIn(pr.CORE_RULES, result.system_prompt)
        self.assertLessEqual(result.token_count, len(one.system_prompt))

    def test_budget_too_small_for_rules_raises_instead_of_truncating(self):
        with self.assertRaises(ValueError):
            pr.build_dynamic_prompt(
                "edit a file", example_tools(),
                count_tokens=len, max_tokens=10, reserve_tokens=0)

    def test_classifier_runs_only_when_keywords_find_nothing(self):
        calls = []

        def classify(request):
            calls.append(request)
            return ["python"]

        keyworded = pr.build_dynamic_prompt(
            "watch a movie", example_tools(), count_tokens=words,
            classify_intents=classify, max_tokens=2000,
        )
        self.assertEqual(calls, [])
        self.assertIn(pr.RULES["stremio"], keyworded.system_prompt)

        classified = pr.build_dynamic_prompt(
            "do the mysterious thing", example_tools(), count_tokens=words,
            classify_intents=classify, max_tokens=2000,
        )
        self.assertEqual(calls, ["do the mysterious thing"])
        self.assertIn(pr.RULES["python"], classified.system_prompt)
        self.assertEqual([tool.name for tool in classified.tools], ["python_execute"])

    def test_classifier_cannot_return_unknown_intents(self):
        with self.assertRaises(ValueError):
            pr.build_dynamic_prompt(
                "something else", example_tools(), count_tokens=words,
                classify_intents=lambda _request: ["admin"], max_tokens=2000)

    def test_duplicate_tool_names_and_unknown_groups_are_rejected(self):
        with self.assertRaises(ValueError):
            pr.build_dynamic_prompt(
                "edit a file",
                [pr.Tool("x", "d", ("python",)), pr.Tool("x", "d", ("python",))],
                count_tokens=words,
            )
        with self.assertRaises(ValueError):
            pr.build_dynamic_prompt(
                "edit a file",
                [pr.Tool("x", "d", ("unknown",))],
                count_tokens=words,
            )

    def test_unmatched_request_falls_back_to_os_and_python_tools(self):
        result = pr.build_dynamic_prompt(
            "tell me a joke", example_tools(), count_tokens=words, max_tokens=2000)
        self.assertNotEqual(result.tools, ())
        self.assertEqual(
            [tool.name for tool in result.tools],
            ["os_control", "python_execute"],
        )
        self.assertIn(pr.RULES["os"], result.system_prompt)
        self.assertIn(pr.RULES["python"], result.system_prompt)
        self.assertNotIn(pr.RULES["stremio"], result.system_prompt)
        self.assertNotIn("No relevant tools are exposed.", result.system_prompt)

    def test_fallback_uses_the_named_core_tools_in_order(self):
        result = pr.build_dynamic_prompt(
            "tell me a joke", example_tools(), count_tokens=words,
            max_tokens=2000,
            fallback_tools=("python_execute", "os_control"),
        )
        self.assertEqual(
            [tool.name for tool in result.tools],
            ["python_execute", "os_control"],
        )

    def test_fallback_names_missing_from_the_catalog_are_ignored(self):
        result = pr.build_dynamic_prompt(
            "tell me a joke", example_tools(), count_tokens=words,
            max_tokens=2000,
            fallback_tools=("os_control", "no_such_tool"),
        )
        self.assertEqual([tool.name for tool in result.tools], ["os_control"])

    def test_matched_requests_never_receive_the_fallback(self):
        result = pr.build_dynamic_prompt(
            "watch a movie", example_tools(), count_tokens=words,
            max_tokens=2000,
            fallback_tools=("python_execute", "os_control"),
        )
        self.assertEqual(
            [tool.name for tool in result.tools],
            ["stremio_play", "os_control"],
        )
        self.assertNotIn(pr.RULES["python"], result.system_prompt)


class ReactLoopClassificationTests(unittest.TestCase):
    def test_complex_requests_require_the_react_loop(self):
        for request in (
            "install the ADB driver",
            "debloat my phone",
            "reinstall Chrome",
            "build a localhost server",
            "building a local server",
            "fix the script",
            "automate my morning routine",
            "multi-step setup",
            "use adb to uninstall the app",
            "control my desktop",
            "open notepad and type hello",
            "search the web and summarize the results",
            "create a website",
            "set up a server",
            "get Discord installed",
            "remove the bloatware",
            "setup my new laptop",
            "update my drivers",
            "open notepad, type hello",
            "turn off notifications and enable dark mode",
            "Can you install Python 3.12?",
        ):
            self.assertTrue(pr.requires_react_loop(request), request)

    def test_conversational_and_single_tool_requests_stay_fast(self):
        for request in (
            "",
            "hello",
            "what's the weather",
            "what time is it",
            "tell me a joke",
            "open notepad",
            "launch Spotify",
            "take a screenshot",
            "play a movie",
            "set volume to 30",
            "move this file",
            "read this file",
            "send a message to Sam",
            "Should I install Python 3.12?",
            "what is a build",
            "explain the fix",
            "dont install anything",
            "build a playlist",
            "start the app",
            "how do I fix the build",
            "is adb installed",
        ):
            self.assertFalse(pr.requires_react_loop(request), request)

    def test_complexity_wins_over_conversational_words(self):
        self.assertTrue(pr.requires_react_loop("fix the weather app"))
        self.assertTrue(pr.requires_react_loop("install the news app"))


if __name__ == "__main__":
    unittest.main()


class AgentRoutingTests(unittest.TestCase):
    """Live AgentBot prompt/allowlist behavior for the two named requests."""

    def setUp(self):
        import agent
        self.agent = agent
        agent._clear_turn_route()
        patcher = mock.patch.object(agent, "GROUP_MODE", False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(agent._clear_turn_route)

    def test_watch_a_movie_prompt_uses_stremio_and_os_rules(self):
        prompt = self.agent._build_plan_prompt("watch a movie", "")
        self.assertIn(pr.RULES["stremio"], prompt)
        self.assertIn(pr.RULES["os"], prompt)
        self.assertNotIn(pr.RULES["python"], prompt)
        for name in ("play_macro", "open_app", "screenshot_background",
                     "analyze_screenshot", "click_coordinates", "load_skill"):
            self.assertIn(name, prompt)
        self.assertIn(
            '{"tool": "tool_name", "input": "tool_input"}', prompt)
        self.assertNotIn("Use native tool calls", prompt)

    def test_edit_a_file_prompt_uses_python_rules(self):
        prompt = self.agent._build_plan_prompt("edit a file", "")
        self.assertIn(pr.RULES["python"], prompt)
        self.assertNotIn(pr.RULES["stremio"], prompt)
        for name in ("read_file", "write_file", "patch_file",
                     "edit_docx_safe", "load_skill"):
            self.assertIn(name, prompt)
        self.assertLess(len(prompt) / 4, 7800)

    def test_non_stremio_skill_is_not_classified_as_stremio(self):
        with mock.patch.object(self.agent, "_skill_directive",
                               return_value="Use the demo skill now."):
            self.assertEqual(
                self.agent._classify_unmatched_intents("use demo skill"), [])
        with mock.patch.object(self.agent, "_skill_directive",
                               return_value="Use the Stremio skill now."):
            self.assertEqual(
                self.agent._classify_unmatched_intents("watch Cherry"),
                ["stremio"])

    def test_unmatched_request_keeps_core_os_and_python_tools(self):
        prompt = self.agent._build_plan_prompt("tell me a joke", "")
        for name in ("open_app", "type_text", "execute_python",
                     "read_file", "write_file", "load_skill"):
            self.assertIn(name, prompt)
        self.assertIn(pr.RULES["os"], prompt)
        self.assertIn(pr.RULES["python"], prompt)
        self.assertNotIn("No relevant tools are exposed.", prompt)

    def test_docker_and_terminal_requests_keep_core_tools(self):
        for request in ("start a Docker server", "type in the terminal"):
            self.agent._clear_turn_route()
            prompt = self.agent._build_plan_prompt(request, "")
            self.assertIn("open_app", prompt, request)
            self.assertIn("execute_python", prompt, request)
            self.assertIn(pr.RULES["os"], prompt, request)
            self.assertIn(pr.RULES["python"], prompt, request)
            self.assertNotIn(pr.RULES["stremio"], prompt, request)

    def test_keyword_only_request_does_not_leak_stremio_rules(self):
        self.agent._clear_turn_route()
        prompt = self.agent._build_plan_prompt("look at my screen", "")
        self.assertIn(pr.RULES["os"], prompt)
        self.assertIn(pr.RULES["python"], prompt)
        self.assertNotIn(pr.RULES["stremio"], prompt)

    def test_turn_allowlist_blocks_unrelated_tools(self):
        self.agent._set_turn_route("watch a movie")
        allowed = self.agent._ROUTED.allowed
        self.assertNotIn("read_file", allowed)
        action, error = self.agent.parse_action(
            '{"tool": "read_file", "input": "notes.txt"}')
        self.assertIsNone(action)
        self.assertIn("not available this turn", error)
        self.assertTrue(
            self.agent._execute_tool_impl(
                {"tool": "read_file", "input": "notes.txt"}
            ).startswith("ERROR: Tool 'read_file' is not available"))

    def test_group_mode_keeps_the_existing_tool_index(self):
        with mock.patch.object(self.agent, "GROUP_MODE", True):
            prompt = self.agent._build_plan_prompt("hello", "")
        self.assertIn("screenshot_background", prompt)
        self.assertIsNone(getattr(self.agent._ROUTED, "allowed", None))
