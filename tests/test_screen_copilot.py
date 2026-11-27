import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

import screen_copilot as sc


def make_bounds(**changes):
    base = sc.WindowBounds(left=100, top=200, right=620, bottom=420)
    return replace(base, **changes)


def make_target(**changes):
    base = sc.TargetWindow(
        hwnd=101,
        title="Private fixture title",
        process=sc.ProcessIdentity(
            pid=202,
            executable=r"C:\Tools\fixture.exe",
            started_at=123456,
            elevated=False,
        ),
        visible=True,
        bounds=make_bounds(),
    )
    return replace(base, **changes)


def make_control(element_id=0, **changes):
    base = sc.Control(
        element_id=element_id,
        runtime_id=(42, 7),
        automation_id="fixtureButton",
        name="Private fixture label",
        control_type="Button",
        enabled=True,
        visible=True,
        invokable=True,
        editable=False,
    )
    return replace(base, **changes)


def make_observation(**changes):
    base = sc.Observation(
        target=make_target(),
        controls=(make_control(),),
    )
    return replace(base, **changes)


def make_screenshot(**changes):
    base = sc.Screenshot(
        png=b"private-image-bytes",
        bounds=make_bounds(),
        width=520,
        height=220,
        sha256="a" * 64,
    )
    return replace(base, **changes)


def make_envelope(**changes):
    base = sc.PlanEnvelope(
        task_id="task-1",
        target_hwnd=101,
        goal="Search for Dune and open it",
        max_steps=5,
        allowed_actions=frozenset({"click", "type_text", "vision_click"}),
        vision_enabled=True,
    )
    return replace(base, **changes)


class TargetPolicyTests(unittest.TestCase):
    def test_accepts_only_safe_visible_other_process_targets(self):
        self.assertTrue(sc.target_is_eligible(make_target(), own_pid=999))
        self.assertFalse(sc.target_is_eligible(make_target(), own_pid=202))
        self.assertFalse(
            sc.target_is_eligible(make_target(visible=False), own_pid=999))
        self.assertFalse(
            sc.target_is_eligible(make_target(title="  "), own_pid=999))
        self.assertFalse(
            sc.target_is_eligible(
                make_target(bounds=make_bounds(right=100)), own_pid=999))
        self.assertFalse(
            sc.target_is_eligible(
                make_target(bounds=make_bounds(bottom=200)), own_pid=999))
        elevated = replace(
            make_target(),
            process=replace(make_target().process, elevated=True),
        )
        self.assertFalse(sc.target_is_eligible(elevated, own_pid=999))
        for executable in (
            "chrome.exe",
            "msedge.exe",
            "firefox.exe",
            "opera.exe",
            "operagx.exe",
            "brave.exe",
            "vivaldi.exe",
            "arc.exe",
            "iexplore.exe",
        ):
            with self.subTest(executable=executable):
                browser = replace(
                    make_target(),
                    process=replace(
                        make_target().process,
                        executable=rf"C:\Browsers\{executable}",
                    ),
                )
                self.assertFalse(sc.target_is_eligible(browser, own_pid=999))

    def test_rejects_targets_outside_the_virtual_desktop(self):
        user32 = mock.Mock()
        user32.GetSystemMetrics.side_effect = {
            76: -1920,
            77: 0,
            78: 3840,
            79: 1080,
        }.get
        cases = (
            (make_bounds(left=5000, top=5000, right=5520, bottom=5220), False),
            (make_bounds(left=-4000, top=100, right=-3480, bottom=320), False),
            (make_bounds(left=-1500, top=100, right=-980, bottom=320), True),
        )
        with mock.patch.object(sc, "_win32", return_value=(None, user32, None)):
            for bounds, expected in cases:
                with self.subTest(bounds=bounds):
                    self.assertEqual(
                        sc.target_is_eligible(
                            make_target(bounds=bounds), own_pid=999),
                        expected,
                    )

    def test_clean_label_collapses_whitespace_and_caps_length(self):
        self.assertEqual(sc.clean_label("  Search   movies \n now  ", 20), "Search movies now")
        self.assertEqual(sc.clean_label("abcdef", 3), "abc")
        self.assertEqual(sc.clean_label(None, 3), "")


