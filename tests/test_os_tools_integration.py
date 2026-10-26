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
    "click_coordinates",
    "type_text",
    "get_pixel_color",
)


class OSToolRegistryTests(unittest.TestCase):
    def test_tools_registered_once(self):
        for name in OS_TOOLS:
            self.assertIn(name, agent.VALID_TOOLS, name)
            self.assertNotIn(name, agent.SAFE_GROUP_TOOLS, name)
            expected = f"{name}("
            # screenshot_background has no argument, the rest take input.
            pattern = name + ("()" if name == "screenshot_background" else "(input)")
            self.assertEqual(agent.TOOL_DESCRIPTIONS.count(pattern), 1, pattern)

    def test_execute_tool_dispatches(self):
        with mock.patch.object(agent, "screenshot_background",
                               return_value="SHOT") as shot, \
             mock.patch.object(agent, "click_coordinates",
                               return_value="CLICK") as click, \
             mock.patch.object(agent, "type_text",
                               return_value="TYPE") as typed, \
             mock.patch.object(agent, "get_pixel_color",
                               return_value="COLOR") as color:
            self.assertEqual(agent.execute_tool(
                {"tool": "screenshot_background", "input": ""}), "SHOT")
            self.assertEqual(agent.execute_tool(
                {"tool": "click_coordinates", "input": "10,20"}), "CLICK")
            self.assertEqual(agent.execute_tool(
                {"tool": "type_text", "input": "hello"}), "TYPE")
            self.assertEqual(agent.execute_tool(
                {"tool": "get_pixel_color", "input": "10,20"}), "COLOR")

        shot.assert_called_once_with()
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
        self.assertIn("run the full sequence", text)
        self.assertIn("step-by-step asks", text)
        self.assertIn("destructive actions", text)
        self.assertIn('"x,y | App title"', text)
        self.assertIn('"{enter}" to submit', text)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
