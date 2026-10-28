"""Unit tests for guarded Qoder delegation.

No real screen capture, mouse movement, keyboard input, or clipboard access
occurs.
Run: python -m pytest tests/test_qoder_delegate.py -v
Or:  python tests/test_qoder_delegate.py
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault(
    "AGENTBOT_MOOD_FILE",
    os.path.join(tempfile.gettempdir(), "agentbot_test_mood.json"))

import qoder_delegate as qd

PROMPT = "Build a todo app with SQLite"
GOAL = "Server running"
WINDOW = qd.QoderWindow(
    hwnd=123, title="project - Qoder IDE", process="Qoder IDE.exe",
    left=0, top=0, width=1200, height=800)


class FakeClipboard:
    def __init__(self, initial="previous clipboard"):
        self.value = initial
        self.copies = []
        self.fail_first_copy = False
        self.fail_restore = False

    def paste(self):
        return self.value

    def copy(self, text):
        if self.fail_first_copy and not self.copies:
            raise RuntimeError("clipboard is locked")
        if self.fail_restore and self.copies:
            raise RuntimeError("clipboard restore failed")
        self.copies.append(text)
        self.value = text


class FakeKeyboard:
    def __init__(self):
        self.events = []
        self.fail_on = None
        self.on_hotkey = None

    def hotkey(self, *keys):
        self.events.append(("hotkey", keys))
        if self.on_hotkey is not None:
            self.on_hotkey(keys)
        if self.fail_on == "hotkey":
            raise RuntimeError("keyboard unavailable")

    def press(self, key):
        self.events.append(("press", key))
        if self.fail_on == "press":
            raise RuntimeError("keyboard unavailable")


class FakeOS:
    def __init__(self):
        self.foreground = WINDOW.hwnd
        self.kill = False
        self.activate_works = True
        self.calls = []

    def ensure_kill_switch(self):
        self.calls.append(("ensure",))

    def kill_switch_active(self):
        return self.kill

    def foreground_window(self):
        self.calls.append(("foreground",))
        return self.foreground

    def activate_window(self, hwnd, maximize=False):
        self.calls.append(("activate", hwnd))
        if self.activate_works:
            self.foreground = hwnd
            return True
        return False


class QoderDelegateTests(unittest.TestCase):
    def setUp(self):
        self.os = FakeOS()
        self.clipboard = FakeClipboard()
        self.keyboard = FakeKeyboard()

    def run_delegate(self, **overrides):
        options = dict(
            os_module=self.os,
            find_windows=lambda: [WINDOW],
            clipboard=self.clipboard,
            keyboard=self.keyboard,
            sleep=lambda seconds: None,
        )
        options.update(overrides)
        return qd.delegate_to_qoder(PROMPT, GOAL, **options)

    # ------------------------------------------------------------------
    # Success path
    # ------------------------------------------------------------------
    def test_success_returns_exact_message_and_submits_prompt(self):
        result = self.run_delegate()
        self.assertEqual(result, qd.SUCCESS_MESSAGE)
        self.assertEqual(self.clipboard.copies, [PROMPT, "previous clipboard"])
        self.assertEqual(
            self.keyboard.events,
            [
                ("hotkey", ("ctrl", "l")),
                ("hotkey", ("ctrl", "v")),
                ("press", "enter"),
            ],
        )

    def test_multiline_prompt_is_preserved_and_goal_is_not_pasted(self):
        prompt = "Build a server\nwith SQLite\nand a health endpoint"
        result = qd.delegate_to_qoder(
            prompt, "Build successful",
            os_module=self.os, find_windows=lambda: [WINDOW],
            clipboard=self.clipboard, keyboard=self.keyboard,
            sleep=lambda _: None)
        self.assertEqual(result, qd.SUCCESS_MESSAGE)
        self.assertEqual(self.clipboard.copies[0], prompt)
        self.assertNotIn("Build successful", self.clipboard.copies)

    # ------------------------------------------------------------------
    # Validation and discovery
    # ------------------------------------------------------------------
    def test_prompt_and_goal_validation(self):
        with self.assertRaisesRegex(qd.DelegateError, "needs a prompt"):
            qd._validate("   ", GOAL)
        with self.assertRaisesRegex(qd.DelegateError, "prompt is too long"):
            qd._validate("x" * (qd.MAX_PROMPT_CHARS + 1), GOAL)
        with self.assertRaisesRegex(qd.DelegateError, "goal is too long"):
            qd._validate("prompt", "g" * (qd.MAX_GOAL_CHARS + 1))
        with self.assertRaisesRegex(qd.DelegateError, "NUL"):
            qd._validate("prompt\x00", GOAL)
        with self.assertRaisesRegex(qd.DelegateError, "goal must be a string"):
            qd._validate("prompt", 5)
        self.assertEqual(qd._validate("prompt", ""),
                         ("prompt", qd.DEFAULT_GOAL))
        self.assertEqual(qd._validate("prompt", None),
                         ("prompt", qd.DEFAULT_GOAL))

    def test_invalid_prompt_never_touches_clipboard_or_keyboard(self):
        result = qd.delegate_to_qoder(
            "   ", GOAL, os_module=self.os,
            find_windows=lambda: [WINDOW], clipboard=self.clipboard,
            keyboard=self.keyboard, sleep=lambda _: None)
        self.assertTrue(result.startswith("ERROR:"))
        self.assertEqual(self.clipboard.copies, [])
        self.assertEqual(self.keyboard.events, [])

    def test_no_verified_qoder_window_aborts_without_input(self):
        result = self.run_delegate(find_windows=lambda: [])
        self.assertTrue(result.startswith("ERROR: no verified Qoder IDE"))
        self.assertEqual(self.clipboard.copies, [])
        self.assertEqual(self.keyboard.events, [])

    def test_find_windows_requires_the_qoder_process(self):
        class FakeNative:
            def __init__(self, title, hwnd, width, height):
                self.title = title
                self._hWnd = hwnd
                self.left = 0
                self.top = 0
                self.width = width
                self.height = height

        fake_gw = mock.Mock()
        fake_gw.getAllWindows.return_value = [
            FakeNative("AgentBot - Qoder", 10, 800, 600),
            FakeNative("Qoder docs - Chrome", 11, 1200, 900),
            FakeNative("Qoder", 12, 400, 300),
        ]
        processes = {10: "Qoder.exe", 11: "chrome.exe", 12: "Qoder.exe"}
        with mock.patch.object(qd, "_load_pygetwindow", return_value=fake_gw), \
                mock.patch.object(qd, "_window_pid", side_effect=lambda h: h), \
                mock.patch.object(qd, "_process_name",
                                  side_effect=lambda pid: processes[pid]):
            windows = qd.find_qoder_windows()

        self.assertEqual([window.hwnd for window in windows], [10, 12])

    def test_explicit_qoder_ide_window_wins_over_plain_qoder(self):
        class FakeNative:
            def __init__(self, title, hwnd, width, height):
                self.title = title
                self._hWnd = hwnd
                self.left = 0
                self.top = 0
                self.width = width
                self.height = height

        fake_gw = mock.Mock()
        fake_gw.getAllWindows.return_value = [
            FakeNative("Qoder", 10, 1600, 1000),
            FakeNative("project - Qoder IDE", 11, 800, 600),
        ]
        processes = {10: "Qoder.exe", 11: "Qoder IDE.exe"}
        with mock.patch.object(qd, "_load_pygetwindow", return_value=fake_gw), \
                mock.patch.object(qd, "_window_pid", side_effect=lambda h: h), \
                mock.patch.object(qd, "_process_name",
                                  side_effect=lambda pid: processes[pid]):
            windows = qd.find_qoder_windows()

        self.assertEqual([window.hwnd for window in windows], [11, 10])

    # ------------------------------------------------------------------
    # Safety gates
    # ------------------------------------------------------------------
    def test_focus_retries_after_transient_foreground_error(self):
        state = {"calls": 0}

        def foreground():
            state["calls"] += 1
            if state["calls"] == 1:
                raise RuntimeError("transient foreground query failure")
            return WINDOW.hwnd

        self.os.foreground_window = foreground
        result = self.run_delegate()
        self.assertEqual(result, qd.SUCCESS_MESSAGE)

    def test_focus_failure_aborts_before_clipboard_or_keyboard(self):
        self.os.foreground = 999
        self.os.activate_works = False
        result = self.run_delegate()
        self.assertIn("could not bring Qoder IDE to the foreground", result)
        self.assertEqual(self.clipboard.copies, [])
        self.assertEqual(self.keyboard.events, [])

    def test_kill_switch_blocks_before_any_input(self):
        self.os.kill = True
        result = self.run_delegate()
        self.assertTrue(result.startswith("ABORTED: kill switch"))
        self.assertEqual(self.clipboard.copies, [])
        self.assertEqual(self.keyboard.events, [])

    def test_kill_switch_after_chat_hotkey_stops_before_paste(self):
        def latch(_keys):
            self.os.kill = True

        self.keyboard.on_hotkey = latch
        result = self.run_delegate()
        self.assertTrue(result.startswith("ABORTED: kill switch"))
        self.assertEqual(
            self.keyboard.events, [("hotkey", ("ctrl", "l"))])
        self.assertEqual(self.clipboard.value, "previous clipboard")

    def test_focus_loss_after_paste_reports_unsent_prompt(self):
        def move_focus(keys):
            if keys == ("ctrl", "v"):
                self.os.foreground = 999

        self.keyboard.on_hotkey = move_focus
        result = self.run_delegate()
        self.assertIn("was not sent", result)
        self.assertEqual(
            self.keyboard.events,
            [("hotkey", ("ctrl", "l")), ("hotkey", ("ctrl", "v"))])
        self.assertEqual(self.clipboard.value, "previous clipboard")

    def test_clipboard_copy_failure_stops_before_keyboard(self):
        self.clipboard.fail_first_copy = True
        result = self.run_delegate()
        self.assertTrue(result.startswith(
            "ERROR: could not copy the prompt"))
        self.assertEqual(self.keyboard.events, [])

    def test_clipboard_restore_failure_does_not_mask_success(self):
        self.clipboard.fail_restore = True
        result = self.run_delegate()
        self.assertEqual(result, qd.SUCCESS_MESSAGE)
        self.assertEqual(self.clipboard.copies, [PROMPT])

    def test_keyboard_failure_is_reported_and_clipboard_restored(self):
        self.keyboard.fail_on = "hotkey"
        result = self.run_delegate()
        self.assertTrue(result.startswith("ERROR: keyboard input failed"))
        self.assertEqual(self.clipboard.value, "previous clipboard")

    def test_second_delegation_is_refused_while_one_is_running(self):
        self.assertTrue(qd._delegate_lock.acquire(blocking=False))
        try:
            result = self.run_delegate()
        finally:
            qd._delegate_lock.release()
        self.assertIn("already running", result)
        self.assertEqual(self.clipboard.copies, [])
        self.assertEqual(self.keyboard.events, [])


class QoderToolRegistrationTests(unittest.TestCase):
    def setUp(self):
        import agent
        self.agent = agent
        agent._clear_turn_route()
        self.addCleanup(agent._clear_turn_route)
        patcher = mock.patch.object(agent, "note_tool_result")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_tool_is_registered_and_not_group_safe(self):
        agent = self.agent
        self.assertIn("delegate_to_qoder", agent.VALID_TOOLS)
        self.assertNotIn("delegate_to_qoder", agent.SAFE_GROUP_TOOLS)
        self.assertIn("delegate_to_qoder", agent.TOOL_DESCRIPTIONS)
        manual = agent.load_skill_tool("core_tools")
        self.assertEqual(manual.count("delegate_to_qoder(prompt, goal)"), 1)

    def test_execute_tool_dispatches_prompt_and_goal(self):
        with mock.patch.object(self.agent, "delegate_to_qoder",
                               return_value="DELEGATED") as delegate:
            result = self.agent.execute_tool({
                "tool": "delegate_to_qoder",
                "input": "a todo app",
                "goal": "Server running",
            })
        self.assertEqual(result, "DELEGATED")
        delegate.assert_called_once_with("a todo app", "Server running")

    def test_execute_tool_accepts_prompt_key(self):
        with mock.patch.object(self.agent, "delegate_to_qoder",
                               return_value="DELEGATED") as delegate:
            result = self.agent.execute_tool({
                "tool": "delegate_to_qoder",
                "prompt": "a todo app",
                "goal": "Server running",
            })
        self.assertEqual(result, "DELEGATED")
        delegate.assert_called_once_with("a todo app", "Server running")

    def test_group_mode_blocks_delegation(self):
        with mock.patch.object(self.agent, "GROUP_MODE", True):
            result = self.agent.execute_tool({
                "tool": "delegate_to_qoder",
                "input": "a todo app",
                "goal": "Server running",
            })
        self.assertIn("disabled in group chats", result)

    def test_parse_action_preserves_the_goal(self):
        action, error = self.agent.parse_action(
            '{"tool":"delegate_to_qoder","input":"Build X",'
            '"goal":"Server running"}')
        self.assertIsNone(error)
        self.assertEqual(action["goal"], "Server running")

    def test_parse_action_accepts_prompt_key(self):
        action, error = self.agent.parse_action(
            '{"tool":"delegate_to_qoder","prompt":"Build X",'
            '"goal":"Server running"}')
        self.assertIsNone(error)
        self.assertEqual(action["input"], "Build X")
        self.assertEqual(action["goal"], "Server running")

    def test_queued_delegate_action_aliases_prompt(self):
        plan = (
            '{"tool":"chat","input":"hi"}\n'
            '{"tool":"delegate_to_qoder","prompt":"Build X","goal":"G"}')
        action, error = self.agent.parse_action(plan)
        self.assertIsNone(error)
        queued = action["_queued_actions"][0]
        self.assertEqual(queued["input"], "Build X")
        self.assertEqual(queued["goal"], "G")

    def test_autonomous_spec_exposes_prompt_and_goal(self):
        spec = self.agent._autonomous_tool_spec("delegate_to_qoder")
        self.assertIn('"prompt"', spec)
        self.assertIn('"goal"', spec)

    def test_router_selects_the_delegate_for_a_server_request(self):
        _, selected = self.agent._route_prompt(
            "build a complex website with a localhost server")
        self.assertIn("delegate_to_qoder",
                      {tool.name for tool in selected})

    def test_skill_is_discovered_and_loadable(self):
        skill_ids = {
            skill.skill_id
            for skill in self.agent.get_skill_manager().list_skills()
        }
        self.assertIn("local:qoder_delegate", skill_ids)
        content = self.agent.load_skill_tool("qoder_delegate")
        self.assertIn("delegate_to_qoder(prompt, goal)", content)
        self.assertIn("Ctrl+L", content)
        self.assertIn("Do not try to write the code yourself", content)


if __name__ == "__main__":
    unittest.main()
