"""Unit tests for browser_agent.py.

No Playwright installation is required: the pruner output, executor, vision
gates and service loop are driven with fake pages/locators.

Run: python -m pytest tests/test_browser_agent.py -v
Or:  python tests/test_browser_agent.py
"""
import asyncio
import concurrent.futures
import hashlib
import json
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import browser_agent as b

HEX = "a" * 32
SEL = f'[data-agent-id="{HEX}-0"]'
ORIGIN = ("https", "example.com", 443)
BASE = "https://example.com/"
FINISH = json.dumps({
    "action": "finish",
    "summary": "Task complete",
    "evidence": "The page shows a confirmation message.",
})


def raw_observation(label="Sign in", disabled=False, field_type="", checked=None,
                    status=None, frames=0):
    """Shape returned by the in-page extractor (before Python formatting)."""
    return {
        "elements": [{
            "id": f"{HEX}-0",
            "tag": "button" if field_type != "password" else "input",
            "type": field_type,
            "role": "",
            "label": label,
            "disabled": disabled,
            "checked": checked,
            "required": False,
            "options": [],
        }],
        "truncated": False,
        "status": list(status or []),
        "title": "Example",
        "frames": frames,
    }


def make_observation(url=BASE, selector=SEL, label="Sign in", disabled=False,
                     field_type="", checked=None):
    return {
        "html": f"<control>{label}</control>",
        "status": "",
        "targets": {
            selector: {
                "label": label,
                "tag": "button",
                "type": field_type,
                "role": "",
                "disabled": disabled,
                "checked": checked,
                "required": False,
            }
        },
        "truncated": False,
        "url": url,
        "title": "Example",
        "frames": 0,
    }


def action_json(kind, **kwargs):
    payload = {"action": kind}
    payload.update(kwargs)
    return json.dumps(payload)


class FakeMouse:
    def __init__(self):
        self.clicks = []

    async def click(self, x, y, **kwargs):
        self.clicks.append((x, y))


class FakeLocator:
    def __init__(self, count=1, trial_error=None, action_error=None):
        self._count = count
        self.trial_error = trial_error
        self.action_error = action_error
        self.calls = []
        self.filled = []
        self.selected = []
        self.checked = []

    async def count(self):
        return self._count

    async def click(self, timeout=None, trial=False):
        self.calls.append(("click", trial))
        if trial:
            if self.trial_error:
                raise self.trial_error
            return
        if self.action_error:
            raise self.action_error

    async def fill(self, text, timeout=None):
        self.calls.append(("fill", text))
        self.filled.append(text)

    async def select_option(self, value=None, timeout=None):
        self.calls.append(("select", value))
        self.selected.append(value)

    async def set_checked(self, checked, timeout=None):
        self.calls.append(("check", checked))
        self.checked.append(checked)


class FakePage:
    def __init__(self, url=BASE, locator=None, observation=None, png=b"fake-png",
                 geometry=None, redirect_url=None):
        self._url = url
        self.locator_obj = locator or FakeLocator()
        self.observation = observation or raw_observation()
        self.png = png
        self.geometry = geometry or {
            "url": url, "width": 1280, "height": 800, "scrollX": 0, "scrollY": 0,
        }
        self.geometry_sequence = None
        self._geometry_index = 0
        self.redirect_url = redirect_url
        self.goto_calls = []
        self.scroll_calls = []
        self.screenshot_calls = []
        self.mouse = FakeMouse()
        self.last_selector = None

    @property
    def url(self):
        return self._url

    @url.setter
    def url(self, value):
        self._url = value

    def locator(self, selector):
        self.last_selector = selector
        return self.locator_obj

    async def evaluate(self, script, arg=None):
        if script == b.EXTRACT_CONTROLS:
            return self.observation
        if script == b.PAGE_GEOMETRY_JS:
            if self.geometry_sequence:
                index = min(self._geometry_index, len(self.geometry_sequence) - 1)
                self._geometry_index += 1
                return dict(self.geometry_sequence[index])
            return dict(self.geometry)
        if script == b.SCROLL_JS:
            self.scroll_calls.append(arg)
            return None
        raise AssertionError("unexpected script")

    async def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append(url)
        self._url = self.redirect_url or url

    async def screenshot(self, **kwargs):
        self.screenshot_calls.append(kwargs)
        return self.png


