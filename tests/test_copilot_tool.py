"""Behavioral tests for the main-chat Screen Copilot capability adapter."""
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import screen_copilot
from copilot_tool import (
    format_progress,
    format_result,
    resolve_target,
    run_copilot_capability,
)
from screen_copilot import (
    CopilotBusy,
    CopilotResult,
    ProcessIdentity,
    TargetWindow,
    WindowBounds,
)


def target(hwnd, title, executable, pid):
    return TargetWindow(
        hwnd=hwnd,
        title=title,
        process=ProcessIdentity(
            pid=pid,
            executable=executable,
            started_at=1,
            elevated=False,
        ),
        visible=True,
        bounds=WindowBounds(0, 0, 800, 600),
    )


class ResolveTargetTests(unittest.TestCase):
    def test_exact_window_title_selects_only_that_target(self):
        targets = (
            target(10, "Quarterly plan - Word", r"C:\Office\WINWORD.EXE", 100),
            target(20, "Notes - Word", r"C:\Office\WINWORD.EXE", 200),
        )

        selected, candidates = resolve_target("Quarterly plan - Word", targets)

        self.assertEqual(selected.hwnd, 10)
        self.assertEqual(candidates, ())

    def test_process_name_selects_a_unique_window(self):
        targets = (
            target(10, "Untitled - Notepad", r"C:\Windows\notepad.exe", 100),
            target(20, "Quarterly plan - Word", r"C:\Office\WINWORD.EXE", 200),
        )

        selected, candidates = resolve_target("notepad", targets)

        self.assertEqual(selected.hwnd, 10)
        self.assertEqual(candidates, ())

    def test_ambiguous_process_match_returns_candidates_without_selecting(self):
        targets = (
            target(10, "Notes - Word", r"C:\Office\WINWORD.EXE", 100),
            target(20, "Quarterly plan - Word", r"C:\Office\WINWORD.EXE", 200),
        )

        selected, candidates = resolve_target("word", targets)

        self.assertIsNone(selected)
        self.assertEqual(tuple(item.hwnd for item in candidates), (10, 20))

    def test_missing_target_returns_no_selection_or_candidates(self):
        selected, candidates = resolve_target(
            "Firefox", (target(10, "Notes", r"C:\Windows\notepad.exe", 100),))

        self.assertIsNone(selected)
        self.assertEqual(candidates, ())


class ProgressFormattingTests(unittest.TestCase):
    def test_service_events_become_safe_main_task_progress(self):
        cases = (
            ({"event": "observing", "step": 1, "status": "ready"},
             "Copilot: Inspecting the selected window…"),
            ({"event": "planning", "step": 2, "status": "started"},
             "Copilot: Planning step 2…"),
            ({"event": "action", "step": 3, "action": "type_text",
              "status": "started"},
             "Copilot: Performing step 3…"),
            ({"event": "step_outcome", "step": 3, "action": "type_text",
              "status": "executed"},
             "Copilot: Step 3 finished; checking the window…"),
        )
        for event, expected in cases:
            with self.subTest(event=event):
                self.assertEqual(format_progress(event), expected)

    def test_unknown_or_malformed_progress_does_not_leak_fields(self):
        self.assertEqual(
            format_progress({"event": "secret", "goal": "private"}),
            "Copilot: Working…",
        )
        self.assertEqual(format_progress(None), "Copilot: Working…")


class ResultFormattingTests(unittest.TestCase):
    def test_completed_result_reports_observed_change_without_overclaiming(self):
        result = CopilotResult(
            status="completed",
            summary="The approved plan finished.",
            task_id="internal-task-id",
            steps=2,
            changed=True,
            last_action="type_text",
        )

        text = format_result(result)

        self.assertIn("Screen Copilot status: completed", text)
        self.assertIn("Actions completed: 2", text)
        self.assertIn("Observed UI change: yes", text)
        self.assertIn("Last action: type_text", text)
        self.assertIn("not independent verification", text)
        self.assertNotIn("internal-task-id", text)

    def test_denied_result_is_terminal_and_says_nothing_ran(self):
        result = CopilotResult(
            status="denied",
            summary="Approval was denied.",
            task_id="internal-task-id",
        )

        text = format_result(result)

        self.assertTrue(text.startswith("ERROR:"), text)
        self.assertIn("denied", text)
        self.assertIn("Do not retry", text)


class RunCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.targets = (
            target(10, "Notes - Notepad", r"C:\Windows\notepad.exe", 100),
        )
        self.args = {"target": "notepad", "goal": "Type hello into the editor."}
        self.planner = lambda prompt: "{}"
        self.approval = lambda request: False

    def test_missing_interactive_approval_fails_closed_before_scanning(self):
        with mock.patch.object(
                screen_copilot, "list_copilot_targets") as scan, mock.patch.object(
                screen_copilot, "run_copilot_task") as run:
            result = run_copilot_capability(
                self.args, planner=self.planner, approval_handler=None,
                stop_event=threading.Event())

        scan.assert_not_called()
        run.assert_not_called()
        self.assertTrue(result.startswith("ERROR:"), result)
        self.assertIn("interactive approval", result)

    def test_ambiguous_target_returns_choices_without_running(self):
        targets = self.targets + (
            target(20, "Other - Notepad", r"C:\Windows\notepad.exe", 200),
        )
        with mock.patch.object(
                screen_copilot, "list_copilot_targets", return_value=targets), \
                mock.patch.object(screen_copilot, "run_copilot_task") as run:
            result = run_copilot_capability(
                self.args, planner=self.planner, approval_handler=self.approval,
                stop_event=threading.Event())

        run.assert_not_called()
        self.assertTrue(result.startswith("ERROR:"), result)
        self.assertIn("ambiguous", result.lower())
        self.assertIn("Notes - Notepad", result)
        self.assertIn("Other - Notepad", result)

    def test_unique_target_runs_bounded_service_and_forwards_safe_progress(self):
        captured = {}
        progress = []

        def fake_run(**kwargs):
            captured.update(kwargs)
            kwargs["progress_handler"]({
                "event": "observing", "step": 1, "status": "ready",
                "goal": "must not leak",
            })
            return CopilotResult(
                status="completed", summary="Plan finished.", task_id="hidden",
                steps=1, changed=True, last_action="type_text",
            )

        with mock.patch.object(
                screen_copilot, "list_copilot_targets", return_value=self.targets), \
                mock.patch.object(screen_copilot, "run_copilot_task", side_effect=fake_run):
            result = run_copilot_capability(
                self.args, planner=self.planner, approval_handler=self.approval,
                stop_event=threading.Event(), progress_handler=progress.append)

        self.assertEqual(captured["hwnd"], 10)
        self.assertEqual(captured["goal"], self.args["goal"])
        self.assertIs(captured["planner"], self.planner)
        self.assertIs(captured["approval_handler"], self.approval)
        self.assertEqual(progress, ["Copilot: Inspecting the selected window…"])
        self.assertIn("Screen Copilot status: completed", result)
        self.assertNotIn("hidden", result)
        self.assertNotIn("must not leak", result)

    def test_busy_and_unexpected_failures_are_redacted(self):
        for error in (CopilotBusy("private hwnd 10"), RuntimeError("secret path")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(
                        screen_copilot, "list_copilot_targets",
                        return_value=self.targets), mock.patch.object(
                        screen_copilot, "run_copilot_task", side_effect=error):
                    result = run_copilot_capability(
                        self.args, planner=self.planner,
                        approval_handler=self.approval,
                        stop_event=threading.Event())
                self.assertTrue(result.startswith("ERROR:"), result)
                self.assertNotIn(str(error), result)

    def test_cancelled_turn_does_not_scan_or_run(self):
        stop_event = threading.Event()
        stop_event.set()
        with mock.patch.object(
                screen_copilot, "list_copilot_targets") as scan, mock.patch.object(
                screen_copilot, "run_copilot_task") as run:
            result = run_copilot_capability(
                self.args, planner=self.planner, approval_handler=self.approval,
                stop_event=stop_event)

        scan.assert_not_called()
        run.assert_not_called()
        self.assertIn("cancelled", result.lower())


if __name__ == "__main__":
    unittest.main()