class IdentityAndPlannerTests(unittest.TestCase):
    def test_digest_changes_for_every_identity_field(self):
        original = make_observation()
        variants = (
            replace(original, target=replace(original.target, title="Other")),
            replace(
                original,
                target=replace(
                    original.target,
                    process=replace(original.target.process, started_at=999),
                ),
            ),
            replace(
                original,
                target=replace(original.target, bounds=make_bounds(left=101)),
            ),
            replace(original, controls=(make_control(name="Other"),)),
            replace(original, controls=(make_control(automation_id="other"),)),
            replace(original, controls=(make_control(runtime_id=(42, 8)),)),
            replace(original, controls=(make_control(control_type="MenuItem"),)),
            replace(original, controls=(make_control(invokable=False),)),
            replace(original, controls=(make_control(editable=True),)),
            replace(original, controls=(make_control(enabled=False),)),
            replace(original, controls=(make_control(visible=False),)),
            replace(original, controls=()),
        )
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotEqual(
                    sc.observation_digest(original),
                    sc.observation_digest(variant),
                )

    def test_control_hash_changes_for_identity_and_capabilities(self):
        original = make_control()
        for variant in (
            make_control(name="Other"),
            make_control(automation_id="other"),
            make_control(runtime_id=(42, 8)),
            make_control(invokable=False),
            make_control(editable=True),
        ):
            with self.subTest(variant=variant):
                self.assertNotEqual(
                    sc.control_hash(original), sc.control_hash(variant))

    def test_prompt_has_no_tools_and_marks_screen_text_untrusted(self):
        prompt = sc.build_planner_prompt(
            "Search for Dune and open it",
            make_observation(),
            make_envelope(),
        )
        self.assertIn("Search for Dune and open it", prompt)
        self.assertIn("untrusted", prompt.lower())
        self.assertIn("five", prompt.lower())
        self.assertIn('"element_id":0', prompt.replace(" ", ""))
        self.assertNotIn("execute_python", prompt)
        self.assertNotIn("tool_registry", prompt)

    def test_parser_accepts_only_exact_schemas_and_goal_text(self):
        editable = make_control(
            element_id=1,
            automation_id="fixtureSearch",
            name="Search",
            control_type="Edit",
            invokable=False,
            editable=True,
        )
        observation = replace(
            make_observation(), controls=(make_control(), editable))
        goal = "Search for Dune and open it"
        self.assertEqual(
            sc.parse_proposal(
                '{"action":"click","element_id":0}', observation, goal),
            sc.Proposal(action="click", element_id=0),
        )
        self.assertEqual(
            sc.parse_proposal(
                '{"action":"type_text","element_id":1,"text":"Dune"}',
                observation,
                goal,
            ),
            sc.Proposal(action="type_text", element_id=1, text="Dune"),
        )
        self.assertEqual(
            sc.parse_proposal(
                '{"action":"vision_click","query":"Search"}',
                observation,
                goal,
            ),
            sc.Proposal(action="vision_click", query="Search"),
        )
        self.assertEqual(
            sc.parse_proposal(
                '{"action":"done","reason":"Visible"}', observation, goal),
            sc.Proposal(action="done", reason="Visible"),
        )
        invalid = (
            '```json\n{"action":"click","element_id":0}\n```',
            '[{"action":"click","element_id":0}]',
            '{"action":"click","element_id":true}',
            '{"action":"click","element_id":9}',
            '{"action":"click","element_id":0,"x":1}',
            '{"action":"type_text","element_id":0,"text":"Dune"}',
            '{"action":"type_text","element_id":1,"text":"Arrakis"}',
            '{"action":"type_text","element_id":1,"text":""}',
            '{"action":"type_text","element_id":1,"text":"Dune\\n"}',
            '{"action":"vision_click","query":"Arrakis"}',
            '{"action":"done"}',
            'not json',
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(sc.PlannerError):
                sc.parse_proposal(raw, observation, goal)

    def test_parser_rejects_over_limit_text_and_query(self):
        editable = make_control(
            element_id=1,
            control_type="Edit",
            invokable=False,
            editable=True,
        )
        observation = replace(
            make_observation(), controls=(make_control(), editable))
        goal = "x" * 500
        with self.assertRaises(sc.PlannerError):
            sc.parse_proposal(
                json.dumps({
                    "action": "type_text",
                    "element_id": 1,
                    "text": "x" * 201,
                }),
                observation,
                goal,
            )
        with self.assertRaises(sc.PlannerError):
            sc.parse_proposal(
                json.dumps({
                    "action": "vision_click",
                    "query": "x" * 121,
                }),
                observation,
                goal,
            )


class VisionSchemaTests(unittest.TestCase):
    def test_parser_accepts_local_coordinates_and_not_found(self):
        screenshot = make_screenshot()
        self.assertEqual(
            sc.parse_vision_response('{"x":10,"y":20}', screenshot),
            sc.VisionPoint(x=10, y=20),
        )
        self.assertIsNone(sc.parse_vision_response(
            '{"action":"not_found","reason":"Absent"}', screenshot))

    def test_parser_rejects_invalid_coordinates_and_schemas(self):
        screenshot = make_screenshot()
        invalid = (
            '{"x":true,"y":20}',
            '{"x":10.5,"y":20}',
            '{"x":-1,"y":20}',
            '{"x":520,"y":20}',
            '{"x":10,"y":220}',
            '{"x":10,"y":20,"reason":"extra"}',
            '{"action":"not_found"}',
            '{"action":"not_found","reason":"Absent","x":1}',
            'not json',
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(sc.VisionError):
                sc.parse_vision_response(raw, screenshot)


class VisionAdapterTests(unittest.TestCase):
    def _desktop_with_image(self, image):
        desktop = mock.Mock()
        desktop.window.return_value.capture_as_image.return_value = image
        return desktop

    def test_capture_uses_target_hwnd_instead_of_a_screen_grab(self):
        adapter = sc.PyWinAutoUIAAdapter()
        observation = make_observation()
        image = Image.new("RGB", (520, 220), (12, 34, 56))
        desktop = self._desktop_with_image(image)
        with mock.patch.object(adapter, "observe", return_value=observation), \
             mock.patch.object(adapter, "_desktop", return_value=desktop), \
             mock.patch.object(
                 adapter, "_activate_and_get_root", return_value=101), \
             mock.patch.object(
                 adapter, "_capture_hwnd_image", create=True,
                 return_value=image) as capture_hwnd:
            screenshot = adapter.capture(observation)

        capture_hwnd.assert_called_once_with(101, observation.target.bounds)
        desktop.window.assert_not_called()
        self.assertEqual(screenshot.bounds, observation.target.bounds)
        self.assertEqual((screenshot.width, screenshot.height), (520, 220))
        self.assertTrue(screenshot.png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(
            screenshot.sha256,
            hashlib.sha256(screenshot.png).hexdigest(),
        )

    def test_keyboard_fallback_sends_literal_text_not_key_syntax(self):
        adapter = sc.PyWinAutoUIAAdapter()
        observation = make_observation()
        control = make_control(editable=True, invokable=False, control_type="Edit")
        element = mock.Mock()
        element.iface_value = None
        text = "%{F4}+^%~ 🎬"
        user32 = mock.Mock()
        user32.GetForegroundWindow.return_value = 101
        user32.GetAncestor.return_value = 101
        with mock.patch.object(
                adapter, "_validate_action",
                return_value=(observation, element, control)), \
             mock.patch.object(adapter, "_activate_and_get_root", return_value=101), \
             mock.patch.object(adapter, "_target", return_value=observation.target), \
             mock.patch.object(sc, "_win32", return_value=(None, user32, None)), \
             mock.patch.object(adapter, "_send_literal_text", create=True) as send:
            adapter.set_text(observation, control, text)

        send.assert_called_once_with(text)
        element.type_keys.assert_not_called()

    def test_literal_keyboard_input_uses_unicode_scan_codes(self):
        text = "%{F4}🎬"
        sent = []
        user32 = mock.Mock()

        def send_input(count, inputs, size):
            sent.extend(
                (item.type, item.ki.wVk, item.ki.wScan, item.ki.dwFlags)
                for item in inputs
            )
            return count

        user32.SendInput.side_effect = send_input
        with mock.patch.object(sc, "_win32", return_value=(None, user32, None)):
            sc.PyWinAutoUIAAdapter._send_literal_text(text)

        expected_units = [
            int.from_bytes(text.encode("utf-16-le")[index:index + 2], "little")
            for index in range(0, len(text.encode("utf-16-le")), 2)
        ]
        self.assertEqual(
            sent,
            [(1, 0, unit, flags) for unit in expected_units for flags in (4, 6)],
        )

    def test_capture_rejects_changed_observation(self):
        adapter = sc.PyWinAutoUIAAdapter()
        observation = make_observation()
        changed = replace(
            observation,
            target=replace(observation.target, bounds=make_bounds(left=101)),
        )
        desktop = self._desktop_with_image(Image.new("RGB", (1, 1)))
        with mock.patch.object(adapter, "observe", return_value=changed), \
             mock.patch.object(adapter, "_desktop", return_value=desktop):
            with self.assertRaises(sc.TargetDrift):
                adapter.capture(observation)
        desktop.window.assert_not_called()

    def test_capture_rejects_too_many_pixels(self):
        adapter = sc.PyWinAutoUIAAdapter()
        image = mock.Mock(size=(sc.MAX_SCREENSHOT_PIXELS + 1, 1))
        desktop = self._desktop_with_image(image)
        with mock.patch.object(
                adapter, "observe", return_value=make_observation()), \
             mock.patch.object(adapter, "_desktop", return_value=desktop), \
             mock.patch.object(
                 adapter, "_activate_and_get_root", return_value=101):
            with self.assertRaises(sc.VisionError):
                adapter.capture(make_observation())

    def test_capture_rejects_too_many_png_bytes(self):
        adapter = sc.PyWinAutoUIAAdapter()
        image = Image.new("RGB", (1, 1), (1, 2, 3))
        desktop = self._desktop_with_image(image)
        with mock.patch.object(
                adapter, "observe", return_value=make_observation()), \
             mock.patch.object(adapter, "_desktop", return_value=desktop), \
             mock.patch.object(
                 adapter, "_activate_and_get_root", return_value=101), \
             mock.patch.object(sc, "MAX_SCREENSHOT_BYTES", 8):
            with self.assertRaises(sc.VisionError):
                adapter.capture(make_observation())

    def test_capture_stop_blocks_image_capture(self):
        adapter = sc.PyWinAutoUIAAdapter()
        desktop = self._desktop_with_image(Image.new("RGB", (520, 220)))
        with mock.patch.object(
                adapter, "observe", return_value=make_observation()), \
             mock.patch.object(adapter, "_desktop", return_value=desktop), \
             mock.patch.object(
                 adapter, "_activate_and_get_root", return_value=101):
            with self.assertRaises(sc.CopilotCancelled):
                adapter.capture(make_observation(), should_stop=lambda: True)
        desktop.window.assert_not_called()

    def test_physical_click_requires_same_hash_and_foreground(self):
        adapter = sc.PyWinAutoUIAAdapter()
        observation = make_observation()
        screenshot = make_screenshot()
        user32 = mock.Mock()
        user32.GetForegroundWindow.return_value = 101
        user32.GetAncestor.return_value = 101
        with mock.patch.object(adapter, "observe", return_value=observation), \
             mock.patch.object(adapter, "capture", return_value=screenshot), \
             mock.patch.object(
                 adapter, "_activate_and_get_root", return_value=101), \
             mock.patch.object(adapter, "_target", return_value=observation.target), \
             mock.patch.object(sc, "_win32", return_value=(None, user32, None)), \
             mock.patch.object(sc, "pyautogui") as input_module:
            adapter.vision_click(
                observation,
                screenshot,
                sc.VisionPoint(x=10, y=20),
                should_stop=lambda: False,
            )
        input_module.click.assert_called_once_with(110, 220)

    def test_target_movement_during_final_activation_blocks_click(self):
        adapter = sc.PyWinAutoUIAAdapter()
        observation = make_observation()
        moved = replace(
            observation.target,
            bounds=make_bounds(left=111),
        )
        with mock.patch.object(adapter, "observe", return_value=observation), \
             mock.patch.object(adapter, "capture", return_value=make_screenshot()), \
             mock.patch.object(
                 adapter, "_activate_and_get_root", return_value=101), \
             mock.patch.object(adapter, "_target", return_value=moved), \
             mock.patch.object(sc, "pyautogui") as input_module:
            with self.assertRaises(sc.TargetDrift):
                adapter.vision_click(
                    observation, make_screenshot(), sc.VisionPoint(10, 20))
            input_module.click.assert_not_called()

    def test_foreground_loss_after_final_activation_blocks_click(self):
        adapter = sc.PyWinAutoUIAAdapter()
        observation = make_observation()
        user32 = mock.Mock()
        user32.GetForegroundWindow.return_value = 999
        user32.GetAncestor.return_value = 999
        with mock.patch.object(adapter, "observe", return_value=observation), \
             mock.patch.object(adapter, "capture", return_value=make_screenshot()), \
             mock.patch.object(
                 adapter, "_activate_and_get_root", return_value=101), \
             mock.patch.object(adapter, "_target", return_value=observation.target), \
             mock.patch.object(sc, "_win32", return_value=(None, user32, None)), \
             mock.patch.object(sc, "pyautogui") as input_module:
            with self.assertRaises(sc.TargetDrift):
                adapter.vision_click(
                    observation, make_screenshot(), sc.VisionPoint(10, 20))
            input_module.click.assert_not_called()

    def test_changed_screenshot_blocks_physical_click(self):
        adapter = sc.PyWinAutoUIAAdapter()
        observation = make_observation()
        screenshot = make_screenshot()
        changed = replace(screenshot, sha256="b" * 64)
        with mock.patch.object(adapter, "observe", return_value=observation), \
             mock.patch.object(adapter, "capture", return_value=changed), \
             mock.patch.object(sc, "pyautogui") as input_module:
            with self.assertRaises(sc.TargetDrift):
                adapter.vision_click(observation, screenshot, sc.VisionPoint(10, 20))
            input_module.click.assert_not_called()

    def test_changed_target_blocks_physical_click(self):
        adapter = sc.PyWinAutoUIAAdapter()
        observation = make_observation()
        changed = replace(
            observation,
            target=replace(observation.target, bounds=make_bounds(left=101)),
        )
        with mock.patch.object(adapter, "observe", return_value=changed), \
             mock.patch.object(adapter, "capture") as capture, \
             mock.patch.object(sc, "pyautogui") as input_module:
            with self.assertRaises(sc.TargetDrift):
                adapter.vision_click(
                    observation, make_screenshot(), sc.VisionPoint(10, 20))
            capture.assert_not_called()
            input_module.click.assert_not_called()

    def test_invalid_local_coordinates_block_physical_click(self):
        adapter = sc.PyWinAutoUIAAdapter()
        screenshot = make_screenshot()
        for point in (
            sc.VisionPoint(True, 20),
            sc.VisionPoint(10.0, 20),
            sc.VisionPoint(-1, 20),
            sc.VisionPoint(520, 20),
            sc.VisionPoint(10, 220),
        ):
            with self.subTest(point=point), \
                 mock.patch.object(adapter, "observe") as observe, \
                 mock.patch.object(sc, "pyautogui") as input_module:
                with self.assertRaises(sc.VisionError):
                    adapter.vision_click(make_observation(), screenshot, point)
                observe.assert_not_called()
                input_module.click.assert_not_called()

    def test_foreground_drift_blocks_physical_click(self):
        adapter = sc.PyWinAutoUIAAdapter()
        observation = make_observation()
        screenshot = make_screenshot()
        with mock.patch.object(adapter, "observe", return_value=observation), \
             mock.patch.object(adapter, "capture", return_value=screenshot), \
             mock.patch.object(
                 adapter, "_activate_and_get_root", return_value=999), \
             mock.patch.object(sc, "pyautogui") as input_module:
            with self.assertRaises(sc.TargetDrift):
                adapter.vision_click(observation, screenshot, sc.VisionPoint(10, 20))
            input_module.click.assert_not_called()

    def test_stop_prevents_physical_click(self):
        adapter = sc.PyWinAutoUIAAdapter()
        observation = make_observation()
        screenshot = make_screenshot()
        with mock.patch.object(adapter, "observe", return_value=observation), \
             mock.patch.object(adapter, "capture", return_value=screenshot), \
             mock.patch.object(
                 adapter, "_activate_and_get_root", return_value=101), \
             mock.patch.object(sc, "pyautogui") as input_module:
            with self.assertRaises(sc.CopilotCancelled):
                adapter.vision_click(
                    observation,
                    screenshot,
                    sc.VisionPoint(10, 20),
                    should_stop=lambda: True,
                )
            input_module.click.assert_not_called()


class VisionLocatorTests(unittest.TestCase):
    def test_locator_deletes_temp_png_after_success(self):
        png = b"private-image-bytes"
        paths = []

        def describe(value):
            path = Path(value.split("|", 1)[0])
            paths.append(path)
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), png)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            return '{"x":10,"y":20}'

        fake_agent = SimpleNamespace(describe_image=mock.Mock(side_effect=describe))
        with mock.patch.dict(sys.modules, {"agent": fake_agent}):
            result = sc.deepseek_vision_locator(png, "Search")

        self.assertEqual(result, '{"x":10,"y":20}')
        fake_agent.describe_image.assert_called_once()
        prompt = fake_agent.describe_image.call_args.args[0].split("|", 1)[1]
        self.assertIn("untrusted", prompt.lower())
        self.assertIn("JSON only", prompt)
        self.assertIn("top-left", prompt)
        self.assertIn("Query: Search", prompt)
        self.assertEqual(len(paths), 1)
        self.assertFalse(paths[0].exists())

    def test_locator_deletes_temp_png_after_provider_exception(self):
        paths = []
        failure = RuntimeError("provider unavailable")

        def describe(value):
            path = Path(value.split("|", 1)[0])
            paths.append(path)
            self.assertTrue(path.exists())
            raise failure

        fake_agent = SimpleNamespace(describe_image=mock.Mock(side_effect=describe))
        with mock.patch.dict(sys.modules, {"agent": fake_agent}):
            with self.assertRaises(RuntimeError) as caught:
                sc.deepseek_vision_locator(b"private-image-bytes", "Search")
        self.assertIs(caught.exception, failure)
        fake_agent.describe_image.assert_called_once()
        self.assertEqual(len(paths), 1)
        self.assertFalse(paths[0].exists())

    def test_provider_error_string_is_a_locator_failure_and_deletes_png(self):
        paths = []

        def describe(value):
            path = Path(value.split("|", 1)[0])
            paths.append(path)
            self.assertTrue(path.exists())
            return "ERROR: DeepSeek HTTP 500: private provider response"

        fake_agent = SimpleNamespace(describe_image=mock.Mock(side_effect=describe))
        with mock.patch.dict(sys.modules, {"agent": fake_agent}):
            with self.assertRaises(RuntimeError) as caught:
                sc.deepseek_vision_locator(b"private-image-bytes", "Search")
        self.assertNotIn("private provider response", str(caught.exception))
        self.assertEqual(len(paths), 1)
        self.assertFalse(paths[0].exists())


class FakeAdapter:
    def __init__(self, observation):
        self.observation = observation
        self.invocations = 0
        self.text_inputs = 0
        self.vision_clicks = 0
        self.captures = 0
        self.observe_calls = 0
        self.drift_after_observations = None
        self.target_drift_after_observations = None
        self.observe_error_on = None
        self.invoke_error = None
        self.text_error = None
        self.vision_error = None
        self.capture_error = None
        self.changed_after_action = True
        self.action_log = []

    def list_windows(self):
        return [self.observation.target]

    def observe(self, hwnd):
        self.observe_calls += 1
        if self.observe_error_on == self.observe_calls:
            raise sc.TargetPolicyError("unreadable")
        observation = self.observation
        if (
            self.target_drift_after_observations is not None
            and self.observe_calls >= self.target_drift_after_observations
        ):
            observation = replace(
                observation,
                target=replace(
                    observation.target,
                    process=replace(
                        observation.target.process,
                        pid=observation.target.process.pid + 1,
                    ),
                    bounds=replace(
                        observation.target.bounds,
                        left=observation.target.bounds.left + 1,
                    ),
                ),
            )
        if (
            self.drift_after_observations is not None
            and self.observe_calls >= self.drift_after_observations
        ):
            observation = replace(
                observation,
                controls=(replace(
                    observation.controls[0], name="Drifted"),),
            )
        return observation

    def _after(self):
        if self.changed_after_action:
            self.observation = replace(
                self.observation,
                controls=(replace(
                    self.observation.controls[0],
                    name=f"Changed after {self.action_log[-1]} {len(self.action_log)}",
                ),),
            )

    def invoke(self, expected, control, should_stop=None):
        if should_stop and should_stop():
            raise sc.CopilotCancelled("cancelled")
        if self.invoke_error:
            raise self.invoke_error
        self.action_log.append("click")
        self.invocations += 1
        self._after()

    def set_text(self, expected, control, text, should_stop=None):
        if should_stop and should_stop():
            raise sc.CopilotCancelled("cancelled")
        if self.text_error:
            raise self.text_error
        self.action_log.append("type_text")
        self.text_inputs += 1
        self._after()

    def capture(self, expected, should_stop=None):
        if should_stop and should_stop():
            raise sc.CopilotCancelled("cancelled")
        if self.capture_error:
            raise self.capture_error
        self.captures += 1
        bounds = expected.target.bounds
        return make_screenshot(
            bounds=bounds,
            width=bounds.width,
            height=bounds.height,
        )

    def vision_click(self, expected, screenshot, point, should_stop=None):
        if should_stop and should_stop():
            raise sc.CopilotCancelled("cancelled")
        if self.vision_error:
            raise self.vision_error
        self.action_log.append("vision_click")
        self.vision_clicks += 1
        self._after()


class ServiceTests(unittest.TestCase):
    def setUp(self):
        shutdown = mock.patch.object(sc, "_shutdown_event", threading.Event())
        shutdown.start()
        self.addCleanup(shutdown.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.audit_path = Path(self.directory.name) / "audit.jsonl"
        self.audit = sc.AuditWriter(self.audit_path)
        self.stop = threading.Event()
        self.approvals = []
        self.progress = []

    def service(
        self,
        adapter,
        proposals,
        approval=True,
        vision=None,
        audit=None,
        handler=None,
    ):
        responses = iter(proposals)

        def planner(prompt):
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return response

        if handler is None:
            def handler(payload):
                self.approvals.append(payload)
                future = Future()
                future.set_result(approval)
                return future

        return sc.ScreenCopilotService(
            adapter=adapter,
            audit=audit or self.audit,
            planner=mock.Mock(side_effect=planner),
            approval_handler=handler,
            vision_locator=vision,
            stop_event=self.stop,
            progress_handler=self.progress.append,
        )

    @staticmethod
    def two_control_observation():
        editable = make_control(
            element_id=1,
            automation_id="fixtureSearch",
            name="Search",
            control_type="Edit",
            invokable=False,
            editable=True,
        )
        return replace(
            make_observation(), controls=(make_control(), editable))

    def test_one_approval_covers_two_adaptive_actions(self):
        adapter = FakeAdapter(self.two_control_observation())
        service = self.service(adapter, (
            '{"action":"type_text","element_id":1,"text":"Dune"}',
            '{"action":"click","element_id":0}',
            '{"action":"done","reason":"Visible"}',
        ))
        result = service.run("Search for Dune and open it", 101)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.steps, 2)
        self.assertTrue(result.changed)
        self.assertEqual(adapter.text_inputs, 1)
        self.assertEqual(adapter.invocations, 1)
        self.assertEqual(adapter.action_log, ["type_text", "click"])
        self.assertEqual(len(self.approvals), 1)
        self.assertEqual(self.approvals[0]["max_steps"], 5)
        self.assertEqual(
            self.approvals[0]["allowed_actions"],
            ["click", "type_text"],
        )
        self.assertIn("not independent proof", result.summary)

    def test_done_completes_without_actions(self):
        adapter = FakeAdapter(make_observation())
        service = self.service(adapter, (
            '{"action":"done","reason":"Visible"}',
        ))
        result = service.run("Search for Dune and open it", 101)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.steps, 0)
        self.assertEqual(adapter.action_log, [])
        self.assertEqual(len(self.approvals), 1)

    def test_five_actions_are_the_hard_maximum(self):
        adapter = FakeAdapter(make_observation())
        proposals = tuple(
            '{"action":"click","element_id":0}' for _ in range(5)
        ) + ('{"action":"done","reason":"Visible"}',)
        service = self.service(adapter, proposals)
        result = service.run("Search for Dune and open it", 101)
        self.assertEqual(result.status, "max_steps_reached")
        self.assertEqual(result.steps, 5)
        self.assertEqual(adapter.invocations, 5)
        self.assertEqual(service.planner.call_count, 5)

    def test_deny_timeout_missing_error_and_prestart_cancel_do_nothing(self):
        cases = (
            ("denied", False, "handler", False),
            ("timeout", True, "timeout", False),
            ("denied", True, "missing", False),
            ("denied", True, "error", False),
            ("cancelled", True, "handler", True),
        )
        for expected, approval, handler_kind, prestart_cancel in cases:
            with self.subTest(status=expected, handler=handler_kind):
                adapter = FakeAdapter(make_observation())
                self.stop = threading.Event()
                if prestart_cancel:
                    self.stop.set()
                if handler_kind == "timeout":
                    handler = lambda payload: Future()
                elif handler_kind == "missing":
                    handler = None
                elif handler_kind == "error":
                    def handler(payload):
                        raise RuntimeError("private approval error")
                else:
                    handler = None
                if handler_kind == "missing":
                    service = sc.ScreenCopilotService(
                        adapter=adapter,
                        audit=self.audit,
                        planner=lambda prompt: '{"action":"click","element_id":0}',
                        approval_handler=None,
                        stop_event=self.stop,
                    )
                else:
                    service = self.service(
                        adapter,
                        ('{"action":"click","element_id":0}',),
                        approval=approval,
                        handler=handler,
                    )
                with mock.patch.object(
                        sc, "APPROVAL_TIMEOUT_SECONDS", 0.01), \
                     mock.patch.object(sc, "CANCEL_POLL_SECONDS", 0.001):
                    result = service.run("Search for Dune and open it", 101)
                self.assertEqual(result.status, expected)
                self.assertEqual(result.steps, 0)
                self.assertEqual(adapter.action_log, [])

    def test_out_of_goal_typing_and_vision_are_out_of_envelope(self):
        cases = (
            (
                self.two_control_observation(),
                '{"action":"type_text","element_id":1,"text":"Arrakis"}',
                None,
                "text_inputs",
            ),
            (
                make_observation(),
                '{"action":"vision_click","query":"Arrakis"}',
                lambda png, query: '{"x":10,"y":20}',
                "captures",
            ),
        )
        for observation, proposal, vision, counter in cases:
            with self.subTest(action=proposal):
                adapter = FakeAdapter(observation)
                service = self.service(adapter, (proposal,), vision=vision)
                result = service.run("Search for Dune and open it", 101)
                self.assertEqual(result.status, "out_of_envelope")
                self.assertEqual(getattr(adapter, counter), 0)
                self.assertEqual(adapter.action_log, [])

    def test_vision_without_approved_locator_is_out_of_envelope(self):
        adapter = FakeAdapter(make_observation())
        service = self.service(adapter, (
            '{"action":"vision_click","query":"Search"}',
        ))
        result = service.run("Search for Dune and open it", 101)
        self.assertEqual(result.status, "out_of_envelope")
        self.assertEqual(adapter.captures, 0)

    def test_target_and_control_drift_block_uia_input(self):
        cases = (
            ("control", 3),
            ("target", 2),
            ("target", 3),
        )
        for kind, drift_after in cases:
            with self.subTest(kind=kind, drift_after=drift_after):
                adapter = FakeAdapter(make_observation())
                if kind == "control":
                    adapter.drift_after_observations = drift_after
                else:
                    adapter.target_drift_after_observations = drift_after
                service = self.service(adapter, (
                    '{"action":"click","element_id":0}',
                ))
                result = service.run("Search for Dune and open it", 101)
                self.assertEqual(result.status, "drift_blocked")
                self.assertEqual(adapter.invocations, 0)

    def test_cancellation_after_execution_audit_blocks_input(self):
        adapter = FakeAdapter(make_observation())
        real_append = self.audit.append

        def append(record):
            real_append(record)
            if record.get("event") == "execution_attempt":
                self.stop.set()

        service = self.service(
            adapter,
            ('{"action":"click","element_id":0}',),
            audit=mock.Mock(append=mock.Mock(side_effect=append)),
        )
        result = service.run("Search for Dune and open it", 101)
        self.assertEqual(result.status, "cancelled_before_action")
        self.assertEqual(adapter.invocations, 0)

    def test_stop_at_action_boundary_is_audited_without_input(self):
        adapter = FakeAdapter(make_observation())
        real_append = self.audit.append

        def append(record):
            real_append(record)
            if record.get("event") == "step_proposal":
                self.stop.set()

        service = self.service(
            adapter,
            ('{"action":"click","element_id":0}',),
            audit=mock.Mock(append=mock.Mock(side_effect=append)),
        )
        result = service.run("Search for Dune and open it", 101)
        self.assertEqual(result.status, "cancelled_before_action")
        self.assertEqual(adapter.invocations, 0)
        events = [
            json.loads(line)["event"]
            for line in self.audit_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(events[-2:], ["step_outcome", "outcome"])

    def test_kill_switch_at_action_boundary_blocks_input(self):
        state = {"active": False}
        fake_os_control = SimpleNamespace(
            kill_switch_active=lambda: state["active"])
        adapter = FakeAdapter(make_observation())
        real_append = self.audit.append

        def append(record):
            real_append(record)
            if record.get("event") == "step_proposal":
                state["active"] = True

        service = self.service(
            adapter,
            ('{"action":"click","element_id":0}',),
            audit=mock.Mock(append=mock.Mock(side_effect=append)),
        )
        with mock.patch.dict(sys.modules, {"os_control": fake_os_control}):
            result = service.run("Search for Dune and open it", 101)
        self.assertEqual(result.status, "cancelled_before_action")
        self.assertEqual(adapter.invocations, 0)

    def test_cancellation_after_vision_request_is_audited_without_click(self):
        adapter = FakeAdapter(make_observation())
        service = None

        def vision(png, query):
            service.cancel()
            return '{"x":10,"y":20}'

        service = self.service(
            adapter,
            ('{"action":"vision_click","query":"Search"}',),
            vision=vision,
        )
        result = service.run("Search for Dune and open it", 101)
        self.assertEqual(result.status, "cancelled_before_action")
        self.assertEqual(adapter.captures, 1)
        self.assertEqual(adapter.vision_clicks, 0)
        events = [
            json.loads(line)["event"]
            for line in self.audit_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(events[-2:], ["step_outcome", "outcome"])

    def test_pre_execution_audit_failure_blocks_input(self):
        adapter = FakeAdapter(make_observation())
        real_append = self.audit.append

        def append(record):
            if record.get("event") == "execution_attempt":
                raise sc.AuditError("private audit error")
            real_append(record)

        service = self.service(
            adapter,
            ('{"action":"click","element_id":0}',),
            audit=mock.Mock(append=mock.Mock(side_effect=append)),
        )
        result = service.run("Search for Dune and open it", 101)
        self.assertEqual(result.status, "audit_failed")
        self.assertEqual(adapter.invocations, 0)

    def test_outcome_audit_failure_after_input_is_reported_honestly(self):
        adapter = FakeAdapter(make_observation())
        real_append = self.audit.append

        def append(record):
            if record.get("event") == "step_outcome":
                raise sc.AuditError("private audit error")
            real_append(record)

        service = self.service(
            adapter,
            ('{"action":"click","element_id":0}',),
            audit=mock.Mock(append=mock.Mock(side_effect=append)),
        )
        result = service.run("Search for Dune and open it", 101)
        self.assertEqual(result.status, "audit_failed_after_execution")
        self.assertIn("may have occurred", result.summary)
        self.assertEqual(adapter.invocations, 1)

    def test_late_audit_failure_after_an_action_is_reported_honestly(self):
        adapter = FakeAdapter(make_observation())
        real_append = self.audit.append

        def append(record):
            if record.get("event") == "planner_done":
                raise sc.AuditError("private audit error")
            real_append(record)

        service = self.service(
            adapter,
            ('{"action":"click","element_id":0}',
             '{"action":"done","reason":"Visible"}'),
            audit=mock.Mock(append=mock.Mock(side_effect=append)),
        )
        result = service.run("Search for Dune and open it", 101)
        self.assertEqual(result.status, "audit_failed_after_execution")
        self.assertEqual(result.steps, 1)
        self.assertIn("may have occurred", result.summary)
        self.assertEqual(adapter.invocations, 1)

    def test_post_action_changed_states_remain_distinct(self):
        for changed_after, error_on, expected_status, expected_changed in (
            (False, None, "completed", False),
            (True, None, "completed", True),
            (True, 4, "drift_blocked", None),
        ):
            with self.subTest(changed=expected_changed):
                adapter = FakeAdapter(make_observation())
                adapter.changed_after_action = changed_after
                adapter.observe_error_on = error_on
                service = self.service(adapter, (
                    '{"action":"click","element_id":0}',
                    '{"action":"done","reason":"Visible"}',
                ))
                result = service.run("Search for Dune and open it", 101)
                self.assertEqual(result.status, expected_status)
                self.assertIs(result.changed, expected_changed)
                self.assertEqual(adapter.invocations, 1)

    def test_action_and_vision_failures_use_fixed_statuses_without_retry(self):
        cases = (
            ("invoke_error", "click", None, "click_failed", "invocations"),
            ("text_error", "type_text", None, "type_failed", "text_inputs"),
            ("capture_error", "vision_click", "ok", "vision_capture_failed", "captures"),
            ("invoke_error", "vision_click", "locator_error", "vision_locator_failed", "captures"),
            ("invoke_error", "vision_click", "not_found", "vision_not_found", "captures"),
            ("invoke_error", "vision_click", "invalid", "vision_invalid", "captures"),
            ("vision_error", "vision_click", "ok", "vision_click_failed", "vision_clicks"),
        )
        for error_attr, action, vision_kind, expected, counter in cases:
            with self.subTest(status=expected):
                observation = self.two_control_observation()
                adapter = FakeAdapter(observation)
                setattr(adapter, error_attr, RuntimeError("private failure"))
                if action == "click":
                    proposal = '{"action":"click","element_id":0}'
                elif action == "type_text":
                    proposal = '{"action":"type_text","element_id":1,"text":"Dune"}'
                else:
                    proposal = '{"action":"vision_click","query":"Search"}'
                if vision_kind == "ok":
                    vision = lambda png, query: '{"x":10,"y":20}'
                elif vision_kind == "locator_error":
                    def vision(png, query):
                        raise RuntimeError("private vision failure")
                elif vision_kind == "not_found":
                    vision = lambda png, query: '{"action":"not_found","reason":"Absent"}'
                elif vision_kind == "invalid":
                    vision = lambda png, query: '{"x":9999,"y":20}'
                else:
                    vision = None
                service = self.service(adapter, (proposal,), vision=vision)
                result = service.run("Search for Dune and open it", 101)
                self.assertEqual(result.status, expected)
                self.assertLessEqual(getattr(adapter, counter), 1)
                self.assertEqual(service.planner.call_count, 1)

    def test_planner_failure_and_invalid_proposal_are_fixed_statuses(self):
        for proposal, expected in (
            (RuntimeError("private planner failure"), "planner_failed"),
            ("not json", "invalid_proposal"),
        ):
            with self.subTest(status=expected):
                adapter = FakeAdapter(make_observation())
                service = self.service(adapter, (proposal,))
                result = service.run("Search for Dune and open it", 101)
                self.assertEqual(result.status, expected)
                self.assertEqual(adapter.action_log, [])

    def test_progress_events_contain_only_safe_fields(self):
        adapter = FakeAdapter(self.two_control_observation())
        service = self.service(adapter, (
            '{"action":"type_text","element_id":1,"text":"Dune"}',
            '{"action":"done","reason":"Visible"}',
        ))
        service.run("Search for Dune and open it", 101)
        self.assertTrue(self.progress)
        for event in self.progress:
            self.assertLessEqual(
                set(event), {"event", "step", "action", "status"})
            rendered = json.dumps(event)
            for private in (
                "Private fixture title",
                "Private fixture label",
                "Search for Dune",
                "Dune",
            ):
                self.assertNotIn(private, rendered)

    def test_cancellation_after_planner_and_module_shutdown_block_input(self):
        for shutdown in (False, True):
            with self.subTest(shutdown=shutdown):
                adapter = FakeAdapter(make_observation())
                service = self.service(adapter, (
                    '{"action":"click","element_id":0}',
                ))
                original_planner = service.planner.side_effect

                def planner(prompt):
                    response = original_planner(prompt)
                    if shutdown:
                        sc._shutdown_event.set()
                    else:
                        service.cancel()
                    return response

                service.planner.side_effect = planner
                with mock.patch.object(sc, "_shutdown_event", threading.Event()):
                    result = service.run("Search for Dune and open it", 101)
                self.assertEqual(result.status, "cancelled")
                self.assertEqual(adapter.invocations, 0)

    def test_kill_switch_blocks_before_approval(self):
        fake_os_control = SimpleNamespace(kill_switch_active=lambda: True)
        adapter = FakeAdapter(make_observation())
        service = self.service(adapter, (
            '{"action":"click","element_id":0}',
        ))
        with mock.patch.dict(sys.modules, {"os_control": fake_os_control}):
            result = service.run("Search for Dune and open it", 101)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(adapter.invocations, 0)
        self.assertEqual(self.approvals, [])

    def test_second_concurrent_task_raises_busy(self):
        entered = threading.Event()
        release = threading.Event()
        results = []

        def planner(prompt):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test planner was not released")
            return '{"action":"done","reason":"Visible"}'

        def approve(payload):
            future = Future()
            future.set_result(True)
            return future

        adapter = FakeAdapter(make_observation())

        def run_first():
            try:
                results.append(sc.run_copilot_task(
                    "Search for Dune and open it",
                    101,
                    planner,
                    approve,
                    adapter=adapter,
                    audit=self.audit,
                    stop_event=self.stop,
                ))
            except Exception as exc:
                results.append(exc)

        thread = threading.Thread(target=run_first)
        thread.start()
        try:
            self.assertTrue(entered.wait(timeout=5))
            with self.assertRaises(sc.CopilotBusy):
                sc.run_copilot_task(
                    "Search for Dune and open it",
                    101,
                    planner,
                    approve,
                    adapter=adapter,
                    audit=self.audit,
                    stop_event=self.stop,
                )
        finally:
            release.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].status, "completed")

    def test_audit_order_and_privacy(self):
        adapter = FakeAdapter(self.two_control_observation())
        service = self.service(adapter, (
            '{"action":"type_text","element_id":1,"text":"Dune"}',
            '{"action":"click","element_id":0}',
            '{"action":"done","reason":"Visible"}',
        ))
        service.run("Search for Dune and open it", 101)
        records = [
            json.loads(line)
            for line in self.audit_path.read_text(encoding="utf-8").splitlines()
        ]
        events = [record["event"] for record in records]
        self.assertEqual(events.count("approval_requested"), 1)
        self.assertEqual(events.count("execution_attempt"), 2)
        self.assertEqual(events.count("step_outcome"), 2)
        self.assertEqual(events[-2:], ["planner_done", "outcome"])
        for index, event in enumerate(events):
            if event == "execution_attempt":
                self.assertEqual(events[index - 1], "step_proposal")
        rendered = self.audit_path.read_text(encoding="utf-8")
        for private in (
            "Private fixture title",
            "Private fixture label",
            "Search for Dune and open it",
            "Dune",
            "Search",
        ):
            self.assertNotIn(private, rendered)

    def test_default_audit_path_uses_agent_memory(self):
        fake_config = SimpleNamespace(
            memory_path=lambda *parts: str(Path("memory").joinpath(*parts)))
        with mock.patch.dict(sys.modules, {"config": fake_config}):
            self.assertEqual(
                sc.default_audit_path(),
                Path("memory", "copilot", "audit.jsonl"),
            )