class AsyncApprover:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    async def __call__(self, action, target, context):
        self.calls.append({"action": action, "target": target, "context": context})
        return self.result


class RecordingHandler:
    """Synchronous approval handler used by the service (returns bool/Future)."""

    def __init__(self, result=True):
        self.result = result
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class SettingTests(unittest.TestCase):
    def test_https_origin_rules(self):
        self.assertEqual(b.https_origin("https://Example.com/x"), ("https", "example.com", 443))
        self.assertEqual(b.https_origin("https://example.com:8443/x"),
                         ("https", "example.com", 8443))
        for bad in ("http://example.com", "https://user:pass@example.com",
                    "https:///path", "ftp://example.com", "not-a-url"):
            with self.assertRaises(ValueError, msg=bad):
                b.https_origin(bad)

    def test_settings_require_origins_only_when_enabled(self):
        disabled = b.load_settings({"BROWSER_ENABLED": False,
                                    "BROWSER_ALLOWED_ORIGINS": "not-a-url"})
        self.assertFalse(disabled.enabled)
        self.assertEqual(disabled.allowed_origins, frozenset())

        with self.assertRaises(b.BrowserConfigError):
            b.load_settings({"BROWSER_ENABLED": True})

        with self.assertRaises(b.BrowserConfigError):
            b.load_settings({"BROWSER_ENABLED": True,
                             "BROWSER_ALLOWED_ORIGINS": "http://insecure.example"})

        enabled = b.load_settings({
            "BROWSER_ENABLED": "true",
            "BROWSER_ALLOWED_ORIGINS": "https://a.example, https://b.example:8443",
            "BROWSER_VISION_ENABLED": "yes",
            "BROWSER_MAX_STEPS": "99",
            "BROWSER_TASK_TIMEOUT": "5",
        })
        self.assertTrue(enabled.enabled)
        self.assertTrue(enabled.vision_enabled)
        self.assertEqual(enabled.allowed_origins, frozenset({
            ("https", "a.example", 443), ("https", "b.example", 8443),
        }))
        self.assertEqual(enabled.max_steps, 50)      # clamped
        self.assertEqual(enabled.task_timeout, 30)   # clamped

    def test_start_url_validation(self):
        settings = b.load_settings({
            "BROWSER_ENABLED": True,
            "BROWSER_ALLOWED_ORIGINS": "https://example.com",
        })
        self.assertEqual(b.validate_start_url("https://example.com/ok", settings), ORIGIN)
        with self.assertRaises(ValueError):
            b.validate_start_url("http://example.com", settings)
        with self.assertRaises(b.BrowserConfigError):
            b.validate_start_url("https://other.example", settings)

    def test_describe_and_safe_action_mask_password(self):
        target = {"label": "Password", "type": "password"}
        action = {"action": "fill", "selector": SEL, "text": "hunter2"}
        self.assertNotIn("hunter2", b._describe_action(action, target))
        self.assertNotIn("hunter2", str(b._safe_action(action, target)))
        self.assertIn("hunter2", b._safe_action(action, {"type": "text"})["text"])


class ParseTests(unittest.TestCase):
    def test_valid_actions_and_finish(self):
        click = action_json("click", selector=SEL)
        self.assertEqual(b.parse_action(click, b.ACTION_SCHEMA)["action"], "click")
        self.assertEqual(b.parse_action(FINISH, b.ACTION_SCHEMA)["action"], "finish")

    def test_rejections(self):
        cases = [
            "not json",
            json.dumps({"action": "click", "selector": "#invented"}),
            json.dumps({"action": "click", "selector": SEL, "extra": 1}),
            json.dumps({"action": "scroll", "dy": 50.5}),
            '{"action":"scroll","dy":NaN}',
            '{"action":"scroll","dy":Infinity}',
            json.dumps({"action": "finish", "summary": "x"}),
            "x" * (b.MAX_ACTION_BYTES + 1),
        ]
        for raw in cases:
            with self.assertRaises(Exception, msg=raw[:60]):
                b.parse_action(raw, b.ACTION_SCHEMA)

    def test_vision_schema(self):
        raw = json.dumps({"action": "click_coordinates", "screenshot_id": "abc",
                          "x": 10, "y": 20})
        self.assertEqual(b.parse_action(raw, b.VISION_SCHEMA)["x"], 10)
        for bad in (
            json.dumps({"action": "click_coordinates", "screenshot_id": "abc",
                        "x": -1, "y": 20}),
            json.dumps({"action": "click_coordinates", "screenshot_id": "abc",
                        "x": 10, "y": 20, "extra": True}),
            '{"action":"click_coordinates","screenshot_id":"abc","x":NaN,"y":1}',
        ):
            with self.assertRaises(Exception):
                b.parse_action(bad, b.VISION_SCHEMA)


