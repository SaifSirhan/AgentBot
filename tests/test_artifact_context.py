"""P5: oversized tool observations become artifacts in the autonomous loop.

Run: python -m unittest tests.test_artifact_context -v
"""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import artifact_store
from autonomous_loop import (
    AutonomousLoop,
    RecordedAction,
    ToolResult,
    Verification,
)
from tool_registry import ToolRegistry, ToolSpec


def action(tool, **args):
    return json.dumps({"tool": tool, "args": args})


def read_spec(name):
    return ToolSpec(
        name=name,
        description=f"The {name} tool.",
        input_schema={"type": "object"},
        effect_class="read_only",
        timeout=5,
        func=lambda args: args,
    )


class ArtifactLoopTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(
            os.environ, {"LOCALAPPDATA": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.prompts = []

    def make_loop(self, replies, execute, tools=("fetch",), registry=None,
                  max_steps=3):
        replies = iter(replies)

        def llm(messages):
            self.prompts.append(messages)
            return next(replies, "DONE")

        return AutonomousLoop(
            llm,
            execute,
            lambda goal, history: Verification(True, "Verified"),
            lambda *args: True,
            {name: f"Tool {name}" for name in tools},
            max_steps=max_steps,
            tool_registry=registry,
        )

    def summary_id(self, observation):
        match = re.match(
            r"\[ARTIFACT SAVED: ([0-9a-f]{32}) - ", observation)
        self.assertIsNotNone(match, observation[:120])
        return match.group(1)

    def test_large_output_is_saved_and_replaced_with_a_summary(self):
        raw = "0123456789" * 250
        loop = self.make_loop(
            [action("fetch")],
            mock.Mock(return_value=ToolResult(True, raw)))

        result = loop.run("fetch a page")

        stored = result.history[0].observation
        artifact_id = self.summary_id(stored)
        self.assertEqual(artifact_store.read_artifact(artifact_id), raw)
        self.assertEqual(
            stored,
            "[ARTIFACT SAVED: " + artifact_id + " - " + raw[:200]
            + "... Use read_artifact to view the full text.]")
        self.assertEqual(
            loop.scratchpad.view()["observations"][0]["excerpt"], stored)
        injected = json.dumps(self.prompts[1])
        self.assertIn(raw[:200], injected)
        self.assertNotIn(raw, injected)

    def test_two_thousand_chars_is_left_alone(self):
        raw = "a" * 2000
        loop = self.make_loop(
            [action("fetch")],
            mock.Mock(return_value=ToolResult(True, raw)))

        result = loop.run("fetch a page")

        self.assertEqual(result.history[0].observation, raw)

    def test_two_thousand_and_one_chars_is_summarized(self):
        raw = "a" * 2001
        loop = self.make_loop(
            [action("fetch")],
            mock.Mock(return_value=ToolResult(True, raw)))

        result = loop.run("fetch a page")

        self.assertTrue(
            result.history[0].observation.startswith("[ARTIFACT SAVED: "))

    def test_read_artifact_output_is_never_re_artifacted(self):
        raw = "z" * 3000
        loop = self.make_loop(
            [action("read_artifact")],
            mock.Mock(return_value=ToolResult(True, raw)),
            tools=("read_artifact",))

        result = loop.run("read the full artifact")

        self.assertEqual(result.history[0].observation, raw)

    def test_scratchpad_page_reads_are_never_re_artifacted(self):
        prior = RecordedAction(1, "search_web", True, "p" * 3000)
        loop = self.make_loop(
            [action("scratchpad_read", id="obs-1")],
            mock.Mock(),
            tools=("scratchpad_read",))

        result = loop.run(
            "read the next page", initial_history=[prior])

        stored = result.history[-1].observation
        self.assertFalse(stored.startswith("[ARTIFACT SAVED: "), stored[:80])
        self.assertEqual(json.loads(stored)["text"], "p" * 3000)

    def test_each_large_batch_result_is_saved_in_request_order(self):
        registry = ToolRegistry()
        registry.register(read_spec("read_a"))
        registry.register(read_spec("read_b"))
        outputs = {"read_a": "A" * 2100, "read_b": "B" * 2200}

        def execute(tool, args, stop):
            return ToolResult(True, outputs[tool])

        batch = json.dumps([
            {"tool": "read_a", "args": {}},
            {"tool": "read_b", "args": {}},
        ])
        loop = self.make_loop(
            [batch], execute, tools=("read_a", "read_b"),
            registry=registry)

        result = loop.run("fetch both pages")

        self.assertEqual(
            [item.tool for item in result.history], ["read_a", "read_b"])
        first_id = self.summary_id(result.history[0].observation)
        second_id = self.summary_id(result.history[1].observation)
        self.assertEqual(
            artifact_store.read_artifact(first_id), outputs["read_a"])
        self.assertEqual(
            artifact_store.read_artifact(second_id), outputs["read_b"])


if __name__ == "__main__":
    unittest.main()