class AuditTests(unittest.TestCase):
    def test_audit_contains_hashes_but_no_raw_screen_or_goal_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "copilot" / "audit.jsonl"
            writer = sc.AuditWriter(path)
            writer.append({
                "task_id": "task-1",
                "event": "step_proposal",
                "title_hash": sc.observation_digest(make_observation()),
                "control_hash": sc.control_hash(make_control()),
                "goal": "Private fixture label",
                "raw_title": "Private fixture title",
                "text": "Dune",
                "query": "Search",
                "response": "private model response",
                "image": b"private-image-bytes",
                "nested": {"name": "Private nested label", "safe": 7},
            })
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["schema"], 1)
            self.assertIn("ts", records[0])
            self.assertEqual(records[0]["nested"], {"safe": 7})
            text = path.read_text(encoding="utf-8")
            for private in (
                "Private fixture title",
                "Private fixture label",
                "Private nested label",
                "Dune",
                "Search",
                "private model response",
                "private-image-bytes",
            ):
                self.assertNotIn(private, text)

    def test_audit_failure_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            blocker = Path(directory) / "not-a-directory"
            blocker.write_text("x", encoding="utf-8")
            with self.assertRaises(sc.AuditError):
                sc.AuditWriter(blocker / "audit.jsonl").append({
                    "task_id": "task-1",
                    "event": "inspection",
                })


