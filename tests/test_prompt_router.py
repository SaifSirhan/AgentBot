"""Rule and tool routing tests for prompt_router.

Run: python -m unittest tests.test_prompt_router -v
"""
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import prompt_catalog as pc
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
    def test_every_request_exposes_every_available_tool(self):
        tools = example_tools()
        for request in (
            "watch a movie",
            "edit a file",
            "tell me a joke",
            "do not use python_execute",
            "",
        ):
            with self.subTest(request=request):
                result = pr.build_dynamic_prompt(
                    request, tools, count_tokens=words, max_tokens=2000)
                self.assertEqual(result.tools, tuple(tools))
                for tool in tools:
                    self.assertIn(f"- {tool.name}:", result.system_prompt)

    def test_rules_follow_the_request_even_though_tools_do_not(self):
        result = pr.build_dynamic_prompt(
            "watch a movie", example_tools(), count_tokens=words, max_tokens=2000)
        self.assertIn(pr.RULES["stremio"], result.system_prompt)
        self.assertIn(pr.RULES["os"], result.system_prompt)
        self.assertNotIn(pr.RULES["python"], result.system_prompt)
        self.assertEqual(
            {tool.name for tool in result.tools},
            {tool.name for tool in example_tools()},
        )

    def test_python_rules_do_not_hide_the_other_tools(self):
        result = pr.build_dynamic_prompt(
            "edit a file", example_tools(), count_tokens=words, max_tokens=2000)
        self.assertIn(pr.RULES["python"], result.system_prompt)
        self.assertNotIn(pr.RULES["stremio"], result.system_prompt)
        self.assertEqual(len(result.tools), len(example_tools()))

    def test_unmatched_request_uses_the_default_rule_groups(self):
        result = pr.build_dynamic_prompt(
            "tell me a joke", example_tools(), count_tokens=words, max_tokens=2000)
        self.assertIn(pr.RULES["os"], result.system_prompt)
        self.assertIn(pr.RULES["python"], result.system_prompt)
        self.assertNotIn(pr.RULES["stremio"], result.system_prompt)
        self.assertEqual(len(result.tools), len(example_tools()))

    def test_research_rules_and_tools_are_not_starved_by_other_intents(self):
        tools = pc.build_tools(frozenset({
            "search_web", "execute_python", "open_app",
        }))
        result = pr.build_dynamic_prompt(
            "research the best laptops and write a docx report",
            tools, count_tokens=words, max_tokens=5000)
        self.assertIn(pr.RULES["research"], result.system_prompt)
        self.assertIn("search_web", {tool.name for tool in result.tools})
        self.assertIn("execute_python", {tool.name for tool in result.tools})

    def test_music_rules_and_tools_are_exposed_together(self):
        tools = pc.build_tools(frozenset({
            "youtube_search", "play_on_youtube", "media_control", "open_app",
        }))
        for request in ("find songs from Frank Ocean", "play some music"):
            with self.subTest(request=request):
                result = pr.build_dynamic_prompt(
                    request, tools, count_tokens=words, max_tokens=5000)
                self.assertIn(pr.RULES["music"], result.system_prompt)
                self.assertEqual(len(result.tools), len(tools))
        pause = pr.build_dynamic_prompt(
            "pause the music", tools, count_tokens=words, max_tokens=5000)
        self.assertIn("media_control", {tool.name for tool in pause.tools})

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
        self.assertEqual(len(classified.tools), len(example_tools()))

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

    def test_budget_raises_instead_of_hiding_tools(self):
        tools = [
            pr.Tool(f"file_tool_{i}", "Edit file. " * 50, ("python",))
            for i in range(2)
        ]
        with self.assertRaisesRegex(ValueError, "(?i)toolset.*budget"):
            pr.build_dynamic_prompt(
                "edit a file", tools, count_tokens=len,
                max_tokens=100, reserve_tokens=0)

    def test_budget_guard_keeps_the_complete_catalog(self):
        tools = [pr.Tool(f"file_tool_{i}", "Edit file.", ("python",))
                 for i in range(8)]
        result = pr.build_dynamic_prompt(
            "edit a file", tools, count_tokens=len,
            max_tokens=10_000, reserve_tokens=0)
        self.assertEqual(len(result.tools), 8)
        self.assertLessEqual(result.token_count, 10_000)

    def test_registry_descriptions_are_used_for_every_tool(self):
        class Registry:
            def describe(self, name):
                return f"registry description for {name}"

        result = pr.build_dynamic_prompt(
            "tell me a joke", example_tools(), count_tokens=words,
            max_tokens=5000, tool_registry=Registry())
        for tool in example_tools():
            self.assertIn(
                f"- {tool.name}: registry description for {tool.name}",
                result.system_prompt)

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

    def test_core_rules_do_not_ask_the_model_to_explain_gaps(self):
        """The inventory ban itself lives in agent.CORE_RULES, which reaches
        every prompt variant; this one copy keeps the routed rules short."""
        self.assertNotIn("otherwise explain the limitation", pr.CORE_RULES)

    def test_empty_catalog_does_not_ask_to_clarify_missing_capabilities(self):
        result = pr.build_dynamic_prompt(
            "Do the task.", [], count_tokens=words, max_tokens=5000)
        self.assertIn("No relevant tools are exposed.", result.system_prompt)
        self.assertNotIn("otherwise ask for clarification", result.system_prompt)


