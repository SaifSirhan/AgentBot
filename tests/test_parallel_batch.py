"""Parallel read-only batching: parser, autonomous loop, and progress guard.

Run: python -m unittest tests.test_parallel_batch -v
"""
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent
from autonomous_loop import AutonomousLoop, ToolResult, Verification
from loop_guard import GuardVerdict, LoopGuard
from tool_registry import (
    BATCH_EFFECT_ERROR,
    BATCH_SIZE_ERROR,
    ToolRegistry,
    ToolSpec,
    batch_rejection_error,
)


def read_spec(name):
    return ToolSpec(
        name=name,
        description=f"The {name} tool.",
        input_schema={"type": "object"},
        effect_class="read_only",
        timeout=5,
        func=lambda args: args,
    )


def write_spec(name):
    return ToolSpec(
        name=name,
        description=f"The {name} tool.",
        input_schema={"type": "object"},
        effect_class="write",
        timeout=5,
        func=lambda args: args,
    )


class ParserBatchTests(unittest.TestCase):
    def test_top_level_array_returns_a_list_of_actions(self):
        raw = json.dumps([
            {"tool": "read_file", "input": "a.txt"},
            {"tool": "find_files", "input": r"docs|*.py"},
            {"tool": "weather", "input": "Tokyo"},
        ])
        action, error = agent.parse_action(raw)
        self.assertIsNone(error)
        self.assertIsInstance(action, list)
        self.assertEqual(
            [item["tool"] for item in action],
            ["read_file", "find_files", "weather"])
        self.assertEqual(
            [item["input"] for item in action],
            ["a.txt", r"docs|*.py", "Tokyo"])

    def test_fenced_array_is_accepted(self):
        raw = (
            "```json\n"
            + json.dumps([{"tool": "weather", "input": "Paris"},
                          {"tool": "news", "input": ""}])
            + "\n```"
        )
        action, error = agent.parse_action(raw)
        self.assertIsNone(error)
        self.assertEqual([item["tool"] for item in action],
                         ["weather", "news"])

    def test_array_with_invalid_tool_rejects_the_whole_batch(self):
        raw = json.dumps([
            {"tool": "weather", "input": "Paris"},
            {"tool": "not_a_tool", "input": "x"},
        ])
        action, error = agent.parse_action(raw)
        self.assertIsNone(action)
        self.assertIn("'not_a_tool' is not a valid tool name", error)

    def test_line_delimited_actions_still_queue_on_the_first(self):
        raw = ('{"tool": "read_file", "input": "a.txt"}\n'
               '{"tool": "find_files", "input": "docs|*.py"}')
        action, error = agent.parse_action(raw)
        self.assertIsNone(error)
        self.assertIsInstance(action, dict)
        self.assertEqual(action["tool"], "read_file")
        self.assertEqual([item["tool"] for item in action["_queued_actions"]],
                         ["find_files"])