class AdapterContractTests(unittest.TestCase):
    def test_production_adapter_is_exposed(self):
        self.assertTrue(hasattr(sc, "PyWinAutoUIAAdapter"))
        self.assertTrue(hasattr(sc, "process_identity"))

    def test_stopped_scan_does_not_start_enumeration(self):
        user32 = mock.Mock()
        adapter = sc.PyWinAutoUIAAdapter(own_pid=999)
        with mock.patch.object(sc, "_win32", return_value=(None, user32, None)):
            self.assertEqual(
                adapter.list_windows(should_stop=lambda: True), [])
        user32.EnumWindows.assert_not_called()

    def test_scan_stops_between_enumerated_windows(self):
        stopped = threading.Event()
        user32 = mock.Mock()
        callback_results = []
        metadata_calls = 0

        def enumerate_windows(callback, _):
            callback_results.append(callback(1, 0))
            callback_results.append(callback(2, 0))
            if callback_results[-1]:
                callback_results.append(callback(3, 0))

        def metadata(_hwnd):
            nonlocal metadata_calls
            metadata_calls += 1
            if metadata_calls == 2:
                stopped.set()
            return 202, "Fixture", True, make_bounds()

        user32.EnumWindows.side_effect = enumerate_windows
        adapter = sc.PyWinAutoUIAAdapter(own_pid=999)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                sc, "_win32", return_value=(None, user32, None)))
            stack.enter_context(mock.patch.object(
                sc, "_window_metadata", side_effect=metadata))
            stack.enter_context(mock.patch.object(
                sc, "process_identity", return_value=make_target().process))
            stack.enter_context(mock.patch.object(
                sc, "target_is_eligible", return_value=True))
            targets = adapter.list_windows(should_stop=stopped.is_set)
        self.assertEqual(callback_results, [True, False])
        self.assertEqual(len(targets), 2)

    def test_target_listing_forwards_the_stop_event(self):
        stop_event = threading.Event()
        stop_event.set()
        adapter = mock.Mock()
        adapter.list_windows.side_effect = (
            lambda should_stop=None:
            [] if should_stop and should_stop() else [make_target()])
        self.assertEqual(
            sc.list_copilot_targets(adapter=adapter, stop_event=stop_event), [])
        adapter.list_windows.assert_called_once_with(
            should_stop=stop_event.is_set)


