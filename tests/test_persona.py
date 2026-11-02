"""Tests for time-of-day persona, mood state, and spontaneous observations.

Run: python -m unittest tests.test_persona -v
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent
import config


class DayPartTests(unittest.TestCase):
    def test_late_night_hours(self):
        for hour in (23, 0, 4):
            self.assertEqual(agent.day_part(hour), "late")

    def test_morning_hours(self):
        for hour in (5, 8, 11):
            self.assertEqual(agent.day_part(hour), "morning")

    def test_day_hours(self):
        for hour in (12, 17, 22):
            self.assertEqual(agent.day_part(hour), "day")


class MoodTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mood_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.object(
            agent, "MOOD_FILE", str(self.tmp / "mood.json"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_file_returns_defaults(self):
        self.assertEqual(
            agent.load_mood(), {"frustration": 0, "success_streak": 0})

    def test_errors_increase_frustration(self):
        for expected in (1, 2, 3):
            mood = agent.note_tool_result("ERROR: tool blew up")
            self.assertEqual(mood["frustration"], expected)
        self.assertEqual(
            agent.get_mood_directive(), agent.FRUSTRATION_DIRECTIVE)

    def test_frustration_is_capped(self):
        for _ in range(15):
            mood = agent.note_tool_result("ERROR: x")
        self.assertEqual(mood["frustration"], agent.MOOD_MAX)

    def test_success_resets_frustration_and_builds_streak(self):
        agent.note_tool_result("ERROR: x")
        agent.note_tool_result("ERROR: y")
        mood = agent.note_tool_result("all good")
        self.assertEqual(mood["frustration"], 0)
        self.assertEqual(mood["success_streak"], 1)
        mood = agent.note_tool_result("still good")
        self.assertEqual(mood["success_streak"], 2)
        self.assertEqual(agent.get_mood_directive(), "")

    def test_success_streak_is_capped(self):
        for _ in range(15):
            mood = agent.note_tool_result("ok")
        self.assertEqual(mood["success_streak"], agent.MOOD_MAX)

    def test_mood_is_persisted(self):
        agent.note_tool_result("ERROR: x")
        data = json.loads((self.tmp / "mood.json").read_text(encoding="utf-8"))
        self.assertEqual(data, {"frustration": 1, "success_streak": 0})

    def test_corrupt_file_falls_back(self):
        (self.tmp / "mood.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(agent.load_mood()["frustration"], 0)

    def test_loaded_values_are_clamped(self):
        (self.tmp / "mood.json").write_text(
            '{"frustration": 99, "success_streak": -3}', encoding="utf-8")
        mood = agent.load_mood()
        self.assertEqual(mood["frustration"], 10)
        self.assertEqual(mood["success_streak"], 0)

    def test_directive_only_at_threshold(self):
        self.assertEqual(agent.get_mood_directive({"frustration": 2}), "")
        self.assertEqual(
            agent.get_mood_directive({"frustration": 3}),
            agent.FRUSTRATION_DIRECTIVE)

    def test_directive_wording_matches_spec(self):
        self.assertEqual(
            agent.FRUSTRATION_DIRECTIVE,
            "You are currently frustrated because tools are failing. "
            "Express slight annoyance to the user.")
        self.assertEqual(
            agent.LATE_NIGHT_DIRECTIVE,
            "It is late at night. The user is up late. Be brief, and gently "
            "suggest they get some rest.")
        self.assertEqual(
            agent.MORNING_DIRECTIVE,
            "It is morning. Greet the user warmly.")


class PromptDirectiveTests(unittest.TestCase):
    def test_late_night_directive_in_prompt(self):
        with mock.patch.object(
            agent, "day_part", return_value="late",
        ), mock.patch.object(agent, "get_mood_directive", return_value=""):
            prompt = agent._build_plan_prompt("hello", "")
        self.assertIn(agent.LATE_NIGHT_DIRECTIVE, prompt)
        self.assertNotIn(agent.MORNING_DIRECTIVE, prompt)

    def test_morning_directive_in_prompt(self):
        with mock.patch.object(
            agent, "day_part", return_value="morning",
        ), mock.patch.object(agent, "get_mood_directive", return_value=""):
            prompt = agent._build_plan_prompt("hello", "")
        self.assertIn(agent.MORNING_DIRECTIVE, prompt)
        self.assertNotIn(agent.LATE_NIGHT_DIRECTIVE, prompt)

    def test_day_has_no_time_directive(self):
        with mock.patch.object(
            agent, "day_part", return_value="day",
        ), mock.patch.object(agent, "get_mood_directive", return_value=""):
            prompt = agent._build_plan_prompt("hello", "")
        self.assertNotIn(agent.LATE_NIGHT_DIRECTIVE, prompt)
        self.assertNotIn(agent.MORNING_DIRECTIVE, prompt)

    def test_frustration_directive_in_prompt(self):
        with mock.patch.object(
            agent, "day_part", return_value="day",
        ), mock.patch.object(
            agent, "get_mood_directive",
            return_value=agent.FRUSTRATION_DIRECTIVE,
        ):
            prompt = agent._build_plan_prompt("hello", "")
        self.assertIn(agent.FRUSTRATION_DIRECTIVE, prompt)


class ToolResultHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mood_hook_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.object(
            agent, "MOOD_FILE", str(self.tmp / "mood.json"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_execute_tool_feeds_result_to_mood(self):
        with mock.patch.object(
            agent, "_execute_tool_impl", return_value="ERROR: boom",
        ), mock.patch.object(agent, "note_tool_result") as note:
            result = agent.execute_tool({"tool": "x"})
        self.assertEqual(result, "ERROR: boom")
        note.assert_called_once_with("ERROR: boom")

    def test_chat_tool_counts_as_success(self):
        result = agent.execute_tool({"tool": "chat", "input": "hi"})
        self.assertEqual(result, "AI: hi")
        self.assertEqual(agent.load_mood()["success_streak"], 1)

    def test_unknown_tool_counts_as_error(self):
        result = agent.execute_tool({"tool": "does_not_exist"})
        self.assertTrue(result.startswith("ERROR:"))
        self.assertEqual(agent.load_mood()["frustration"], 1)


class SpontaneousThoughtTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="thought_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.shot = self.tmp / "shot.png"
        self.shot.write_bytes(b"fake-png")

    def test_returns_single_line(self):
        with mock.patch.object(
            agent, "describe_image",
            return_value="You have 50 unread emails.\nSecond line.",
        ):
            self.assertEqual(
                agent.spontaneous_thought(str(self.shot)),
                "You have 50 unread emails.")

    def test_nothing_and_errors_are_silent(self):
        for reply in (
            "NOTHING",
            "  NOTHING  ",
            '"NOTHING"',
            "ERROR: DEEPSEEK_API_KEY not set",
            "",
        ):
            with mock.patch.object(
                agent, "describe_image", return_value=reply,
            ):
                self.assertEqual(
                    agent.spontaneous_thought(str(self.shot)), "", reply)

    def test_missing_file_is_silent(self):
        with mock.patch.object(agent, "describe_image") as describe:
            self.assertEqual(
                agent.spontaneous_thought(str(self.tmp / "nope.png")), "")
        describe.assert_not_called()

    def test_long_answer_is_truncated(self):
        with mock.patch.object(
            agent, "describe_image", return_value="x" * 500,
        ):
            self.assertEqual(
                len(agent.spontaneous_thought(str(self.shot))), 240)


class ConfigTests(unittest.TestCase):
    def test_spontaneous_thoughts_default_off(self):
        self.assertIs(
            config.DEFAULTS["SPONTANEOUS_THOUGHTS_ENABLED"], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
