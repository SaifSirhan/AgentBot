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

    def test_named_search_and_python_survive_docx_tool_ranking(self):
        tools = [
            pr.Tool(f"document_tool_{i}", "Document helper.", ("python",),
                    ("docx", "document", "write"))
            for i in range(7)
        ] + [
            pr.Tool("search_web", "Search the web.", (), ("search", "web")),
            pr.Tool("execute_python", "Execute Python.", ("python",),
                    ("python", "file")),
        ]
        result = pr.build_dynamic_prompt(
            "Use search_web to research sources, then use execute_python "
            "to write a DOCX document.",
            tools, count_tokens=words, max_tokens=5000,
        )
        names = {tool.name for tool in result.tools}
        self.assertTrue({"search_web", "execute_python"} <= names)
        self.assertIn("- search_web:", result.system_prompt)
        self.assertIn("- execute_python:", result.system_prompt)
        self.assertIn(pr.RULES["python"], result.system_prompt)
        self.assertNotIn(pr.RULES["stremio"], result.system_prompt)
        self.assertEqual(len(result.tools), 5)

    def test_more_than_five_explicit_names_are_all_exposed(self):
        tools = [pr.Tool(f"tool_{i}", "Requested helper.", ()) for i in range(7)]
        result = pr.build_dynamic_prompt(
            "Use tool_0, tool_1, tool_2, tool_3, tool_4, tool_5 and tool_6.",
            tools, count_tokens=words, max_tokens=5000,
        )
        self.assertEqual({tool.name for tool in result.tools},
                         {f"tool_{i}" for i in range(7)})
        for tool in tools:
            self.assertIn(f"- {tool.name}:", result.system_prompt)

    def test_explicit_tool_cannot_be_silently_dropped_for_budget(self):
        with self.assertRaisesRegex(ValueError, "(?i)mandatory.*budget"):
            pr.build_dynamic_prompt(
                "Use search_web.",
                [pr.Tool("search_web", "Search details. " * 200, ())],
                count_tokens=len, max_tokens=1500, reserve_tokens=0,
            )

    def test_budget_removes_optional_tools_not_explicit_names(self):
        tools = [
            pr.Tool(f"helper_{i}", "Optional details. " * 100, (), ("use",))
            for i in range(6)
        ] + [pr.Tool("search_web", "Search the web.", ())]
        result = pr.build_dynamic_prompt(
            "Use search_web.", tools, count_tokens=len,
            max_tokens=1500, reserve_tokens=0,
        )
        self.assertEqual([tool.name for tool in result.tools], ["search_web"])
        self.assertIn("- search_web:", result.system_prompt)
        self.assertLessEqual(result.token_count, 1500)

    def test_document_writes_pin_registered_python(self):
        tools = [
            pr.Tool(f"a_document_{i}", "Document helper.", ("python",),
                    ("docx", "document"))
            for i in range(7)
        ] + [pr.Tool("execute_python", "Execute Python.", ("python",),
                     ("python", "file"))]
        for request in (
            "Create report.docx with the results.",
            "Write a Word document with the findings.",
            "Generate a DOCX summary.",
            "Save the results as a document.",
        ):
            with self.subTest(request=request):
                result = pr.build_dynamic_prompt(
                    request, tools, count_tokens=words, max_tokens=5000)
                self.assertIn("execute_python", [tool.name for tool in result.tools])
                self.assertIn("- execute_python:", result.system_prompt)
                self.assertIn(pr.RULES["python"], result.system_prompt)
                self.assertNotIn(pr.RULES["stremio"], result.system_prompt)

    def test_document_write_does_not_invent_unregistered_python(self):
        result = pr.build_dynamic_prompt(
            "Create a DOCX document.",
            [pr.Tool("write_document", "Write a document.", ("python",))],
            count_tokens=words, max_tokens=5000,
        )
        self.assertEqual([tool.name for tool in result.tools], ["write_document"])
        self.assertNotIn("- execute_python:", result.system_prompt)

    def test_implicit_web_research_survives_document_tool_ranking(self):
        tools = [
            pr.Tool(f"document_tool_{i}", "Document helper.", ("python",),
                    ("docx", "document", "write"))
            for i in range(7)
        ] + [
            pr.Tool("search_web", "Search the web.", (), ("search", "web")),
            pr.Tool("execute_python", "Execute Python.", ("python",)),
        ]
        for request in (
            "Search the web for UM parties then write a docx.",
            "Research Universiti Malaya student parties and write a document.",
        ):
            with self.subTest(request=request):
                result = pr.build_dynamic_prompt(
                    request, tools, count_tokens=words, max_tokens=5000)
                self.assertTrue({"search_web", "execute_python"} <=
                                {tool.name for tool in result.tools})

    def test_document_searches_do_not_grant_python_tools(self):
        tools = [
            pr.Tool("search_web", "Search the web.", (),
                    ("search", "web", "docx", "document")),
            pr.Tool("execute_python", "Execute Python.", ("python",),
                    ("python", "file")),
        ]
        for request in (
            "Search the web for DOCX templates.",
            "Search for document editors.",
            "Use search_web to find DOCX documentation.",
        ):
            with self.subTest(request=request):
                result = pr.build_dynamic_prompt(
                    request, tools, count_tokens=words, max_tokens=5000)
                self.assertEqual([tool.name for tool in result.tools], ["search_web"])
                self.assertNotIn(pr.RULES["stremio"], result.system_prompt)

    def test_implicit_intents_survive_ranking_cap_and_budget(self):
        tools = [
            pr.Tool("a_desktop", "Inspect desktop.", ("os",),
                    ("screenshot", "desktop")),
        ] + [
            pr.Tool(f"b_desktop_{i}", "Desktop details. " * 100, ("os",),
                    ("screenshot", "desktop"))
            for i in range(6)
        ] + [pr.Tool("execute_python", "Edit files.", ("python",), ("file",))]
        for budget in (10_000, 1500):
            with self.subTest(budget=budget):
                result = pr.build_dynamic_prompt(
                    "Take a screenshot of the desktop and edit a file.",
                    tools, count_tokens=len, max_tokens=budget, reserve_tokens=0)
                self.assertTrue({"a_desktop", "execute_python"} <=
                                {tool.name for tool in result.tools})
                self.assertIn("- execute_python:", result.system_prompt)
                self.assertIn(pr.RULES["os"], result.system_prompt)
                self.assertIn(pr.RULES["python"], result.system_prompt)
                self.assertNotIn(pr.RULES["stremio"], result.system_prompt)
                self.assertLessEqual(len(result.tools), 5)
                self.assertLessEqual(result.token_count, budget)

    def test_budget_cannot_drop_implicit_intent_coverage(self):
        tools = [
            pr.Tool("desktop", "Inspect. " * 90, ("os",), ("desktop",)),
            pr.Tool("files", "Edit. " * 150, ("python",), ("file",)),
        ]
        with self.assertRaisesRegex(ValueError, "(?i)mandatory.*budget"):
            pr.build_dynamic_prompt(
                "Inspect the desktop and edit a file.", tools,
                count_tokens=len, max_tokens=2000, reserve_tokens=0)

    def test_stremio_dependency_survives_crowded_ranking_and_budget(self):
        tools = [pr.Tool("a_play", "Play movie.", ("stremio",), ("movie",))] + [
            pr.Tool(f"b_play_{i}", "Playback details. " * 100, ("stremio",),
                    ("movie",))
            for i in range(6)
        ] + [pr.Tool("os_control", "Control desktop.", ("os",))]
        for budget in (10_000, 1500):
            with self.subTest(budget=budget):
                result = pr.build_dynamic_prompt(
                    "Watch a movie.", tools, count_tokens=len,
                    max_tokens=budget, reserve_tokens=0)
                self.assertTrue({"a_play", "os_control"} <=
                                {tool.name for tool in result.tools})
                self.assertIn("- os_control:", result.system_prompt)
                self.assertIn(pr.RULES["stremio"], result.system_prompt)
                self.assertIn(pr.RULES["os"], result.system_prompt)
                self.assertNotIn(pr.RULES["python"], result.system_prompt)

    def test_classifier_multi_intent_coverage_is_retained(self):
        tools = [pr.Tool(f"a_os_{i}", "Desktop helper.", ("os",))
                 for i in range(6)] + [pr.Tool("z_files", "File helper.", ("python",))]
        result = pr.build_dynamic_prompt(
            "Do the task.", tools, count_tokens=words,
            classify_intents=lambda _request: ["os", "python"], max_tokens=5000)
        self.assertIn("z_files", [tool.name for tool in result.tools])
        self.assertIn("a_os_0", [tool.name for tool in result.tools])

    def test_explicit_shared_tool_does_not_leak_stremio_rules(self):
        result = pr.build_dynamic_prompt(
            "Use open_app to inspect my screen.",
            [pr.Tool("open_app", "Open an approved app.", ("os", "stremio"))],
            count_tokens=words, max_tokens=5000)
        self.assertEqual([tool.name for tool in result.tools], ["open_app"])
        self.assertNotIn(pr.RULES["stremio"], result.system_prompt)

    def test_only_positive_whole_tool_names_are_pinned(self):
        tools = [
            pr.Tool("search_web", "Search.", ()),
            pr.Tool("execute_python", "Compute.", ()),
        ]
        for request, expected in (
            ("Do not use search_web. Use execute_python.", ["execute_python"]),
            ("Never use search_web; use execute_python.", ["execute_python"]),
            ("Don't use search_web, but use execute_python.", ["execute_python"]),
            ("Use execute_python; do not use search_web.", ["execute_python"]),
            ("Never use search_web or execute_python.", []),
            ("Use search_web_extra instead.", []),
            ("Use SEARCH_WEB.", ["search_web"]),
        ):
            with self.subTest(request=request):
                result = pr.build_dynamic_prompt(
                    request, tools, count_tokens=words, max_tokens=5000)
                self.assertEqual([tool.name for tool in result.tools], expected)

    def test_explanations_and_avoided_tool_names_are_not_mandatory(self):
        tools = [pr.Tool("execute_python", "Compute.", ()),
                 pr.Tool("search_web", "Search.", ())]
        for request in (
            "Explain the quoted text 'execute_python'; use search_web for documentation.",
            "Explain execute_python; use search_web for documentation.",
            "Avoid execute_python; use search_web.",
        ):
            with self.subTest(request=request):
                result = pr.build_dynamic_prompt(
                    request, tools, count_tokens=words, max_tokens=5000)
                self.assertEqual([tool.name for tool in result.tools], ["search_web"])

    def test_explicit_backtick_tool_calls_remain_mandatory(self):
        tools = [pr.Tool("execute_python", "Compute.", ()),
                 pr.Tool("search_web", "Search.", ())]
        result = pr.build_dynamic_prompt(
            "Use `search_web` and `execute_python`.", tools,
            count_tokens=words, max_tokens=5000)
        self.assertEqual({tool.name for tool in result.tools},
                         {"search_web", "execute_python"})

    def test_capability_shortage_guidance_uses_request_tool(self):
        result = pr.build_dynamic_prompt(
            "Use request_tool to enable a missing capability.",
            [pr.Tool("request_tool", "Request another registered tool.", ())],
            count_tokens=words, max_tokens=5000)
        self.assertIn("- request_tool:", result.system_prompt)
        self.assertIn("request_tool when available", result.system_prompt)
        self.assertNotIn("request or available capabilities are insufficient",
                         result.system_prompt)
        self.assertIn("Ask for clarification when the request", result.system_prompt)
        self.assertIn("Tool access is not authorization", result.system_prompt)

    def test_empty_catalog_does_not_ask_to_clarify_missing_capabilities(self):
        result = pr.build_dynamic_prompt(
            "Do the task.", [], count_tokens=words, max_tokens=5000)
        self.assertIn("No relevant tools are exposed.", result.system_prompt)
        self.assertNotIn("otherwise ask for clarification", result.system_prompt)


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