DUMMY_SCRIPT = """
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.Text = "{title}"
$form.Width = 520
$form.Height = 260
$form.StartPosition = "CenterScreen"
$form.TopMost = $true
$script:count = 0
$search = New-Object System.Windows.Forms.TextBox
$search.Name = "copilotSearch"
$search.Text = "private-existing-value"
$search.Width = 300
$search.Top = 30
$search.Left = 20
$button = New-Object System.Windows.Forms.Button
$button.Name = "copilotTestButton"
$button.Text = "Open Dune"
$button.Width = 180
$button.Top = 80
$button.Left = 20
$label = New-Object System.Windows.Forms.Label
$label.Name = "copilotTestResult"
$label.Text = "0"
$label.Width = 360
$label.Top = 140
$label.Left = 20
$button.Add_Click({{ $script:count++; $label.Text = [string]$script:count }})
$form.Controls.Add($search)
$form.Controls.Add($button)
$form.Controls.Add($label)
[System.Windows.Forms.Application]::Run($form)
"""


class LiveFixture:
    def setUp(self):
        shutdown = mock.patch.object(sc, "_shutdown_event", threading.Event())
        shutdown.start()
        self.addCleanup(shutdown.stop)
        self.title = "Copilot Fixture " + uuid.uuid4().hex[:8]
        script_path = None
        with tempfile.NamedTemporaryFile(
            "w", suffix=".ps1", delete=False, encoding="utf-8") as handle:
            handle.write(DUMMY_SCRIPT.format(title=self.title))
            script_path = Path(handle.name)
        self.script_path = script_path
        try:
            self.proc = subprocess.Popen([
                "powershell.exe",
                "-NoProfile",
                "-STA",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-File",
                str(script_path),
            ])
        except Exception:
            script_path.unlink(missing_ok=True)
            raise
        self.addCleanup(self._cleanup)
        self.adapter = sc.PyWinAutoUIAAdapter()
        self.target = self._wait_for_target()
        self.observation = self.adapter.observe(self.target.hwnd)

    def _cleanup(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.script_path.unlink(missing_ok=True)

    def _wait_for_target(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                return next(
                    target for target in self.adapter.list_windows()
                    if target.title == self.title
                )
            except Exception:
                time.sleep(0.25)
        self.fail("copilot fixture window did not appear")

    def _control(self, automation_id, observation=None):
        observation = observation or self.observation
        return next(
            control for control in observation.controls
            if control.automation_id == automation_id
        )

    def _window(self):
        return self.adapter._desktop().window(handle=self.target.hwnd)

    def _value(self, automation_id):
        element = next(
            element for element in self._window().iter_descendants()
            if element.element_info.automation_id == automation_id
        )
        try:
            return element.iface_value.CurrentValue
        except Exception:
            return element.window_text()


@unittest.skipUnless(sys.platform == "win32", "Windows only")
@unittest.skipUnless(
    getattr(sc, "PYWINAUTO_AVAILABLE", False),
    "pywinauto is not installed",
)
class LiveAdapterTests(LiveFixture, unittest.TestCase):
    def test_observes_button_and_editable_search_without_value(self):
        button = self._control("copilotTestButton")
        search = self._control("copilotSearch")
        self.assertEqual(button.control_type, "Button")
        self.assertTrue(button.invokable)
        self.assertFalse(button.editable)
        self.assertEqual(search.control_type, "Edit")
        self.assertFalse(search.invokable)
        self.assertTrue(search.editable)
        self.assertFalse(any(hasattr(control, "value") for control in self.observation.controls))
        self.assertNotIn("private-existing-value", repr(self.observation.controls))
        self.assertNotIn(
            "private-existing-value",
            sc.observation_digest(self.observation),
        )

    def test_capture_matches_fixture_bounds(self):
        screenshot = self.adapter.capture(
            self.observation, should_stop=lambda: False)
        self.assertEqual(screenshot.bounds, self.target.bounds)
        self.assertEqual(screenshot.width, self.target.bounds.width)
        self.assertEqual(screenshot.height, self.target.bounds.height)
        self.assertEqual(
            screenshot.sha256,
            hashlib.sha256(screenshot.png).hexdigest(),
        )

    def test_invokes_fixture_button_once(self):
        button = self._control("copilotTestButton")
        self.adapter.invoke(
            self.observation, button, should_stop=lambda: False)
        self.assertEqual(self._value("copilotTestResult"), "1")

    def test_sets_fixture_search_text(self):
        search = self._control("copilotSearch")
        self.adapter.set_text(
            self.observation, search, "Dune", should_stop=lambda: False)
        self.assertEqual(self._value("copilotSearch"), "Dune")

    def test_stop_prevents_invoke_and_typing(self):
        button = self._control("copilotTestButton")
        search = self._control("copilotSearch")
        before = sc.observation_digest(self.observation)
        with self.assertRaises(sc.CopilotCancelled):
            self.adapter.invoke(
                self.observation, button, should_stop=lambda: True)
        with self.assertRaises(sc.CopilotCancelled):
            self.adapter.set_text(
                self.observation, search, "Dune", should_stop=lambda: True)
        self.assertEqual(self._value("copilotTestResult"), "0")
        self.assertEqual(self._value("copilotSearch"), "private-existing-value")
        self.assertEqual(
            sc.observation_digest(self.adapter.observe(self.target.hwnd)), before)


@unittest.skipUnless(sys.platform == "win32", "Windows only")
@unittest.skipUnless(
    getattr(sc, "PYWINAUTO_AVAILABLE", False),
    "pywinauto is not installed",
)
class LiveServiceTests(LiveFixture, unittest.TestCase):
    def test_one_approval_runs_two_actions_and_denial_runs_none(self):
        search = self._control("copilotSearch")
        button = self._control("copilotTestButton")
        goal = "Search for Dune and open it"
        proposals = [
            json.dumps({
                "action": "type_text",
                "element_id": search.element_id,
                "text": "Dune",
            }),
            json.dumps({
                "action": "click",
                "element_id": button.element_id,
            }),
            json.dumps({
                "action": "done",
                "reason": "The requested result is visible",
            }),
        ]
        approved_requests = []

        def planner(_prompt):
            return proposals.pop(0)

        def approve(payload):
            approved_requests.append(payload)
            future = Future()
            future.set_result(True)
            return future

        with tempfile.TemporaryDirectory() as directory:
            audit_path = Path(directory) / "audit.jsonl"
            audit = sc.AuditWriter(audit_path)
            approved_adapter = mock.Mock(wraps=self.adapter)
            result = sc.run_copilot_task(
                goal=goal,
                hwnd=self.target.hwnd,
                planner=planner,
                approval_handler=approve,
                adapter=approved_adapter,
                audit=audit,
            )
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.steps, 2)
            self.assertEqual(len(approved_requests), 1)
            self.assertEqual(proposals, [])
            approved_adapter.set_text.assert_called_once()
            approved_adapter.invoke.assert_called_once()
            self.assertEqual(self._value("copilotSearch"), "Dune")
            self.assertEqual(self._value("copilotTestResult"), "1")

            self.adapter.set_text(
                self.observation,
                search,
                "private-existing-value",
                should_stop=lambda: False,
            )
            self.assertEqual(
                self._value("copilotSearch"), "private-existing-value")

            denied_requests = []
            denied_planner = mock.Mock()
            denied_adapter = mock.Mock(wraps=self.adapter)

            def deny(payload):
                denied_requests.append(payload)
                future = Future()
                future.set_result(False)
                return future

            denied = sc.run_copilot_task(
                goal=goal,
                hwnd=self.target.hwnd,
                planner=denied_planner,
                approval_handler=deny,
                adapter=denied_adapter,
                audit=audit,
            )
            self.assertEqual(denied.status, "denied")
            self.assertEqual(denied.steps, 0)
            self.assertEqual(len(denied_requests), 1)
            denied_planner.assert_not_called()
            denied_adapter.set_text.assert_not_called()
            denied_adapter.invoke.assert_not_called()
            self.assertEqual(
                self._value("copilotSearch"), "private-existing-value")
            self.assertEqual(self._value("copilotTestResult"), "1")

            current = self.adapter.observe(self.target.hwnd)
            screenshot = self.adapter.capture(
                current, should_stop=lambda: False)
            self.assertEqual(screenshot.bounds, self.target.bounds)
            self.assertEqual(screenshot.width, self.target.bounds.width)
            self.assertEqual(screenshot.height, self.target.bounds.height)

            audit_text = audit_path.read_text(encoding="utf-8")
            records = [
                json.loads(line) for line in audit_text.splitlines()
                if line.strip()
            ]
            approved_records = [
                record for record in records
                if record["task_id"] == result.task_id
            ]
            attempts = [
                record for record in approved_records
                if record["event"] == "execution_attempt"
            ]
            self.assertEqual(
                [record["action"] for record in attempts],
                ["type_text", "click"],
            )
            for action in ("type_text", "click"):
                attempt_index = next(
                    index for index, record in enumerate(approved_records)
                    if record["event"] == "execution_attempt"
                    and record["action"] == action
                )
                outcome_index = next(
                    index for index, record in enumerate(approved_records)
                    if record["event"] == "step_outcome"
                    and record["action"] == action
                )
                self.assertLess(attempt_index, outcome_index)
            denied_records = [
                record for record in records
                if record["task_id"] == denied.task_id
            ]
            self.assertFalse(any(
                record["event"] == "execution_attempt"
                for record in denied_records
            ))
            for private_text in (
                self.title, "Open Dune", goal, "Dune",
                "private-existing-value", "copilotSearch",
                "copilotTestButton", "The requested result is visible",
            ):
                self.assertNotIn(private_text, audit_text)


if __name__ == "__main__":
    unittest.main()