class ExplicitToolRequestTests(unittest.TestCase):
    NAMES = ("notify", "weather", "remember", "watch_system_condition")

    def test_prose_that_collides_with_a_tool_name_is_not_a_request(self):
        for request in (
            "notify me when RAM reaches 80%",
            "remember that I prefer evening runs",
            "check the weather tomorrow",
        ):
            with self.subTest(request=request):
                self.assertEqual(
                    pr.explicitly_requested_tool_names(request, self.NAMES), ())

    def test_use_verbs_make_a_tool_required(self):
        cases = (
            ("use notify when the build finishes", ("notify",)),
            ("call the notify tool once the download ends", ("notify",)),
            ("tell me the weather; use weather for it", ("weather",)),
            ("Use `notify` when done", ("notify",)),
        )
        for request, expected in cases:
            with self.subTest(request=request):
                self.assertEqual(
                    pr.explicitly_requested_tool_names(request, self.NAMES), expected)

    def test_snake_case_names_are_unambiguous_identifiers(self):
        self.assertEqual(
            pr.explicitly_requested_tool_names(
                "watch_system_condition for ram at 80", self.NAMES),
            ("watch_system_condition",))

    def test_avoided_tools_stay_unrequired(self):
        self.assertEqual(
            pr.explicitly_requested_tool_names(
                "do not use notify; use weather instead", self.NAMES), ("weather",))


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


class WeatherRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tools = pc.build_tools(frozenset({
            "weather", "air_quality", "open_app", "read_file",
            "search_web",
        }))

    def route(self, request):
        return pr.build_dynamic_prompt(
            request, self.tools, count_tokens=words, max_tokens=2000)

    def test_weather_questions_inject_weather_rules(self):
        for request in (
            "will it rain tomorrow",
            "what's the temperature in Paris",
            "give me the forecast for Friday",
            "is it hot outside",
            "how cold will it be tonight",
        ):
            with self.subTest(request=request):
                result = self.route(request)
                self.assertIn(pr.RULES["weather"], result.system_prompt, request)
                self.assertEqual(len(result.tools), len(self.tools))

    def test_air_quality_questions_inject_air_quality_rules(self):
        for request in (
            "what is the air quality in Berlin",
            "show me the aqi for today",
            "is there smog outside",
        ):
            with self.subTest(request=request):
                result = self.route(request)
                self.assertIn(
                    pr.RULES["air_quality"], result.system_prompt, request)
                self.assertEqual(len(result.tools), len(self.tools))


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
        self.assertLess(len(prompt) / 4, self.agent.MAX_PROMPT_TOKENS)

    def test_artist_song_prompt_exposes_music_tools(self):
        prompt = self.agent._build_plan_prompt(
            "find songs from Frank Ocean", "")
        self.assertIn("youtube_search", prompt)
        self.assertIn("play_on_youtube", prompt)
        self.assertIn(pr.RULES["music"], prompt)
        self.assertNotIn(pr.RULES["stremio"], prompt)

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

    def test_weather_request_exposes_the_weather_tool(self):
        self.agent._clear_turn_route()
        prompt = self.agent._build_plan_prompt("will it rain tomorrow", "")
        self.assertIn("- weather:", prompt)
        self.assertIn(pr.RULES["weather"], prompt)

    def test_turn_allowlist_exposes_every_catalog_tool(self):
        self.agent._set_turn_route("watch a movie")
        allowed = self.agent._ROUTED.allowed
        self.assertEqual(allowed, set(self.agent.VALID_TOOLS))
        self.assertIn("read_file", allowed)
        self.assertIn("search_web", allowed)
        action, error = self.agent.parse_action(
            '{"tool": "read_file", "input": "notes.txt"}')
        self.assertIsNone(error)
        self.assertEqual(action["tool"], "read_file")

    def test_core_rules_ban_tool_meta_commentary(self):
        prompt = self.agent._build_plan_prompt("hello", "")
        self.assertIn("NEVER explain your tool availability to the user.", prompt)
        self.assertIn('Do not say "I don\'t have search_web this turn".', prompt)
        self.assertIn("Do NOT generate meta-commentary", prompt)

    def test_group_mode_keeps_the_existing_tool_index(self):
        with mock.patch.object(self.agent, "GROUP_MODE", True):
            prompt = self.agent._build_plan_prompt("hello", "")
        self.assertIn("screenshot_background", prompt)
        self.assertIsNone(getattr(self.agent._ROUTED, "allowed", None))


if __name__ == "__main__":
    unittest.main()