class ObserveTests(unittest.IsolatedAsyncioTestCase):
    async def test_observation_formats_and_escapes(self):
        page = FakePage()
        page.observation = {
            "elements": [{
                "id": "abc-0", "tag": "input", "type": "email", "role": "",
                "label": '<b>"Email"</b>', "disabled": False, "checked": None,
                "required": True,
                "options": [],
            }, {
                "id": "abc-1", "tag": "select", "type": "", "role": "combobox",
                "label": "Country", "disabled": True, "checked": None,
                "required": False,
                "options": [{"label": "Malaysia", "value": "my", "disabled": False}],
            }],
            "truncated": False,
            "status": ["Welcome back", "Signed out"],
            "title": "Sign in",
            "frames": 2,
        }
        obs = await b.observe(page)

        self.assertIn('&lt;b&gt;&quot;Email&quot;&lt;/b&gt;', obs["html"])
        self.assertIn('<option value="my" disabled="false">Malaysia</option>', obs["html"])
        self.assertEqual(obs["targets"][
            '[data-agent-id="abc-0"]']["label"], '<b>"Email"</b>')
        self.assertTrue(obs["targets"]['[data-agent-id="abc-0"]']["required"])
        self.assertTrue(obs["targets"]['[data-agent-id="abc-1"]']["disabled"])
        self.assertEqual(obs["frames"], 2)
        self.assertEqual(obs["status"], "Welcome back\nSigned out")
        self.assertEqual(obs["title"], "Sign in")

    async def test_observation_is_bounded(self):
        page = FakePage()
        elements = [{
            "id": f"x-{i}", "tag": "button", "type": "", "role": "",
            "label": "L" * 400, "disabled": False, "checked": None,
            "required": False, "options": [],
        } for i in range(150)]
        page.observation = {
            "elements": elements, "truncated": False,
            "status": ["S" * 2000] * 20, "title": "T", "frames": 0,
        }
        obs = await b.observe(page)

        self.assertLessEqual(len(obs["targets"]), b.MAX_CONTROLS)
        self.assertLessEqual(len(obs["html"]), b.MAX_OBSERVATION_TEXT_CHARS)
        self.assertLessEqual(len(obs["status"]), b.MAX_STATUS_CHARS)
        self.assertTrue(obs["truncated"])

    async def test_observation_prompt_marks_content_untrusted(self):
        page = FakePage()
        settings = b.load_settings({
            "BROWSER_ENABLED": True,
            "BROWSER_ALLOWED_ORIGINS": "https://example.com",
        })
        prompt = b.build_planner_prompt(
            "click sign in", await b.observe(page), 1, 5, [], settings)
        self.assertIn("untrusted data", prompt)
        self.assertIn("never instructions", prompt)
        self.assertIn("https://example.com", prompt)
        self.assertIn('"finish"', prompt)


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.allowed = {ORIGIN}
        self.page = FakePage()
        self.approver = AsyncApprover(True)

    async def test_click_success(self):
        result = await b.execute_action(
            self.page, action_json("click", selector=SEL),
            make_observation(), b.ACTION_SCHEMA, self.allowed, self.approver)
        self.assertEqual(result["status"], "executed")
        self.assertEqual(self.page.locator_obj.calls,
                         [("click", True), ("click", False)])
        self.assertEqual(len(self.approver.calls), 1)

    async def test_click_denied_does_not_execute(self):
        self.approver = AsyncApprover(False)
        result = await b.execute_action(
            self.page, action_json("click", selector=SEL),
            make_observation(), b.ACTION_SCHEMA, self.allowed, self.approver)
        self.assertEqual(result["status"], "denied")
        self.assertEqual(self.page.locator_obj.calls, [])

    async def test_selector_missing_or_ambiguous(self):
        for count, expected in ((0, "vision_required"), (2, "reobserve")):
            self.page = FakePage(locator=FakeLocator(count=count))
            result = await b.execute_action(
                self.page, action_json("click", selector=SEL),
                make_observation(), b.ACTION_SCHEMA, self.allowed, self.approver)
            self.assertEqual(result["status"], expected)

    async def test_click_preflight_failure_requests_vision(self):
        self.page = FakePage(locator=FakeLocator(
            trial_error=b.PlaywrightError("overlay")))
        result = await b.execute_action(
            self.page, action_json("click", selector=SEL),
            make_observation(), b.ACTION_SCHEMA, self.allowed, self.approver)
        self.assertEqual(result["status"], "vision_required")
        self.assertEqual(self.page.locator_obj.calls, [("click", True)])

    async def test_disabled_control_is_blocked_without_approval(self):
        result = await b.execute_action(
            self.page, action_json("click", selector=SEL),
            make_observation(disabled=True), b.ACTION_SCHEMA,
            self.allowed, self.approver)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.approver.calls, [])

    async def test_invented_selector_is_rejected(self):
        other = f'[data-agent-id="{"b" * 32}-0"]'
        with self.assertRaises(ValueError):
            await b.execute_action(
                self.page, action_json("click", selector=other),
                make_observation(), b.ACTION_SCHEMA, self.allowed, self.approver)

    async def test_origin_policy(self):
        for url in ("https://evil.example/", "http://example.com/"):
            with self.assertRaises(PermissionError, msg=url):
                await b.execute_action(
                    self.page, action_json("navigate", url=url),
                    make_observation(), b.ACTION_SCHEMA,
                    self.allowed, self.approver)

    async def test_navigation_redirect_outside_origin_is_blocked(self):
        self.page.redirect_url = "https://evil.example/"
        result = await b.execute_action(
            self.page, action_json("navigate", url=BASE + "next"),
            make_observation(), b.ACTION_SCHEMA, self.allowed, self.approver)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("outside approved origins", result["reason"])

    async def test_page_url_drift_reobserves(self):
        self.page.url = BASE + "changed"
        result = await b.execute_action(
            self.page, action_json("click", selector=SEL),
            make_observation(), b.ACTION_SCHEMA, self.allowed, self.approver)
        self.assertEqual(result["status"], "reobserve")

    async def test_fill_select_check_scroll(self):
        fill = await b.execute_action(
            self.page, action_json("fill", selector=SEL, text="alice@example.com"),
            make_observation(field_type="email"), b.ACTION_SCHEMA,
            self.allowed, self.approver)
        self.assertEqual(fill["status"], "executed")
        self.assertEqual(self.page.locator_obj.filled, ["alice@example.com"])

        select = await b.execute_action(
            self.page, action_json("select", selector=SEL, value="my"),
            make_observation(), b.ACTION_SCHEMA, self.allowed, self.approver)
        self.assertEqual(select["status"], "executed")
        self.assertEqual(self.page.locator_obj.selected, ["my"])

        check = await b.execute_action(
            self.page, action_json("check", selector=SEL, checked=True),
            make_observation(), b.ACTION_SCHEMA, self.allowed, self.approver)
        self.assertEqual(check["status"], "executed")
        self.assertEqual(self.page.locator_obj.checked, [True])

        scroll = await b.execute_action(
            self.page, action_json("scroll", dy=-300),
            make_observation(), b.ACTION_SCHEMA, self.allowed, self.approver)
        self.assertEqual(scroll["status"], "executed")
        self.assertEqual(self.page.scroll_calls, [-300])

    async def test_execution_error_is_uncertain(self):
        self.page = FakePage(locator=FakeLocator(
            action_error=b.PlaywrightError("navigation timeout")))
        result = await b.execute_action(
            self.page, action_json("click", selector=SEL),
            make_observation(), b.ACTION_SCHEMA, self.allowed, self.approver)
        self.assertEqual(result["status"], "uncertain")


class VisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.allowed = {ORIGIN}
        self.page = FakePage()
        self.approver = AsyncApprover(True)
        self.geometry = {
            "url": BASE, "width": 1280, "height": 800, "scrollX": 0, "scrollY": 0,
        }
        self.capture = {
            "screenshot_id": "abc123",
            "png": b"fake-png",
            "sha256": hashlib.sha256(b"fake-png").hexdigest(),
            "geometry": dict(self.geometry),
            "used": False,
        }
        self.raw = json.dumps({
            "action": "click_coordinates", "screenshot_id": "abc123",
            "x": 100, "y": 200,
        })

    async def test_vision_click_success(self):
        result = await b.execute_vision_click(
            self.page, self.raw, self.capture, self.allowed, self.approver)
        self.assertEqual(result["status"], "executed")
        self.assertTrue(self.capture["used"])
        self.assertEqual(self.page.mouse.clicks, [(100, 200)])
        self.assertEqual(self.approver.calls[0]["context"]["kind"], "vision")

    async def test_capture_is_single_use(self):
        self.capture["used"] = True
        with self.assertRaisesRegex(ValueError, "already been consumed"):
            await b.execute_vision_click(
                self.page, self.raw, self.capture, self.allowed, self.approver)

    async def test_screenshot_id_must_match(self):
        wrong = self.raw.replace("abc123", "zzz999")
        with self.assertRaisesRegex(ValueError, "mismatch"):
            await b.execute_vision_click(
                self.page, wrong, self.capture, self.allowed, self.approver)

    async def test_coordinates_must_be_inside_viewport(self):
        outside = json.dumps({
            "action": "click_coordinates", "screenshot_id": "abc123",
            "x": 5000, "y": 10,
        })
        with self.assertRaisesRegex(ValueError, "outside"):
            await b.execute_vision_click(
                self.page, outside, self.capture, self.allowed, self.approver)

    async def test_denied_vision_click(self):
        self.approver = AsyncApprover(False)
        result = await b.execute_vision_click(
            self.page, self.raw, self.capture, self.allowed, self.approver)
        self.assertEqual(result["status"], "denied")
        self.assertEqual(self.page.mouse.clicks, [])
        self.assertFalse(self.capture["used"])

    async def test_changed_screenshot_requests_recapture(self):
        self.page.png = b"different"
        result = await b.execute_vision_click(
            self.page, self.raw, self.capture, self.allowed, self.approver)
        self.assertEqual(result["status"], "recapture")
        self.assertEqual(self.page.mouse.clicks, [])

    async def test_changed_geometry_requests_recapture(self):
        moved = dict(self.geometry, scrollY=120)
        self.page.geometry_sequence = [dict(self.geometry), moved]
        result = await b.execute_vision_click(
            self.page, self.raw, self.capture, self.allowed, self.approver)
        self.assertEqual(result["status"], "recapture")

    async def test_vision_click_error_is_uncertain(self):
        async def boom(x, y, **kwargs):
            raise b.PlaywrightError("click failed")
        self.page.mouse.click = boom
        result = await b.execute_vision_click(
            self.page, self.raw, self.capture, self.allowed, self.approver)
        self.assertEqual(result["status"], "uncertain")

    async def test_capture_for_vision_checks_stability_and_size(self):
        capture = await b.capture_for_vision(self.page)
        self.assertFalse(capture["used"])
        self.assertEqual(capture["geometry"], self.geometry)
        self.assertEqual(capture["sha256"],
                         hashlib.sha256(b"fake-png").hexdigest())
        self.assertEqual(self.page.screenshot_calls[0]["scale"], "css")
        self.assertFalse(self.page.screenshot_calls[0]["full_page"])

        self.page.geometry_sequence = [dict(self.geometry), dict(self.geometry, scrollY=1)]
        with self.assertRaisesRegex(b.BrowserTaskError, "moved"):
            await b.capture_for_vision(self.page)

        self.page = FakePage(png="not-bytes")
        with self.assertRaisesRegex(b.BrowserTaskError, "PNG bytes"):
            await b.capture_for_vision(self.page)

        with mock.patch.object(b, "MAX_SCREENSHOT_BYTES", 2):
            with self.assertRaisesRegex(b.BrowserTaskError, "too large"):
                await b.capture_for_vision(FakePage(png=b"abc"))


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, **overrides):
        values = dict(
            enabled=True,
            allowed_origins=frozenset({ORIGIN}),
            max_steps=5,
            task_timeout=30.0,
            planner_timeout=5.0,
            approval_timeout=2.0,
        )
        values.update(overrides)
        return b.BrowserSettings(**values)

    def planner(self, replies):
        queue = list(replies)

        def call(prompt):
            return queue.pop(0) if queue else FINISH

        return call

    def service(self, replies, settings=None, handler=None, vision=None):
        service = b.BrowserAgentService(
            settings=settings or self.settings(),
            planner=self.planner(replies),
            vision=vision,
            approval_handler=handler,
        )
        service._cancel_event = asyncio.Event()
        return service

    async def drive(self, service, page, timeout=10.0):
        return await service._drive(
            page, BASE, "do the task",
            asyncio.get_running_loop().time() + timeout,
        )

    async def test_finish_action_completes_with_evidence(self):
        service = self.service([FINISH])
        report = await self.drive(service, FakePage())
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["summary"], "Task complete")
        self.assertIn("confirmation", report["evidence"])

    async def test_click_prompts_and_finish_completes(self):
        handler = RecordingHandler(True)
        page = FakePage()
        service = self.service(
            [action_json("click", selector=SEL), FINISH], handler=handler)
        report = await self.drive(service, page)

        self.assertEqual(report["status"], "completed")
        self.assertEqual(len(handler.requests), 1)
        self.assertEqual(handler.requests[0]["summary"], 'Click "Sign in"')
        self.assertIn("Step 1: click -> executed", report["actions"])
        self.assertEqual(page.locator_obj.calls,
                         [("click", True), ("click", False)])

    async def test_scroll_is_auto_approved(self):
        def explode(request):
            raise AssertionError("scroll must not ask for approval")

        page = FakePage()
        service = self.service(
            [action_json("scroll", dy=200), FINISH], handler=explode)
        report = await self.drive(service, page)

        self.assertEqual(report["status"], "completed")
        self.assertEqual(page.scroll_calls, [200])

    async def test_denied_action_stops_the_task(self):
        handler = RecordingHandler(False)
        service = self.service(
            [action_json("click", selector=SEL), FINISH], handler=handler)
        report = await self.drive(service, FakePage())
        self.assertEqual(report["status"], "denied")
        self.assertEqual(report["steps"], 1)

    async def test_invalid_json_is_retried_then_fails(self):
        prompts = []
        service = b.BrowserAgentService(
            settings=self.settings(),
            planner=lambda p: prompts.append(p) or "not json",
            approval_handler=RecordingHandler(True),
        )
        service._cancel_event = asyncio.Event()
        report = await self.drive(service, FakePage())

        self.assertEqual(report["status"], "error")
        self.assertEqual(len(prompts), b.MAX_JSON_RETRIES + 1)

    async def test_step_budget_is_enforced(self):
        page = FakePage()
        service = self.service(
            [action_json("scroll", dy=10), action_json("scroll", dy=10)],
            settings=self.settings(max_steps=2),
        )
        report = await self.drive(service, page)
        self.assertEqual(report["status"], "budget")
        self.assertEqual(report["steps"], 2)
        self.assertEqual(page.scroll_calls, [10, 10])

    async def test_uncertain_action_is_not_retried(self):
        page = FakePage(locator=FakeLocator(
            action_error=b.PlaywrightError("timeout")))
        service = self.service(
            [action_json("click", selector=SEL), FINISH],
            handler=RecordingHandler(True))
        report = await self.drive(service, page)
        self.assertEqual(report["status"], "uncertain")
        self.assertEqual(report["steps"], 1)
        self.assertEqual(page.locator_obj.calls,
                         [("click", True), ("click", False)])

    async def test_vision_required_but_disabled_is_blocked(self):
        page = FakePage(locator=FakeLocator(count=0))
        service = self.service(
            [action_json("click", selector=SEL), FINISH],
            handler=RecordingHandler(True))
        report = await self.drive(service, page)
        self.assertEqual(report["status"], "blocked")
        self.assertIn("vision", report["summary"].lower())

    async def test_vision_fallback_clicks_and_completes(self):
        page = FakePage(locator=FakeLocator(count=0), png=b"png")
        handler = RecordingHandler(True)

        def fake_vision(png, prompt):
            screenshot_id = re.search(r"Screenshot ID: ([a-f0-9]+)", prompt).group(1)
            return json.dumps({
                "action": "click_coordinates",
                "screenshot_id": screenshot_id,
                "x": 10, "y": 20,
            })

        service = self.service(
            [action_json("click", selector=SEL), FINISH],
            settings=self.settings(vision_enabled=True),
            handler=handler, vision=fake_vision)
        report = await self.drive(service, page)

        self.assertEqual(report["status"], "completed")
        self.assertEqual(page.mouse.clicks, [(10, 20)])
        self.assertEqual([r["kind"] for r in handler.requests], ["dom", "vision"])
        self.assertIn("Step 1: click_coordinates -> executed", report["actions"])

    async def test_password_fill_is_masked_in_approval_request(self):
        handler = RecordingHandler(True)
        page = FakePage(observation=raw_observation(field_type="password"))
        service = self.service(
            [action_json("fill", selector=SEL, text="hunter2"), FINISH],
            handler=handler)
        report = await self.drive(service, page)

        self.assertEqual(report["status"], "completed")
        request = handler.requests[0]
        self.assertEqual(request["action"]["text"], "\u2022" * 7)
        self.assertNotIn("hunter2", request["summary"])
        self.assertEqual(page.locator_obj.filled, ["hunter2"])

    async def test_approval_future_is_awaited(self):
        future = concurrent.futures.Future()
        future.set_result(True)
        service = self.service(
            [action_json("click", selector=SEL), FINISH],
            handler=lambda request: future)
        report = await self.drive(service, FakePage())
        self.assertEqual(report["status"], "completed")

    async def test_approval_timeout_denies(self):
        pending = concurrent.futures.Future()
        service = self.service(
            [action_json("click", selector=SEL), FINISH],
            settings=self.settings(approval_timeout=0.05),
            handler=lambda request: pending)
        report = await self.drive(service, FakePage())
        self.assertEqual(report["status"], "denied")

    async def test_approval_handler_exception_denies(self):
        service = self.service(
            [action_json("click", selector=SEL), FINISH],
            handler=RecordingHandler(RuntimeError("bridge died")))
        report = await self.drive(service, FakePage())
        self.assertEqual(report["status"], "denied")

    async def test_cancellation_and_deadline(self):
        service = self.service([FINISH])
        service._cancel_event.set()
        with self.assertRaises(b.TaskCancelled):
            await self.drive(service, FakePage())

        service = self.service([FINISH])
        with self.assertRaises(b.TaskTimeout):
            await service._drive(
                FakePage(), BASE, "task",
                asyncio.get_running_loop().time() - 1)

    async def test_planner_error_reply_stops(self):
        service = b.BrowserAgentService(
            settings=self.settings(),
            planner=lambda p: "Error: provider unavailable",
            approval_handler=RecordingHandler(True),
        )
        service._cancel_event = asyncio.Event()
        report = await self.drive(service, FakePage())
        self.assertEqual(report["status"], "error")
        self.assertIn("provider unavailable", report["error"])

    async def test_planner_timeout_stops(self):
        def slow(prompt):
            time.sleep(0.3)
            return FINISH

        service = b.BrowserAgentService(
            settings=self.settings(planner_timeout=0.05),
            planner=slow,
            approval_handler=RecordingHandler(True),
        )
        service._cancel_event = asyncio.Event()
        report = await self.drive(service, FakePage())
        self.assertEqual(report["status"], "timeout")


class ReportTests(unittest.TestCase):
    def test_report_and_format_are_bounded(self):
        result = b._report(
            "completed", "s" * 5000, final_url="https://example.com/" + "x" * 5000,
            steps=3, evidence="e" * 5000, actions=["a" * 1000] * 30,
            error="err" * 1000,
        )
        self.assertLessEqual(len(result["summary"]), 1000)
        self.assertLessEqual(len(result["final_url"]), b.MAX_URL_CHARS)
        self.assertLessEqual(len(result["actions"]), 10)
        text = b.format_report(result)
        self.assertLessEqual(len(text), b.MAX_REPORT_CHARS)
        self.assertIn("BROWSER: completed", text)

    def test_task_entry_rejects_when_disabled(self):
        with self.assertRaisesRegex(b.BrowserConfigError, "disabled"):
            b.run_task_sync(BASE, "do it", planner=lambda p: FINISH,
                            settings=b.BrowserSettings(enabled=False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