class BatchValidationTests(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.registry.register(read_spec("read_a"))
        self.registry.register(write_spec("write_b"))

    def test_all_read_only_registered_tools_pass(self):
        self.assertIsNone(batch_rejection_error(["read_a"], self.registry))
        self.assertIsNone(
            batch_rejection_error(["read_a", "read_a"], self.registry))

    def test_write_tool_rejects_the_entire_batch(self):
        self.assertEqual(
            batch_rejection_error(["read_a", "write_b"], self.registry),
            BATCH_EFFECT_ERROR)
        self.assertEqual(
            BATCH_EFFECT_ERROR,
            "ERROR: Cannot batch write/system_control tools. "
            "Execute them sequentially.")

    def test_unregistered_tool_fails_closed(self):
        self.assertEqual(
            batch_rejection_error(["read_a", "mystery"], self.registry),
            BATCH_EFFECT_ERROR)

    def test_more_than_three_actions_is_rejected(self):
        self.assertEqual(
            batch_rejection_error(["read_a"] * 4, self.registry),
            BATCH_SIZE_ERROR)


class FastPathBatchTests(unittest.TestCase):
    def test_executor_runs_concurrently_and_preserves_order(self):
        start_times = {}

        def fake(action):
            start_times[action["input"]] = time.monotonic()
            time.sleep(0.3)
            return "out:" + action["input"]

        with mock.patch.object(agent, "execute_tool", side_effect=fake):
            results, rejection = agent.run_read_only_batch([
                {"tool": "weather", "input": "a"},
                {"tool": "news", "input": "b"},
                {"tool": "current_time", "input": ""},
            ])

        self.assertIsNone(rejection)
        self.assertEqual(results, ["out:a", "out:b", "out:"])
        spread = max(start_times.values()) - min(start_times.values())
        self.assertLess(spread, 0.2)

    def test_executor_rejects_a_write_without_calling_any_tool(self):
        with mock.patch.object(agent, "execute_tool") as fake:
            results, rejection = agent.run_read_only_batch([
                {"tool": "weather", "input": "a"},
                {"tool": "write_file", "input": "note.txt|hello"},
            ])

        self.assertEqual(results, [])
        self.assertEqual(rejection, BATCH_EFFECT_ERROR)
        fake.assert_not_called()


class AutonomousBatchTests(unittest.TestCase):
    def build(self, replies, specs, max_steps=3):
        self.prompts = []
        replies = iter(replies)
        self.calls = []
        self.start_times = {}

        def llm(messages):
            self.prompts.append(messages)
            return next(replies, "DONE")

        def execute(tool, args, stop):
            self.start_times[tool] = time.monotonic()
            time.sleep(0.3)
            self.calls.append(tool)
            return ToolResult(True, f"result:{tool}:{args.get('q', '')}")

        registry = ToolRegistry()
        for spec in specs:
            registry.register(spec)
        self.loop = AutonomousLoop(
            llm,
            execute,
            lambda goal, history: Verification(True, "Verified"),
            lambda *args: True,
            {spec.name: spec.description for spec in specs},
            max_steps=max_steps,
            tool_registry=registry,
        )
        return self.loop

    def test_three_read_only_actions_run_in_parallel_in_request_order(self):
        self.build([], [read_spec("read_a"), read_spec("read_b"),
                        read_spec("read_c")])
        batch = json.dumps([
            {"tool": "read_a", "args": {"q": "first"}},
            {"tool": "read_b", "args": {"q": "second"}},
            {"tool": "read_c", "args": {"q": "third"}},
        ])

        def llm(messages):
            self.prompts.append(messages)
            return batch if len(self.prompts) == 1 else "DONE"

        self.loop.llm = llm
        started = time.monotonic()
        result = self.loop.run("Research three things")
        elapsed = time.monotonic() - started

        self.assertEqual(result.status, "success")
        # Sequential execution would need ~0.9s; overlapping sleeps prove
        # the batch ran concurrently.
        self.assertLess(elapsed, 0.75)
        spread = max(self.start_times.values()) - min(self.start_times.values())
        self.assertLess(spread, 0.2)
        self.assertEqual(sorted(self.calls), ["read_a", "read_b", "read_c"])
        self.assertEqual(
            [item.tool for item in result.history],
            ["read_a", "read_b", "read_c"])
        self.assertEqual(
            [item.observation for item in result.history],
            ["result:read_a:first", "result:read_b:second",
             "result:read_c:third"])
        self.assertEqual([item.step for item in result.history], [1, 1, 1])
        self.assertEqual(result.steps, 2)
        scratch = self.loop.scratchpad.view()["observations"]
        self.assertEqual([item["tool"] for item in scratch],
                         ["read_a", "read_b", "read_c"])

    def test_batch_containing_a_write_tool_is_rejected_without_execution(self):
        self.build([], [read_spec("read_a"), write_spec("write_b")])
        batch = json.dumps([
            {"tool": "read_a", "args": {"q": "first"}},
            {"tool": "write_b", "args": {"q": "second"}},
        ])

        def llm(messages):
            self.prompts.append(messages)
            return batch if len(self.prompts) == 1 else "DONE"

        self.loop.llm = llm
        result = self.loop.run("Research and write")

        self.assertEqual(self.calls, [])
        self.assertEqual(result.history, ())
        observed = json.dumps(self.prompts[1])
        self.assertIn(
            "ERROR: Cannot batch write/system_control tools. "
            "Execute them sequentially.",
            observed)

    def test_repeated_identical_batches_are_blocked(self):
        guard = LoopGuard(registry=self._registry())
        batch = [
            {"tool": "read_a", "args": {"q": "same"}},
            {"tool": "read_b", "args": {"q": "same"}},
        ]
        outcomes = [ToolResult(True, "a"), ToolResult(True, "b")]
        for _ in range(3):
            self.assertEqual(guard.check_batch(batch), GuardVerdict.PASS)
            guard.record_batch(batch, outcomes)
        self.assertEqual(guard.check_batch(batch), GuardVerdict.BLOCK)

    def test_changed_batch_input_is_not_blocked(self):
        guard = LoopGuard(registry=self._registry())
        outcomes = [ToolResult(True, "a"), ToolResult(True, "b")]
        for index in range(3):
            batch = [
                {"tool": "read_a", "args": {"q": f"query-{index}"}},
                {"tool": "read_b", "args": {"q": "same"}},
            ]
            guard.record_batch(batch, outcomes)
        changed = [
            {"tool": "read_a", "args": {"q": "fresh"}},
            {"tool": "read_b", "args": {"q": "same"}},
        ]
        self.assertEqual(guard.check_batch(changed), GuardVerdict.PASS)

    def _registry(self):
        registry = ToolRegistry()
        registry.register(read_spec("read_a"))
        registry.register(read_spec("read_b"))
        return registry


if __name__ == "__main__":
    unittest.main()
