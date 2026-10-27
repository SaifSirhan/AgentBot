"""Integration tests for the Section 3 skills engine in agent.py.

Covers the tool registry, group-mode blocking, the prompt catalog, the
prompt-size budget, and the reload/dirty path used by /reload-skills.

Run: python -m pytest tests/test_skills_integration.py -v
Or:  python tests/test_skills_integration.py
"""
import os
import shutil
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
import skill_manager as sm

MAX_PROMPT_TOKENS = 7800  # chars / 4 estimate, with headroom over the measured baseline


def write_skill(root, name, description, body):
    directory = Path(root) / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )


class SkillIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skills_int_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "skills"
        self.legacy = self.tmp / "legacy"
        self.plugins = self.tmp / "plugins"
        for path in (self.repo, self.legacy, self.plugins):
            path.mkdir(parents=True)
        write_skill(self.repo, "demo-skill",
                    "Demo description for catalog.",
                    "DEMO BODY LINE.")

    def manager(self):
        return sm.SkillManager(
            skills_dir=self.repo,
            plugins_dir=self.plugins,
            extra_skills_dirs=[self.legacy],
        )

    def patched(self, group_mode=False):
        mgr = self.manager()
        mgr.scan()
        return mock.patch.multiple(
            agent,
            _skill_manager=mgr,
            _skills_dirty=False,
            GROUP_MODE=group_mode,
        )

    # ------------------------------------------------------------------
    # Registry
    # ------------------------------------------------------------------
    def test_load_skill_is_registered_and_documented_once(self):
        self.assertIn("load_skill", agent.VALID_TOOLS)
        self.assertEqual(agent.TOOL_DESCRIPTIONS.count("load_skill(input)"), 1)

        i_manage = agent.TOOL_DESCRIPTIONS.index("skill_manage(input)")
        i_load = agent.TOOL_DESCRIPTIONS.index("load_skill(input)")
        i_examples = agent.TOOL_DESCRIPTIONS.index("10. EXAMPLES")
        self.assertLess(i_manage, i_load)
        self.assertLess(i_load, i_examples)

        between = agent.TOOL_DESCRIPTIONS[i_manage:i_load]
        self.assertIn("NEVER invent a skill body", between)
        self.assertNotIn("\n  run_command(input)", between)

    def test_execute_tool_loads_skill_and_reports_errors(self):
        with self.patched():
            content = agent.execute_tool(
                {"tool": "load_skill", "input": "demo-skill"})
            self.assertIn("Skill: local:demo-skill", content)
            self.assertIn("DEMO BODY LINE.", content)

            missing = agent.execute_tool(
                {"tool": "load_skill", "input": "no-such-skill"})
            self.assertTrue(missing.startswith("ERROR:"), missing)

            empty = agent.execute_tool({"tool": "load_skill", "input": ""})
            self.assertIn("needs a skill id", empty)

    # ------------------------------------------------------------------
    # Group mode
    # ------------------------------------------------------------------
    def test_group_mode_blocks_load_skill_and_omits_catalog(self):
        with self.patched(group_mode=True):
            prompt = agent._build_plan_prompt("hello", "")
            self.assertNotIn("AVAILABLE SKILLS", prompt)
            self.assertNotIn("local:demo-skill", prompt)
            self.assertIn("load_skill (skills are disabled in groups)", prompt)

            blocked = agent.execute_tool(
                {"tool": "load_skill", "input": "demo-skill"})
            self.assertIn("disabled in group chats", blocked)

    def test_private_prompt_includes_catalog(self):
        with self.patched(group_mode=False):
            prompt = agent._build_plan_prompt("hello", "")
            self.assertIn("AVAILABLE SKILLS", prompt)
            self.assertIn("local:demo-skill", prompt)
            self.assertIn("Demo description for catalog.", prompt)

    # ------------------------------------------------------------------
    # Prompt budget
    # ------------------------------------------------------------------
    def test_real_prompt_stays_under_token_budget(self):
        # Worst case: late-night directive plus the frustration directive, on
        # top of the real catalog. Mood and time of day otherwise make this
        # measurement vary between runs.
        with mock.patch.multiple(
            agent, _skill_manager=None, _skills_dirty=True, GROUP_MODE=False
        ), mock.patch.object(agent, "day_part", return_value="late"), \
                mock.patch.object(agent, "get_mood_directive",
                                  return_value=agent.FRUSTRATION_DIRECTIVE):
            prompt = agent._build_plan_prompt("hello", "")
            catalog = agent.get_skill_catalog()

        estimated_tokens = len(prompt) / 4
        self.assertLess(
            estimated_tokens, MAX_PROMPT_TOKENS,
            f"prompt is {len(prompt)} chars (~{estimated_tokens:.0f} tokens); "
            f"catalog block is {len(catalog)} chars",
        )
        self.assertLessEqual(len(catalog), sm.CATALOG_MAX_CHARS)

    def test_catalog_is_bounded_with_many_verbose_skills(self):
        for i in range(80):
            write_skill(self.repo, f"bulk-{i:03d}", "D" * 400, "body")
        mgr = self.manager()
        mgr.scan()
        with mock.patch.multiple(
            agent, _skill_manager=mgr, _skills_dirty=False, GROUP_MODE=False
        ):
            catalog = agent.get_skill_catalog()
            prompt = agent._build_plan_prompt("hello", "")

        self.assertLessEqual(len(catalog), sm.CATALOG_MAX_CHARS)
        entries = [l for l in catalog.splitlines() if l.startswith("- ")]
        self.assertLessEqual(len(entries), sm.CATALOG_MAX_ENTRIES + 1)  # +N note
        self.assertLess(len(prompt) / 4, MAX_PROMPT_TOKENS)

    # ------------------------------------------------------------------
    # skill_manage -> dirty -> catalog
    # ------------------------------------------------------------------
    def test_skill_manage_create_and_delete_refresh_the_catalog(self):
        content = (
            "---\nname: temp-skill\ndescription: Temporary skill\n---\n\n"
            "TEMP BODY."
        )
        with mock.patch.multiple(
            agent,
            SKILLS_DIR=self.legacy,
            _skill_manager=None,
            _skills_dirty=True,
            GROUP_MODE=False,
        ):
            created = agent.skill_manage("create", "temp-skill", content)
            self.assertIn("created", created)
            self.assertTrue(agent._skills_dirty)

            catalog = agent.get_skill_catalog()
            self.assertIn("local:temp-skill", catalog)
            self.assertFalse(agent._skills_dirty)

            deleted = agent.skill_manage("delete", "temp-skill")
            self.assertIn("Deleted", deleted)
            self.assertTrue(agent._skills_dirty)

            agent.reload_skills()
            self.assertNotIn("local:temp-skill", agent.get_skill_catalog())


if __name__ == "__main__":
    unittest.main(verbosity=2)
