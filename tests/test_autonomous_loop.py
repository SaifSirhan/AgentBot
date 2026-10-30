"""Pure loop tests: model, dispatcher, authorization and verifier are fakes."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autonomous_loop import AutonomousLoop, LoopResult, RecordedAction, Scratchpad, ToolResult, Verification


def action(tool, **args):
    return json.dumps({"tool": tool, "args": args})


class LoopCheckpointTests(unittest.TestCase):
    def make_loop(self, replies, max_steps=2, verdicts=None):
        self.prompts = []
        replies = iter(replies)

        def llm(messages):
            self.prompts.append(messages)
            return next(replies, "DONE")

        def execute(tool, args, stop):
            if tool == "request_tool":
                loop.tools["new_tool"] = "New task capability"
            return ToolResult(True, "complete observation " + "x" * 10000)

        self.execute = mock.Mock(side_effect=execute)
        self.verify = mock.Mock(side_effect=verdicts) if verdicts else mock.Mock(
            return_value=Verification(True, "Verified"))
        loop = AutonomousLoop(llm, self.execute, self.verify, lambda *a: True,
                              {"work": "Work", "request_tool": "Request"}, max_steps=max_steps)
        return loop

    def test_instructions_enforce_music_controls_and_honesty(self):
        loop = self.make_loop([action("work")])
        loop.run("Do work")
        system = self.prompts[0][0]["content"]
        self.assertIn("media_control", system)
        self.assertIn("never play_on_youtube for playback", system)
        self.assertIn("Queueing a song uses queue_on_ytm", system)
        self.assertIn("queued, paused, or playing", system)

    def test_grant_does_not_consume_last_work_step(self):
        loop = self.make_loop([action("request_tool"), action("new_tool")], max_steps=1)
        result = loop.run("Do work")
        self.assertEqual(result.status, "success")
        self.assertEqual([a.tool for a in result.history], ["request_tool", "new_tool"])
        self.assertEqual(result.steps, 2)
        self.verify.assert_called_once()
        catalog = json.loads(self.prompts[1][0]["content"].split("Allowed tools: ")[1])
        self.assertIn("new_tool", catalog)
        self.assertTrue(any(m["role"] == "system" and "continue" in m["content"].lower()
                            and "grant" in m["content"].lower() for m in self.prompts[1][1:]))

    def test_grants_do_not_consume_single_repair_action(self):
        loop = self.make_loop([action("work"), "DONE", action("request_tool"),
                               action("request_tool"), action("new_tool"), action("work")],
                              max_steps=4, verdicts=[Verification(False, "Repair needed"),
                                                     Verification(True, "Repaired")])
        result = loop.run("Do work")
        self.assertEqual(result.status, "success")
        self.assertEqual(result.steps, 5)
        self.assertEqual([a.tool for a in result.history],
                         ["work", "request_tool", "request_tool", "new_tool"])
        self.assertEqual(self.verify.call_count, 2)

    def test_repeated_grants_are_bounded_and_never_successful(self):
        loop = self.make_loop([action("request_tool")] * 100, max_steps=1)
        result = loop.run("Do work")
        self.assertEqual(result.status, "step_limit")
        self.assertGreater(len(result.history), 1)
        self.assertLessEqual(len(self.prompts), 5)
        self.assertEqual(result.steps, len(self.prompts))
        self.verify.assert_not_called()

    def test_checkpoint_initial_actions_and_exit_preserve_full_state(self):
        prior = RecordedAction(8, "work", True, "prior " + "p" * 12000)
        notes = {"plan": "Keep original facts"}
        checkpoints = []
        loop = self.make_loop([action("scratchpad_write", key="next", text="Write"),
                               action("work")])
        result = loop.run("Do work", initial_history=[prior], initial_notes=notes,
                          on_checkpoint=checkpoints.append)
        self.assertEqual([r.status for r in checkpoints], ["running"] * 3 + ["success"])
        self.assertEqual([r.steps for r in checkpoints], [0, 1, 2, 2])
        self.assertEqual(checkpoints[0].scratchpad_notes, notes)
        self.assertEqual(result.scratchpad_notes, {**notes, "next": "Write"})
        self.assertEqual(result.history[0].observation, prior.observation)
        self.assertGreater(len(result.history[-1].observation), 10000)
        self.assertIn("Keep original facts", json.dumps(self.prompts[0]))
        checkpoints[-1].scratchpad_notes.clear()
        self.assertIn("plan", result.scratchpad_notes)
        self.assertEqual(notes, {"plan": "Keep original facts"})

    def test_all_terminal_paths_checkpoint(self):
        for case, expected in [("cancel", "cancelled"), ("deny", "denied"),
                               ("llm", "error"), ("limit", "step_limit"),
                               ("grant_failure", "error"), ("repair", "incomplete")]:
            with self.subTest(case=case):
                loop = self.make_loop([action("request_tool")], max_steps=1)
                if case == "cancel":
                    loop.stop_event.set()
                elif case == "deny":
                    loop.authorize = lambda *a: False
                elif case == "llm":
                    loop.llm = mock.Mock(side_effect=RuntimeError("offline"))
                elif case == "grant_failure":
                    self.execute.side_effect = lambda *a: ToolResult(False, "blocked grant")
                elif case == "repair":
                    loop = self.make_loop([action("work"), action("work")], max_steps=1,
                                          verdicts=[Verification(False, "no")] * 2)
                snapshots = []
                result = loop.run("Do work", on_checkpoint=snapshots.append)
                self.assertEqual(result.status, expected)
                self.assertEqual(snapshots[0].status, "running")
                self.assertEqual(snapshots[-1], result)

    def test_cancel_after_grant_never_executes_work(self):
        loop = self.make_loop([action("request_tool"), action("new_tool")])
        def execute(*args):
            loop.stop_event.set()
            return ToolResult(True, "granted")
        self.execute.side_effect = execute
        result = loop.run("Do work")
        self.assertEqual(result.status, "cancelled")
        self.assertEqual([a.tool for a in result.history], ["request_tool"])
        self.verify.assert_not_called()

    def test_scratchpad_notes_roundtrip_is_validated_and_detached(self):
        scratchpad = Scratchpad()
        scratchpad.write("plan", "facts")
        notes = scratchpad.export_notes()
        notes["plan"] = "edited"
        self.assertEqual(scratchpad.export_notes(), {"plan": "facts"})
        scratchpad.restore_notes(notes)
        notes.clear()
        self.assertEqual(scratchpad.export_notes(), {"plan": "edited"})
        with self.assertRaises(ValueError):
            scratchpad.restore_notes({"bad": "x" * 4001})
        self.assertEqual(scratchpad.export_notes(), {"plan": "edited"})
        scratchpad.restore_notes(None)
        self.assertEqual(scratchpad.export_notes(), {})

    def test_loop_result_notes_default_is_independent(self):
        first = LoopResult("running", 0, "", ())
        second = LoopResult("running", 0, "", ())
        first.scratchpad_notes["plan"] = "test"
        self.assertEqual(second.scratchpad_notes, {})


if __name__ == "__main__":
    unittest.main()
