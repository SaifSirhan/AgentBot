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
        self.assertIn("load_skill", agent.TOOL_DESCRIPTIONS)
        self.assertIn('load_skill("local:core_tools")',
                      agent.TOOL_DESCRIPTIONS)

        manual = agent.load_skill_tool("core_tools")
        self.assertEqual(manual.count("load_skill(input)"), 1)

        i_manage = manual.index("skill_manage(input)")
        i_load = manual.index("load_skill(input)")
        i_examples = manual.index("10. EXAMPLES")
        self.assertLess(i_manage, i_load)
        self.assertLess(i_load, i_examples)

        between = manual[i_manage:i_load]
        self.assertIn("NEVER invent a skill body", between)
        self.assertNotIn("\n  run_command(input)", between)

    def test_prompt_index_points_at_the_core_tools_manual(self):
        prompt = agent._build_plan_prompt("hello", "")
        self.assertIn("Available tools:", prompt)
        self.assertIn('load_skill("local:core_tools")', prompt)
        self.assertIn("Do not guess the input format.", prompt)
        self.assertIn(
            'To call a tool, you MUST output a valid JSON block: '
            '{"tool": "tool_name", "input": "tool_input"}.', prompt)
        self.assertIn(
            'Output ONLY valid JSON. NEVER write tool("arg") or XML.', prompt)
        self.assertLess(len(agent.TOOL_DESCRIPTIONS), 2000)
        for name in agent.VALID_TOOLS - {"chat", "done"}:
            self.assertIn(name, agent.TOOL_DESCRIPTIONS)

    def test_prompt_carries_anti_hallucination_and_path_lock_rules(self):
        prompt = agent._build_plan_prompt("hello", "")
        self.assertIn(
            "NEVER tell the user a task is Done, Saved, Translated, or Opened "
            "unless you have just received a successful tool result confirming "
            "it. If you haven't run execute_python and seen the result, you "
            "haven't done the task.", prompt)
        self.assertIn(
            r"When a user provides an exact absolute file path (e.g., "
            r"C:\Users\USER\Downloads\Log Intern.docx), you must use that "
            r"EXACT string in your script. Do not search other folders like "
            r"Telegram Desktop.", prompt)

    def test_prompt_enforces_media_control_and_music_honesty(self):
        prompt = agent._build_plan_prompt("hello", "")
        self.assertIn(
            "If the user says pause, skip, next, or stop the music, you MUST "
            "use media_control (global media keys), NOT play_on_youtube.",
            prompt)
        self.assertIn(
            "NEVER say a song is queued, paused, or playing unless you have "
            "successfully run media_control, play_on_youtube, or queue_on_ytm "
            "and verified the result. Do not guess.", prompt)
        self.assertIn(
            "If the user asks to queue a song, use queue_on_ytm, NOT "
            "media_control or play_on_youtube.", prompt)

    def test_core_tools_manual_documents_music_controls_and_queue(self):
        manual = " ".join(agent.load_skill_tool("core_tools").split())
        self.assertIn("close_music_tab()", manual)
        self.assertIn("queue_on_ytm(input)", manual)
        self.assertIn("no queue API", manual)
        self.assertIn(
            "If the user asks to queue a song, use queue_on_ytm. If they ask "
            "to skip or pause, use media_control. NEVER claim you queued or "
            "paused a song unless you actually ran one of these tools.", manual)

    def test_core_tools_manual_holds_the_full_catalog(self):
        manual = agent.load_skill_tool("core_tools")
        for anchor in ("browse_web(input)", "execute_python(input)",
                       "NEVER CLAIM AN ACTION YOU DID NOT TAKE", "10. EXAMPLES"):
            self.assertIn(anchor, manual)

    def test_core_tools_manual_requires_safe_docx_tool(self):
        manual = " ".join(agent.load_skill_tool("core_tools").split())
        self.assertIn(
            "NEVER write your own python-docx script. ALWAYS use the "
            "edit_docx_safe tool to make changes to Word documents. It handles "
            "locks, atomic saves, and validation.", manual)
        self.assertNotIn("You MUST use from docx import Document", manual)

    def test_progressive_disclosure_prompt_budget(self):
        prompt = agent._build_plan_prompt("hello", "")
        self.assertLess(
            len(prompt) / 4, 2000,
            f"prompt is {len(prompt)} chars (~{len(prompt) / 4:.0f} tokens)")

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
    def test_group_mode_allows_load_skill_and_omits_catalog(self):
        with self.patched(group_mode=True):
            prompt = agent._build_plan_prompt("hello", "")
            self.assertNotIn("AVAILABLE SKILLS", prompt)
            self.assertNotIn("local:demo-skill", prompt)
            self.assertIn("load_skill", agent.SAFE_GROUP_TOOLS)
            self.assertIn(
                'You are in a group chat. You can use '
                'load_skill("local:core_tools") to read the manual',
                prompt)

    def test_group_mode_loads_only_core_tools(self):
        with mock.patch.object(agent, "GROUP_MODE", True):
            for skill_id in ("demo-skill", "local:stremio", "stremio"):
                self.assertEqual(
                    agent.execute_tool(
                        {"tool": "load_skill", "input": skill_id}),
                    "ERROR: Only core_tools can be loaded in group mode.")
            for skill_id in ("core_tools", "local:core_tools"):
                self.assertIn(
                    "Skill: local:core_tools",
                    agent.execute_tool(
                        {"tool": "load_skill", "input": skill_id}))

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
    # Skill directive injection
    # ------------------------------------------------------------------
    def test_stremio_request_injects_exact_directive(self):
        with self.patched(group_mode=False):
            prompt = agent._build_plan_prompt("watch Cherry", "")
        directive = (
            "Use the Stremio skill now. Do not think about it, just execute "
            'the steps. Call load_skill("local:stremio") first.')
        self.assertIn(directive, prompt)
        self.assertLess(prompt.index("AVAILABLE SKILLS"),
                        prompt.index(directive))

    def test_music_request_does_not_inject_stremio(self):
        self.assertEqual(agent._skill_directive("play a song"), "")

    def test_watch_title_starts_a_stremio_task(self):
        self.assertTrue(agent.starts_new_task("watch Jujutsu Kaisen"))
        self.assertFalse(agent.starts_new_task("now click the poster"))
        self.assertFalse(agent.starts_new_task("play a song"))

    def test_generic_start_and_play_do_not_inject_stremio(self):
        for text in ("start", "start a Docker server", "start the server",
                     "start the app", "play", "play Jujutsu Kaisen"):
            self.assertEqual(agent._skill_directive(text), "", text)

    def test_only_media_words_inject_stremio(self):
        for text in ("stremio", "watch Cherry", "play a movie",
                     "start the episode"):
            self.assertIn("Use the Stremio skill now.",
                          agent._skill_directive(text), text)

    def test_named_skill_injects_directive(self):
        with self.patched(group_mode=False):
            directive = agent._skill_directive("use demo skill")
        self.assertIn("Use the demo skill now.", directive)

    def test_group_mode_does_not_inject_skill_directive(self):
        with self.patched(group_mode=True):
            prompt = agent._build_plan_prompt("watch Cherry", "")
        self.assertNotIn("Use the Stremio skill now.", prompt)

    def test_real_stremio_prompt_stays_under_token_budget(self):
        with mock.patch.multiple(
            agent, _skill_manager=None, _skills_dirty=True, GROUP_MODE=False
        ), mock.patch.object(agent, "day_part", return_value="late"), \
                mock.patch.object(agent, "get_mood_directive",
                                  return_value=agent.FRUSTRATION_DIRECTIVE):
            prompt = agent._build_plan_prompt("watch Cherry", "")
        self.assertLess(len(prompt) / 4, MAX_PROMPT_TOKENS)

    # ------------------------------------------------------------------
    # Skill manage -> dirty -> catalog
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


class StremioSkillContentTests(unittest.TestCase):
    """The Stremio skill must stay a strict four-step checklist."""

    def test_skill_has_strict_checklist(self):
        path = (Path(__file__).resolve().parent.parent
                / "skills" / "stremio" / "SKILL.md")
        text = " ".join(path.read_text(encoding="utf-8").split())
        for step in (
            "1. **Focus Stremio. Take a screenshot.**",
            "2. **Click the search bar. Type the requested show/movie name. "
            "Press Enter. Wait 3 seconds.**",
            "3. **Take a screenshot. Click the poster that exactly matches "
            "the title. Wait 3 seconds.**",
            "4. **Take a screenshot. Find the Torrentio link that says "
            "'1080p'. Click it.**",
            "DO NOT click any other link.",
            "DO NOT ask for permission.",
            "STOP after clicking the 1080p link.",
        ):
            self.assertIn(step, text)
        self.assertIn("external player (mpv)", text)
        for forbidden in ("web.stremio.com", "browse_web", "Playwright",
                          "Chromium"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
