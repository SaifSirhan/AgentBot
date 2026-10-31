"""LoopGuard unit tests and AutonomousLoop integration tests.

Run: python -m unittest tests.test_loop_guard -v
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from autonomous_loop import AutonomousLoop, ToolResult, Verification
from loop_guard import LoopGuard
from tool_registry import ToolRegistry, ToolSpec


def action(tool, **args):
    return json.dumps({"tool": tool, "args": args})


def polling_registry(*names):
    registry = ToolRegistry()
    for name in names:
        registry.register(ToolSpec(
            name=name,
            description=f"{name} polling tool.",
            input_schema={"type": "object"},
            effect_class="read_only",
            timeout=5,
            func=lambda value: "ok",
            is_polling=True,
        ))
    return registry


class LoopGuardTests(unittest.TestCase):
    def test_three_identical_outcomes_block_the_next_attempt(self):
        guard = LoopGuard()
        for _ in range(3):
            self.assertEqual(guard.check("flaky", "same input"), "PASS")
            guard.record("flaky", "same input", "same error")
        self.assertEqual(guard.check("flaky", "same input"), "BLOCK")

    def test_three_identical_inputs_block_even_when_outputs_differ(self):
        guard = LoopGuard()
        for index in range(3):
            self.assertEqual(guard.check("flaky", "same input"), "PASS")
            guard.record("flaky", "same input", f"error {index}")
        self.assertEqual(guard.check("flaky", "same input"), "BLOCK")

    def test_different_inputs_do_not_count_together(self):
        guard = LoopGuard()
        for value in ("a", "b", "c"):
            guard.check("flaky", value)
            guard.record("flaky", value, "same error")
        self.assertEqual(guard.check("flaky", "d"), "PASS")

    def test_alternating_pairs_block_the_proposed_sixth_action(self):
        guard = LoopGuard()
        for tool, value in (
                ("alpha", "1"), ("beta", "2"),
                ("alpha", "1"), ("beta", "2"),
                ("alpha", "1")):
            self.assertEqual(guard.check(tool, value), "PASS")
            guard.record(tool, value, "ok")

        self.assertEqual(guard.check("beta", "2"), "BLOCK")
        self.assertEqual(guard.check("gamma", "3"), "PASS")

    def test_five_consecutive_polling_calls_pass_and_the_sixth_blocks(self):
        registry = polling_registry("poll_tool")
        guard = LoopGuard(registry=registry)
        for _ in range(5):
            self.assertEqual(guard.check("poll_tool", "now"), "PASS")
            guard.record("poll_tool", "now", "same state")
        self.assertEqual(guard.check("poll_tool", "now"), "BLOCK")

    def test_other_work_resets_the_polling_streak(self):
        registry = polling_registry("poll_tool")
        guard = LoopGuard(registry=registry)
        for _ in range(5):
            guard.record("poll_tool", "now", "same state")
        guard.record("work", "1", "done")
        self.assertEqual(guard.check("poll_tool", "now"), "PASS")

    def test_identical_actions_leave_the_window_after_enough_other_work(self):
        guard = LoopGuard()
        for _ in range(3):
            guard.record("flaky", "same", "same error")
        self.assertEqual(guard.check("flaky", "same"), "BLOCK")
        for index in range(10):
            guard.check("work", index)
            guard.record("work", index, f"result {index}")
        self.assertEqual(guard.check("flaky", "same"), "PASS")

    def test_fourth_near_stationary_click_blocks(self):
        guard = LoopGuard()
        for x, y in ((920, 75), (920, 71), (914, 72)):
            self.assertEqual(
                guard.check("click_coordinates", f"{x},{y}"), "PASS")
            guard.record("click_coordinates", f"{x},{y}", "clicked")
        self.assertEqual(
            guard.check("click_coordinates", "915,70"), "BLOCK")

    def test_three_near_stationary_clicks_are_allowed(self):
        guard = LoopGuard()
        for x, y in ((920, 75), (920, 71)):
            guard.record("click_coordinates", f"{x},{y}", "clicked")
        self.assertEqual(
            guard.check("click_coordinates", "914,72"), "PASS")

    def test_clicks_moving_across_the_screen_do_not_block(self):
        guard = LoopGuard()
        for x in (100, 200, 300):
            self.assertEqual(
                guard.check("click_coordinates", f"{x},100"), "PASS")
            guard.record("click_coordinates", f"{x},100", "clicked")
        self.assertEqual(
            guard.check("click_coordinates", "400,100"), "PASS")

    def test_other_work_resets_the_click_streak(self):
        guard = LoopGuard()
        for x, y in ((920, 75), (920, 71), (914, 72)):
            guard.record("click_coordinates", f"{x},{y}", "clicked")
        guard.record("read_file", "notes.txt", "file contents")
        self.assertEqual(
            guard.check("click_coordinates", "915,70"), "PASS")

    def test_click_window_suffix_still_counts_as_the_same_spot(self):
        guard = LoopGuard()
        for value in ("920,75 | Stremio", "920,71 | Stremio",
                      "914,72 | Stremio"):
            guard.record("click_coordinates", value, "clicked")
        self.assertEqual(
            guard.check("click_coordinates", "915,70 | Stremio"), "BLOCK")

    def test_unparseable_click_input_does_not_crash(self):
        guard = LoopGuard()
        guard.record("click_coordinates", "center", "clicked")
        guard.record("click_coordinates", "left", "clicked")
        guard.record("click_coordinates", "top", "clicked")
        self.assertEqual(
            guard.check("click_coordinates", "anywhere"), "PASS")


class LoopGuardLoopTests(unittest.TestCase):
    def make_loop(self, replies, execute, *, registry=None, max_steps=10,
                  wall_clock_timeout=300.0):
        prompts = []
        replies = iter(replies)

        def llm(messages):
            prompts.append(messages)
            return next(replies, "DONE")

        verify = mock.Mock(return_value=Verification(True, "Verified"))
        loop = AutonomousLoop(
            llm,
            execute,
            verify,
            lambda tool, args: True,
            {"flaky": "Flaky tool", "poll": "Polling tool", "work": "Work"},
            max_steps=max_steps,
            tool_registry=registry,
            wall_clock_timeout=wall_clock_timeout,
        )
        return loop, prompts, verify

    def test_repeated_identical_errors_block_the_fourth_call_and_fail(self):
        execute = mock.Mock(return_value=ToolResult(False, "same failure"))
        loop, prompts, verify = self.make_loop([action("flaky")] * 10, execute)

        result = loop.run("finish the task")

        self.assertEqual(execute.call_count, 3)
        self.assertEqual(result.status, "failed")
        self.assertIn("stuck in a loop", result.message)
        self.assertIn("flaky", result.message)
        self.assertEqual(verify.call_count, 0)
        self.assertTrue(any(
            "[GUARD]" in json.dumps(messages) for messages in prompts))

    def test_polling_tool_may_run_five_times_and_then_finish(self):
        registry = polling_registry("poll")
        execute = mock.Mock(return_value=ToolResult(True, "still waiting"))
        loop, prompts, verify = self.make_loop(
            [action("poll")] * 5 + ["DONE"], execute, registry=registry)

        result = loop.run("wait for the server")

        self.assertEqual(result.status, "success", result.message)
        self.assertEqual(execute.call_count, 5)
        self.assertEqual(verify.call_count, 1)
        self.assertFalse(any(
            "[GUARD]" in json.dumps(messages) for messages in prompts))

    def test_wall_clock_timeout_fails_the_loop(self):
        clock = {"now": 0.0}

        def llm(messages):
            clock["now"] = 301.0
            return action("work")

        execute = mock.Mock(return_value=ToolResult(True, "working"))
        loop = AutonomousLoop(
            llm,
            execute,
            mock.Mock(return_value=Verification(True, "Verified")),
            lambda tool, args: True,
            {"work": "Work"},
            max_steps=10,
            wall_clock_timeout=300.0,
        )
        with mock.patch(
                "autonomous_loop.time.monotonic",
                side_effect=lambda: clock["now"]):
            result = loop.run("do work")

        self.assertEqual(result.status, "failed")
        self.assertIn("timeout", result.message.lower())
        self.assertEqual(execute.call_count, 1)


if __name__ == "__main__":
    unittest.main()
