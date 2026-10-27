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
        for name in OS_TOOLS:
            self.assertIn(name, agent.VALID_TOOLS, name)
            self.assertNotIn(name, agent.SAFE_GROUP_TOOLS, name)
            pattern = f"{name}(input)"
            self.assertEqual(agent.TOOL_DESCRIPTIONS.count(pattern), 1, pattern)

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
            self.assertIn("OS control", prompt)
            for name in OS_TOOLS:
                out = agent.execute_tool(
                    {"tool": name,
                     "input": "1,1" if name != "screenshot_background" else ""})
                self.assertIn("disabled in group chats", out, name)

    def test_prompt_requires_full_os_sequence(self):
        text = agent.TOOL_DESCRIPTIONS
        flat = " ".join(text.split())
        self.assertIn("run the full sequence", text)
        self.assertIn("step-by-step asks", text)
        self.assertIn("destructive actions", text)
        self.assertIn('"x,y | App title"', text)
        self.assertIn('"{enter}" to submit', text)
        self.assertIn(
            "You have a vision model. Use analyze_screenshot to read the screen.",
            flat)
        self.assertIn("Never ask the user for coordinates.", flat)

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


class VisionCoordinateTests(unittest.TestCase):
    def test_parses_common_vision_formats(self):
        for reply in (
            "x=640, y=40",
            "(640, 40)",
            "640, 40",
            "The search bar is at 640, 40 in this 1280x720 image.",
        ):
            self.assertEqual(
                agent._vision_coordinates(reply, 1280, 720), (640, 40), reply)

    def test_rejects_none_and_out_of_bounds(self):
        self.assertIsNone(agent._vision_coordinates("NONE", 1280, 720))
        self.assertIsNone(agent._vision_coordinates("9999, 9999", 1280, 720))
        self.assertIsNone(agent._vision_coordinates("", 1280, 720))


class StremioSearchTests(unittest.TestCase):
    def test_opens_app_then_searches_and_clicks_first_result(self):
        with mock.patch.object(agent, "PYGETWINDOW_AVAILABLE", True), \
             mock.patch.object(agent, "gw") as gw, \
             mock.patch.object(agent, "open_app",
                               return_value="Opened Stremio.") as opened, \
             mock.patch.object(agent, "_stremio_locate", side_effect=[
                 ((100, 20), "search"), ((300, 200), "first")]) as locate, \
             mock.patch.object(agent, "click_coordinates",
                               return_value="Clicked") as click, \
             mock.patch.object(agent, "type_text",
                               return_value="Typed") as typed, \
             mock.patch.object(agent.time, "sleep") as sleep:
            gw.getWindowsWithTitle.side_effect = [[], [object()]]
            out = agent.stremio_search("Cherry")

        opened.assert_called_once_with("Stremio")
        self.assertEqual(
            click.call_args_list,
            [mock.call("100,20 | Stremio"), mock.call("300,200 | Stremio")])
        typed.assert_called_once_with("Cherry{enter}")
        sleep.assert_any_call(agent.STREMIO_RESULT_WAIT)
        self.assertEqual(locate.call_count, 2)
        self.assertIn("searched 'Cherry'", out)

    def test_existing_window_is_not_relaunched(self):
        with mock.patch.object(agent, "PYGETWINDOW_AVAILABLE", True), \
             mock.patch.object(agent, "gw") as gw, \
             mock.patch.object(agent, "open_app") as opened, \
             mock.patch.object(agent, "_stremio_locate", side_effect=[
                 ((100, 20), "search"), ((300, 200), "first")]), \
             mock.patch.object(agent, "click_coordinates",
                               return_value="Clicked"), \
             mock.patch.object(agent, "type_text", return_value="Typed"), \
             mock.patch.object(agent.time, "sleep"):
            gw.getWindowsWithTitle.return_value = [object()]
            agent.stremio_search("Cherry")

        opened.assert_not_called()

    def test_no_result_does_not_click_twice(self):
        with mock.patch.object(agent, "PYGETWINDOW_AVAILABLE", True), \
             mock.patch.object(agent, "gw") as gw, \
             mock.patch.object(agent, "_stremio_locate", side_effect=[
                 ((100, 20), "search"),
                 (None, "ERROR: vision saw no results")]), \
             mock.patch.object(agent, "click_coordinates",
                               return_value="Clicked") as click, \
             mock.patch.object(agent, "type_text", return_value="Typed"), \
             mock.patch.object(agent.time, "sleep"):
            gw.getWindowsWithTitle.return_value = [object()]
            out = agent.stremio_search("Cherry")

        self.assertTrue(out.startswith("ERROR: search for 'Cherry'"), out)
        self.assertEqual(click.call_count, 1)

    def test_missing_search_bar_stops_before_typing(self):
        with mock.patch.object(agent, "PYGETWINDOW_AVAILABLE", True), \
             mock.patch.object(agent, "gw") as gw, \
             mock.patch.object(agent, "_stremio_locate",
                               return_value=(None, "ERROR: no search bar")), \
             mock.patch.object(agent, "click_coordinates") as click, \
             mock.patch.object(agent, "type_text") as typed, \
             mock.patch.object(agent.time, "sleep"):
            gw.getWindowsWithTitle.return_value = [object()]
            out = agent.stremio_search("Cherry")

        self.assertEqual(out, "ERROR: no search bar")
        click.assert_not_called()
        typed.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
