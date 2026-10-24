"""Agent-level integration tests for the Section 4 browser tool.

Run: python -m pytest tests/test_browser_integration.py -v
Or:  python tests/test_browser_integration.py
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent
import browser_agent as b

MAX_PROMPT_TOKENS = 7800


class BrowserToolIntegrationTests(unittest.TestCase):
    def test_registry_has_one_browse_web_entry(self):
        self.assertIn("browse_web", agent.VALID_TOOLS)
        self.assertEqual(agent.TOOL_DESCRIPTIONS.count("browse_web(input)"), 1)

        i_load = agent.TOOL_DESCRIPTIONS.index("load_skill(input)")
        i_browse = agent.TOOL_DESCRIPTIONS.index("browse_web(input)")
        i_examples = agent.TOOL_DESCRIPTIONS.index("10. EXAMPLES")
        self.assertLess(i_load, i_browse)
        self.assertLess(i_browse, i_examples)

    def test_prompt_budget_after_tool_addition(self):
        prompt = agent._build_plan_prompt("hello", "")
        estimated = len(prompt) / 4
        self.assertLess(
            estimated, MAX_PROMPT_TOKENS,
            f"prompt is {len(prompt)} chars (~{estimated:.0f} tokens)",
        )

    def test_group_mode_blocks_browse_web(self):
        with mock.patch.object(agent, "GROUP_MODE", True):
            prompt = agent._build_plan_prompt("hello", "")
            self.assertIn(
                "browse_web (browser control is disabled in groups)", prompt)
            blocked = agent.execute_tool(
                {"tool": "browse_web", "input": "https://example.com | do it"})
            self.assertIn("disabled in group chats", blocked)

    def test_usage_errors(self):
        self.assertIn("format", agent.browse_web("https://example.com"))
        self.assertIn("both", agent.browse_web(" | do it"))
        self.assertIn("both", agent.browse_web("https://example.com | "))

    def test_disabled_config_reports_clearly(self):
        with mock.patch.object(
            b, "load_settings", return_value=b.BrowserSettings(enabled=False)
        ):
            result = agent.browse_web("https://example.com | do it")
        self.assertTrue(result.startswith("ERROR:"), result)
        self.assertIn("disabled", result)

    def test_delegates_to_browser_agent(self):
        settings = b.BrowserSettings(
            enabled=True,
            allowed_origins=frozenset({("https", "example.com", 443)}),
        )
        report = {
            "status": "completed",
            "summary": "Signed in",
            "final_url": "https://example.com/home",
            "steps": 3,
            "evidence": "The page shows the account menu.",
            "actions": ["Step 1: click -> executed"],
            "error": "",
        }
        with mock.patch.object(b, "load_settings", return_value=settings), \
             mock.patch.object(b, "run_task_sync", return_value=report) as run:
            result = agent.browse_web("https://example.com | sign in")

        self.assertIn("BROWSER: completed", result)
        self.assertIn("Signed in", result)
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(args, ("https://example.com", "sign in"))
        self.assertIs(kwargs["settings"], settings)
        self.assertTrue(callable(kwargs["planner"]))

    def test_errors_are_surfaced_not_raised(self):
        with mock.patch.object(
            b, "load_settings",
            side_effect=b.BrowserConfigError("bad allowlist"),
        ):
            result = agent.browse_web("https://example.com | do it")
        self.assertTrue(result.startswith("ERROR:"), result)
        self.assertIn("bad allowlist", result)

    def test_vision_defaults_off_in_config(self):
        import config
        self.assertIs(config.DEFAULTS["BROWSER_ENABLED"], True)
        self.assertIs(config.DEFAULTS["BROWSER_VISION_ENABLED"], False)
        self.assertEqual(
            config.DEFAULTS["BROWSER_ALLOWED_ORIGINS"],
            "https://example.com,https://quotes.toscrape.com")
        self.assertEqual(config.DEFAULTS["BROWSER_MAX_STEPS"], 15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
