"""Agent registry tests for the OS control tools.

Run: python -m pytest tests/test_os_tools_integration.py -v
Or:  python tests/test_os_tools_integration.py
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

# Keep tool-result mood writes out of the user's real AgentMemory.
os.environ.setdefault(
    "AGENTBOT_MOOD_FILE",
    os.path.join(tempfile.gettempdir(), "agentbot_test_mood.json"))
try:
    os.remove(os.environ["AGENTBOT_MOOD_FILE"])
except OSError:
    pass

import agent

OS_TOOLS = (
    "screenshot_background",
    "analyze_screenshot",
    "click_coordinates",
    "type_text",
    "get_pixel_color",
)


class OSToolRegistryTests(unittest.TestCase):
    def test_tools_registered_once(self):
        manual = agent.load_skill_tool("core_tools")
        for name in OS_TOOLS:
            self.assertIn(name, agent.VALID_TOOLS, name)
            self.assertIn(name, agent.TOOL_DESCRIPTIONS, name)
            self.assertNotIn(name, agent.SAFE_GROUP_TOOLS, name)
            pattern = f"{name}(input)"
            self.assertEqual(manual.count(pattern), 1, pattern)

    def test_execute_tool_dispatches(self):
        with mock.patch.object(agent, "screenshot_background",
                               return_value="SHOT") as shot, \
             mock.patch.object(agent, "analyze_screenshot",
                               return_value="VISION") as analyze, \
             mock.patch.object(agent, "click_coordinates",
                               return_value="CLICK") as click, \
             mock.patch.object(agent, "type_text",
                               return_value="TYPE") as typed, \
             mock.patch.object(agent, "get_pixel_color",
                               return_value="COLOR") as color:
            self.assertEqual(agent.execute_tool(
                {"tool": "screenshot_background", "input": "Stremio"}), "SHOT")
            self.assertEqual(agent.execute_tool(
                {"tool": "analyze_screenshot", "input": "Where is Search?"}),
                "VISION")
            self.assertEqual(agent.execute_tool(
                {"tool": "click_coordinates", "input": "10,20"}), "CLICK")
            self.assertEqual(agent.execute_tool(
                {"tool": "type_text", "input": "hello"}), "TYPE")
            self.assertEqual(agent.execute_tool(
                {"tool": "get_pixel_color", "input": "10,20"}), "COLOR")

        shot.assert_called_once_with("Stremio")
        analyze.assert_called_once_with("Where is Search?")
        click.assert_called_once_with("10,20")
        typed.assert_called_once_with("hello")
        color.assert_called_once_with("10,20")

    def test_group_mode_blocks_all_os_tools(self):
        with mock.patch.object(agent, "GROUP_MODE", True):
            prompt = agent._build_plan_prompt("hello", "")
            self.assertIn("screenshot_background", prompt)
            for name in OS_TOOLS:
                out = agent.execute_tool(
                    {"tool": name,
                     "input": "1,1" if name != "screenshot_background" else ""})
                self.assertIn("disabled in group chats", out, name)

    def test_prompt_requires_autonomous_execution(self):
        text = agent.load_skill_tool("core_tools")
        flat = " ".join(text.split())
        self.assertIn(
            "You are an autonomous agent. When the user asks you to perform a "
            "multi-step task (like controlling an app), you MUST execute the "
            "full sequence of steps (screenshot, click, type, screenshot, "
            "click) without stopping to ask for permission. Only ask for "
            "permission before deleting files or sending money.",
            flat,
        )
        self.assertIn("Stop, take a fresh screenshot, and look again.", flat)
        self.assertIn('"x,y | App title"', text)
        self.assertIn('"{enter}" to submit', text)
        self.assertIn(
            "You have a vision model. Use analyze_screenshot to read the screen.",
            flat)
        self.assertIn("Never ask the user for coordinates.", flat)

    def test_pre_action_rubric_precedes_autonomy_rules(self):
        flat = " ".join(agent.load_skill_tool("core_tools").split())
        rubric = (
            "Before executing a click or file modification, ask yourself: "
            "1. Is this the exact app window I want? 2. Is the target "
            "clearly visible? 3. Did the previous click actually do "
            "something? If the answer to any is NO, take a fresh screenshot "
            "before acting.")
        self.assertIn(rubric, flat)
        self.assertLess(flat.index("PRE-ACTION RUBRIC"), flat.index(rubric))
        self.assertLess(
            flat.index(rubric), flat.index("You are an autonomous agent."))

    def test_screenshots_can_repeat_but_clicks_cannot(self):
        shot = ("screenshot_background", "Stremio")
        self.assertFalse(
            agent._is_repeat_action("screenshot_background", "Stremio", shot))
        click = ("click_coordinates", "10,20")
        self.assertTrue(
            agent._is_repeat_action("click_coordinates", "10,20", click))
        self.assertFalse(
            agent._is_repeat_action("click_coordinates", "30,40", click))

    def test_stream_media_is_constrained_to_urls_and_music(self):
        text = " ".join(agent.load_skill_tool("core_tools").split())
        self.assertIn(
            "ONLY use stream_media for YouTube videos, music, or direct video "
            "URLs. NEVER use stream_media to search for full movies or TV "
            "shows.",
            text,
        )

    def test_prompt_budget(self):
        prompt = agent._build_plan_prompt("hello", "")
        self.assertLess(
            len(prompt) / 4, 7800,
            f"prompt is {len(prompt)} chars (~{len(prompt) / 4:.0f} tokens)",
        )

    def test_os_control_import_is_lazy(self):
        # The tool wrappers import os_control on use; agent itself must not
        # require PyAutoGUI/MSS just to build the prompt.
        self.assertFalse(hasattr(agent, "os_control"))


class FakeOSControl:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def screenshot_background(self, title=""):
        self.calls.append(title)
        return self.result


SHOT_OK = (
    "Screenshot saved: C:\\tmp\\screen.png\n"
    "Image: 800x600 pixels (primary monitor at 0,0)\n"
    "DPI: 100% scale, per-monitor-aware\n"
    "GUI: hidden\n"
    "Focus: focused Stremio\n"
    "Coordinates for click_coordinates/get_pixel_color are pixels "
    "from this image's top-left."
)


class ScreenshotVisionTests(unittest.TestCase):
    def test_screenshot_focuses_app_and_auto_describes(self):
        fake = FakeOSControl(SHOT_OK)
        with mock.patch.object(agent, "_os_control_module",
                               return_value=fake), \
             mock.patch.object(agent, "describe_image",
                               return_value="Search - 400,40") as vision:
            out = agent.screenshot_background("Stremio")

        self.assertEqual(fake.calls, ["Stremio"])
        self.assertIn("Search - 400,40", out)
        prompt = vision.call_args.args[0]
        self.assertTrue(prompt.startswith("C:\\tmp\\screen.png|"), prompt)
        self.assertIn("800x600", prompt)

    def test_screenshot_accepts_title_before_pipe(self):
        fake = FakeOSControl(SHOT_OK)
        with mock.patch.object(agent, "_os_control_module",
                               return_value=fake), \
             mock.patch.object(agent, "describe_image",
                               return_value="Search - 400,40"):
            agent.screenshot_background("Stremio|where is Search?")

        self.assertEqual(fake.calls, ["Stremio"])

    def test_analyze_screenshot_parses_title_and_question(self):
        fake = FakeOSControl(SHOT_OK)
        with mock.patch.object(agent, "_os_control_module",
                               return_value=fake), \
             mock.patch.object(agent, "describe_image",
                               return_value="Search - 400,40") as vision:
            agent.analyze_screenshot("Stremio|Where is the search bar?")

        self.assertEqual(fake.calls, ["Stremio"])
        prompt = vision.call_args.args[0]
        self.assertIn("Where is the search bar?", prompt)
        self.assertIn(agent.VISION_COORDINATE_RULE, prompt)
        self.assertIn("800x600", prompt)

    def test_vision_failure_is_reported_without_coordinates(self):
        fake = FakeOSControl(SHOT_OK)
        with mock.patch.object(agent, "_os_control_module",
                               return_value=fake), \
             mock.patch.object(agent, "describe_image",
                               return_value="ERROR: no vision key"):
            out = agent.screenshot_background()

        self.assertIn("VISION ERROR: no vision key", out)
        self.assertIn("do not invent coordinates", out)
        self.assertIn("C:\\tmp\\screen.png", out)

    def test_failed_capture_skips_vision(self):
        fake = FakeOSControl("ABORTED: kill switch is latched")
        with mock.patch.object(agent, "_os_control_module",
                               return_value=fake), \
             mock.patch.object(agent, "describe_image") as vision:
            out = agent.screenshot_background("Stremio")

        self.assertEqual(out, "ABORTED: kill switch is latched")
        vision.assert_not_called()


class DescribeImageBudgetTests(unittest.TestCase):
    def test_vision_request_uses_8192_tokens(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            fh.write(b"fake-png")
            path = fh.name
        self.addCleanup(os.remove, path)

        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": "found it"}}]}
        with mock.patch.object(agent, "DEEPSEEK_API_KEY", "test"), \
             mock.patch.object(agent.requests, "post",
                               return_value=response) as post:
            out = agent.describe_image(f"{path}|Where is the button?")

        self.assertEqual(out, "found it")
        self.assertEqual(
            post.call_args.kwargs["json"]["max_tokens"], 8192)


if __name__ == "__main__":
    unittest.main(verbosity=2)
