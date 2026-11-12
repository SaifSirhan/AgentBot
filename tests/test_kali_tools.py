"""Kestrel's Kali tools: local validation, approval ordering, honest output.

Run: python -m unittest tests.test_kali_tools -v

Nothing here touches the network: every kali_bridge entry point is patched, and
the Kali-side wrapper is only ever loaded to compare constants.
"""
import importlib.util
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent
import kali_bridge
import kali_bus
import native_tools
from tool_registry import InputValidationError

ROOT = Path(__file__).resolve().parents[1]


class KaliTaskTests(unittest.TestCase):
    def setUp(self):
        self.steps = [{"argv": ["nmap", "-sV", "192.168.56.10"],
                       "timeout": 10}]
        self.input = {"objective": "scan", "steps": self.steps,
                      "total_timeout": 30}
        self.check = {
            "ok": True,
            "risk": "recon",
            "targets": ["192.168.56.10"],
            "steps": [["nmap", "-sV", "192.168.56.10"]],
        }
        self.result = {
            "ok": True,
            "risk": "recon",
            "targets": ["192.168.56.10"],
            "results": [{
                "argv": ["nmap", "-sV", "192.168.56.10"],
                "exit_code": 0,
                "stdout": "80/tcp open http",
                "stderr": "",
                "timed_out": False,
                "truncated": False,
            }],
        }

    def test_checks_then_approves_then_executes(self):
        calls = []
        with mock.patch.object(agent.kali_bridge, "check",
                               side_effect=lambda *a, **k: calls.append("check")
                               or self.check), \
             mock.patch.object(agent.kali_bus, "request_approval",
                               side_effect=lambda *a, **k: calls.append("approve")
                               or True), \
             mock.patch.object(agent.kali_bridge, "task",
                               side_effect=lambda *a, **k: calls.append("task")
                               or self.result):
            text = agent.kali_task(self.input)

        self.assertEqual(calls, ["check", "approve", "task"])
        self.assertIn("80/tcp open http", text)

    def test_a_denied_dialog_never_executes(self):
        with mock.patch.object(agent.kali_bridge, "check",
                               return_value=self.check), \
             mock.patch.object(agent.kali_bus, "request_approval",
                               return_value=False), \
             mock.patch.object(agent.kali_bridge, "task") as task:
            text = agent.kali_task(self.input)

        task.assert_not_called()
        self.assertIn("not approved", text)

    def test_a_policy_denial_never_opens_the_dialog(self):
        with mock.patch.object(
                agent.kali_bridge, "check",
                side_effect=kali_bridge.KaliPolicyError("outside scope")), \
             mock.patch.object(agent.kali_bus, "request_approval") as approve:
            text = agent.kali_task(self.input)

        approve.assert_not_called()
        self.assertIn("outside scope", text)

    def test_bad_steps_are_rejected_before_ssh(self):
        for value in (

            {"objective": "scan", "steps": []},
            {"objective": "", "steps": self.steps},
            {"objective": "scan", "steps": [{"argv": "nmap"}]},
            {"objective": "scan", "steps": [{"argv": []}]},
            {"objective": "scan", "steps": [{"argv": ["nmap"],
                                             "timeout": 0}]},
        ):
            with self.subTest(value=value):
                with mock.patch.object(agent.kali_bridge, "check") as check:
                    text = agent.kali_task(value)
                check.assert_not_called()
                self.assertTrue(text.startswith("ERROR"))

    def test_a_missing_vm_is_honest(self):
        with mock.patch.object(
                agent.kali_bridge, "check",
                side_effect=kali_bridge.KaliUnavailable("not reachable")):
            text = agent.kali_task(self.input)
        self.assertTrue(text.startswith("ERROR"))
        self.assertIn("not reachable", text)


class KaliScopeTests(unittest.TestCase):
    def test_formats_the_scope(self):
        with mock.patch.object(agent.kali_bridge, "scope",
                               return_value={
                                   "networks": ["192.168.56.0/24"],
                                   "domains": ["*.lab.local"],
                                   "allow_public": False,
                                   "expires": "2027-12-31T23:59:59Z",
                               }):
            text = agent.kali_scope("")
        self.assertIn("192.168.56.0/24", text)
        self.assertIn("*.lab.local", text)
        self.assertIn("public", text)

    def test_a_missing_vm_is_honest(self):
        with mock.patch.object(
                agent.kali_bridge, "scope",
                side_effect=kali_bridge.KaliUnavailable("not reachable")):
            text = agent.kali_scope("")
        self.assertTrue(text.startswith("ERROR"))
        self.assertIn("not reachable", text)


class KaliSchemaTests(unittest.TestCase):
    def test_tools_are_registered(self):
        for name in ("kali_task", "kali_scope"):
            self.assertIn(name, agent.VALID_TOOLS)
            self.assertIsNotNone(agent.TOOL_REGISTRY.get(name))
        self.assertIn("kali_task", agent.OBJECT_INPUT_TOOLS)

    def test_kali_task_schema_requires_argv(self):
        spec = agent.TOOL_REGISTRY.get("kali_task")
        spec.validate({"objective": "scan",
                       "steps": [{"argv": ["nmap", "192.168.56.10"]}]})
        for value in ({}, {"objective": "scan"},
                      {"objective": "scan", "steps": [{}]}):
            with self.subTest(value=value):
                with self.assertRaises(InputValidationError):
                    spec.validate(value)

    def test_native_schema_keeps_the_step_shape(self):
        specs = native_tools.build_specs(["kali_task"],
                                         agent.TOOL_REGISTRY)
        parameters = specs[0]["function"]["parameters"]
        self.assertEqual(parameters["type"], "object")
        self.assertIn("steps", parameters["properties"])
        self.assertEqual(parameters["required"], ["objective", "steps"])


class KaliFixture(unittest.TestCase):
    """The same plan the brief's tests use, for the added ordering checks."""

    def setUp(self):
        self.steps = [{"argv": ["nmap", "-sV", "192.168.56.10"],
                       "timeout": 10}]
        self.input = {"objective": "scan", "steps": self.steps,
                      "total_timeout": 30}
        self.check = {"ok": True, "risk": "recon",
                      "targets": ["192.168.56.10"],
                      "steps": [["nmap", "-sV", "192.168.56.10"]]}
        self.result = {"ok": True, "risk": "recon",
                       "targets": ["192.168.56.10"],
                       "results": [{"argv": ["nmap", "-sV", "192.168.56.10"],
                                    "exit_code": 0, "stdout": "80/tcp open",
                                    "stderr": "", "timed_out": False,
                                    "truncated": False, "read_errors": []}]}

    def assert_rejected_before_ssh(self, value):
        """No SSH, no dialog and no execution for a locally invalid input."""
        with mock.patch.object(agent.kali_bridge, "check") as check, \
             mock.patch.object(agent.kali_bus, "request_approval") as approve, \
             mock.patch.object(agent.kali_bridge, "task") as task:
            text = agent.kali_task(value)
        check.assert_not_called()
        approve.assert_not_called()
        task.assert_not_called()
        self.assertIsInstance(text, str)
        self.assertTrue(text.startswith("ERROR"), text)
        return text


class KaliApprovalOrderTests(KaliFixture):
    """check -> approve -> execute, and every 'no' stops before execute."""

    def test_a_headless_approval_never_executes(self):
        # The real bus, with no GUI listener attached: no dialog was shown.
        with mock.patch.object(agent.kali_bridge, "check",
                               return_value=self.check), \
             mock.patch.object(agent.kali_bus, "_listener", None), \
             mock.patch.object(agent.kali_bridge, "task") as task:
            text = agent.kali_task(self.input)

        task.assert_not_called()
        self.assertIn("not approved", text)

    def test_a_timed_out_approval_never_executes(self):
        real_request = kali_bus.request_approval

        def expired_listener(plan, timeout):
            # A real wait that runs out of budget, exactly as the relay does.
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                time.sleep(0.01)
            return False

        # Only the wait budget is shortened (120s would stall the suite); the
        # real bus and a real expired wait still decide the answer.
        with mock.patch.object(agent.kali_bridge, "check",
                               return_value=self.check), \
             mock.patch.object(agent.kali_bus, "_listener", expired_listener), \
             mock.patch.object(agent.kali_bus, "request_approval",
                               side_effect=lambda plan: real_request(
                                   plan, timeout=0.05)), \
             mock.patch.object(agent.kali_bridge, "task") as task:
            text = agent.kali_task(self.input)

        task.assert_not_called()
        self.assertIn("not approved", text)

    def test_a_listener_that_raises_never_executes(self):
        def broken(plan, timeout):
            raise RuntimeError("window closed mid-turn")

        with mock.patch.object(agent.kali_bridge, "check",
                               return_value=self.check), \
             mock.patch.object(agent.kali_bus, "_listener", broken), \
             mock.patch.object(agent.kali_bridge, "task") as task:
            text = agent.kali_task(self.input)

        task.assert_not_called()
        self.assertIn("not approved", text)

    def test_a_policy_denial_never_executes_either(self):
        with mock.patch.object(
                agent.kali_bridge, "check",
                side_effect=kali_bridge.KaliPolicyError("outside scope")), \
             mock.patch.object(agent.kali_bus, "request_approval") as approve, \
             mock.patch.object(agent.kali_bridge, "task") as task:
            text = agent.kali_task(self.input)

        approve.assert_not_called()
        task.assert_not_called()
        self.assertTrue(text.startswith("ERROR"))

    def test_any_check_failure_stops_before_the_dialog_and_execute(self):
        for exc in (kali_bridge.KaliUnavailable("not reachable"),
                    kali_bridge.KaliPolicyError("outside scope"),
                    kali_bridge.KaliProtocolError("bad json"),
                    kali_bridge.KaliError("boom")):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(agent.kali_bridge, "check",
                                       side_effect=exc), \
                     mock.patch.object(agent.kali_bus,
                                       "request_approval") as approve, \
                     mock.patch.object(agent.kali_bridge, "task") as task:
                    text = agent.kali_task(self.input)
                approve.assert_not_called()
                task.assert_not_called()
                self.assertIsInstance(text, str)
                self.assertTrue(text.startswith("ERROR"), text)

    def test_an_execute_failure_is_a_string_not_an_exception(self):
        for exc in (kali_bridge.KaliUnavailable("not reachable"),
                    kali_bridge.KaliPolicyError("outside scope"),
                    kali_bridge.KaliProtocolError("bad json"),
                    kali_bridge.KaliError("boom")):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(agent.kali_bridge, "check",
                                       return_value=self.check), \
                     mock.patch.object(agent.kali_bus, "request_approval",
                                       return_value=True), \
                     mock.patch.object(agent.kali_bridge, "task",
                                       side_effect=exc):
                    text = agent.kali_task(self.input)
                self.assertIsInstance(text, str)
                self.assertTrue(text.startswith("ERROR"), text)

    def test_the_dialog_shows_the_checked_plan(self):
        with mock.patch.object(agent.kali_bridge, "check",
                               return_value=self.check), \
             mock.patch.object(agent.kali_bus, "request_approval",
                               return_value=True) as approve, \
             mock.patch.object(agent.kali_bridge, "task",
                               return_value=self.result):
            agent.kali_task(self.input)

        approve.assert_called_once()
        plan = approve.call_args[0][0]
        self.assertEqual(plan["objective"], "scan")
        self.assertEqual(plan["risk"], "recon")
        self.assertEqual(plan["targets"], ["192.168.56.10"])
        self.assertEqual(plan["steps"], self.steps)

    def test_execute_runs_exactly_what_was_checked_and_approved(self):
        with mock.patch.object(agent.kali_bridge, "check",
                               return_value=self.check) as check, \
             mock.patch.object(agent.kali_bus, "request_approval",
                               return_value=True) as approve, \
             mock.patch.object(agent.kali_bridge, "task",
                               return_value=self.result) as task:
            agent.kali_task(self.input)

        check.assert_called_once_with("scan", self.steps, total_timeout=30)
        task.assert_called_once_with("scan", self.steps, total_timeout=30)
        self.assertEqual(approve.call_args[0][0]["steps"],
                         task.call_args[0][1])

    def test_an_unapproved_plan_is_not_executed_even_once(self):
        # A second call after a denial must not inherit any earlier consent.
        with mock.patch.object(agent.kali_bridge, "check",
                               return_value=self.check), \
             mock.patch.object(agent.kali_bus, "request_approval",
                               side_effect=[False, True]) as approve, \
             mock.patch.object(agent.kali_bridge, "task",
                               return_value=self.result) as task:
            first = agent.kali_task(self.input)
            second = agent.kali_task(self.input)

        self.assertIn("not approved", first)
        self.assertIn("80/tcp open", second)
        self.assertEqual(approve.call_count, 2)
        task.assert_called_once()


class KaliValidationTests(KaliFixture):
    """Every invalid shape is refused locally, before any SSH round trip."""

    def test_a_non_object_input_is_rejected(self):
        for value in (None, "scan", [], 7, True,
                      {"objective": "scan"},
                      {"objective": "scan", "steps": "nmap"},
                      {"objective": "scan", "steps": {"argv": ["nmap"]}},
                      {"objective": "scan", "steps": None}):
            with self.subTest(value=value):
                self.assert_rejected_before_ssh(value)

    def test_a_non_dict_step_is_rejected(self):
        for step in ("nmap", ["nmap"], None, 7, True, (), []):
            with self.subTest(step=step):
                self.assert_rejected_before_ssh(
                    {"objective": "scan", "steps": [step]})

    def test_a_bad_argv_is_rejected(self):
        for argv in ("nmap", [], [""], ["nmap", ""], ["nmap", 7],
                     ["nmap", None], ["nmap", ["-sV"]], ("nmap",), None):
            with self.subTest(argv=argv):
                self.assert_rejected_before_ssh(
                    {"objective": "scan", "steps": [{"argv": argv}]})

    def test_a_missing_argv_is_rejected(self):
        self.assert_rejected_before_ssh(
            {"objective": "scan", "steps": [{"timeout": 10}]})

    def test_a_boolean_step_timeout_is_rejected(self):
        for timeout in (True, False):
            with self.subTest(timeout=timeout):
                self.assert_rejected_before_ssh(
                    {"objective": "scan",
                     "steps": [{"argv": ["nmap"], "timeout": timeout}]})

    def test_a_non_integer_step_timeout_is_rejected(self):
        for timeout in ("10", 10.5, None, [10]):
            with self.subTest(timeout=timeout):
                self.assert_rejected_before_ssh(
                    {"objective": "scan",
                     "steps": [{"argv": ["nmap"], "timeout": timeout}]})

    def test_an_out_of_range_step_timeout_is_rejected(self):
        for timeout in (0, -1, 601, 100000):
            with self.subTest(timeout=timeout):
                self.assert_rejected_before_ssh(
                    {"objective": "scan",
                     "steps": [{"argv": ["nmap"], "timeout": timeout}]})

    def test_a_boolean_total_timeout_is_rejected(self):
        for total in (True, False):
            with self.subTest(total=total):
                self.assert_rejected_before_ssh(
                    {"objective": "scan", "steps": self.steps,
                     "total_timeout": total})

    def test_a_bad_total_timeout_is_rejected(self):
        for total in (0, -1, 1801, "300", 30.5, None):
            with self.subTest(total=total):
                self.assert_rejected_before_ssh(
                    {"objective": "scan", "steps": self.steps,
                     "total_timeout": total})

    def test_a_blank_objective_is_rejected(self):
        for objective in ("", "   ", "\t\n"):
            with self.subTest(objective=objective):
                self.assert_rejected_before_ssh(
                    {"objective": objective, "steps": self.steps})

    def test_a_huge_step_list_is_rejected_before_ssh(self):
        # R5-C: this client-side cap is a round-trip guard, not the security
        # boundary. The wrapper enforces its own MAX_STEPS independently.
        steps = [{"argv": ["nmap", "192.168.56.10"]}
                 for _ in range(agent.KALI_MAX_STEPS + 1)]
        text = self.assert_rejected_before_ssh(
            {"objective": "scan", "steps": steps})
        self.assertIn("at most %d steps" % agent.KALI_MAX_STEPS, text)

    def test_the_last_allowed_step_count_is_accepted(self):
        steps = [{"argv": ["nmap", "192.168.56.10"], "timeout": 5}
                 for _ in range(agent.KALI_MAX_STEPS)]
        with mock.patch.object(agent.kali_bridge, "check",
                               return_value=self.check) as check, \
             mock.patch.object(agent.kali_bus, "request_approval",
                               return_value=True), \
             mock.patch.object(agent.kali_bridge, "task",
                               return_value=self.result):
            text = agent.kali_task({"objective": "scan", "steps": steps})
        check.assert_called_once()
        self.assertEqual(len(check.call_args[0][1]), agent.KALI_MAX_STEPS)
        self.assertIn("80/tcp open", text)

    def test_only_argv_and_timeout_reach_the_bridge(self):
        with mock.patch.object(agent.kali_bridge, "check",
                               return_value=self.check) as check, \
             mock.patch.object(agent.kali_bus, "request_approval",
                               return_value=True), \
             mock.patch.object(agent.kali_bridge, "task",
                               return_value=self.result) as task:
            agent.kali_task({"objective": "scan",
                             "steps": [{"argv": ["nmap"], "timeout": 9,
                                        "note": "ignored"}]})
        expected = [{"argv": ["nmap"], "timeout": 9}]
        self.assertEqual(check.call_args[0][1], expected)
        self.assertEqual(task.call_args[0][1], expected)

    def test_the_default_step_timeout_is_120(self):
        with mock.patch.object(agent.kali_bridge, "check",
                               return_value=self.check) as check, \
             mock.patch.object(agent.kali_bus, "request_approval",
                               return_value=True), \
             mock.patch.object(agent.kali_bridge, "task",
                               return_value=self.result):
            agent.kali_task({"objective": "scan",
                             "steps": [{"argv": ["nmap"]}]})
        self.assertEqual(check.call_args[0][1],
                         [{"argv": ["nmap"], "timeout": 120}])

    def test_the_default_total_timeout_is_300(self):
        with mock.patch.object(agent.kali_bridge, "check",
                               return_value=self.check) as check, \
             mock.patch.object(agent.kali_bus, "request_approval",
                               return_value=True), \
             mock.patch.object(agent.kali_bridge, "task",
                               return_value=self.result):
            agent.kali_task({"objective": "scan",
                             "steps": [{"argv": ["nmap"]}]})
        self.assertEqual(check.call_args[1], {"total_timeout": 300})


class KaliResultFormatTests(unittest.TestCase):
    """R5-B: a stream that could not be read is never reported as silence."""

    def result(self, **overrides):
        item = {"argv": ["nmap", "192.168.56.10"], "exit_code": 0,
                "stdout": "", "stderr": "", "timed_out": False,
                "truncated": False, "read_errors": []}
        item.update(overrides)
        return {"ok": True, "risk": "recon", "targets": ["192.168.56.10"],
                "results": [item]}

    def test_read_errors_are_surfaced(self):
        text = agent._format_kali_result(self.result(
            read_errors=["stdout: OSError: injected pipe failure"]))
        self.assertIn("could not be read", text)
        self.assertIn("OSError: injected pipe failure", text)
        self.assertIn("Step 1", text)

    def test_both_stream_errors_are_surfaced(self):
        text = agent._format_kali_result(self.result(
            read_errors=["stdout: OSError: out failed",
                         "stderr: OSError: err failed"]))
        self.assertIn("out failed", text)
        self.assertIn("err failed", text)

    def test_partial_output_and_the_read_error_are_both_shown(self):
        text = agent._format_kali_result(self.result(
            stdout="80/tcp open http",
            read_errors=["stderr: OSError: err failed"]))
        self.assertIn("80/tcp open http", text)
        self.assertIn("could not be read", text)

    def test_a_read_error_is_bounded(self):
        text = agent._format_kali_result(self.result(
            read_errors=["stdout: OSError: %s" % ("x" * 5000)]))
        self.assertIn("could not be read", text)
        self.assertLess(len(text), 1000)

    def test_a_clean_step_is_unchanged(self):
        text = agent._format_kali_result(self.result(stdout="80/tcp open"))
        self.assertEqual(
            text,
            "Kali task finished (risk recon).\n"
            "Step 1 [ok] nmap 192.168.56.10: 80/tcp open")
        self.assertNotIn("could not be read", text)

    def test_no_results_is_honest(self):
        self.assertEqual(agent._format_kali_result({"results": []}),
                         "Kali returned no results.")
        self.assertEqual(agent._format_kali_result({}),
                         "Kali returned no results.")

    def test_a_timeout_and_a_nonzero_exit_are_labelled(self):
        text = agent._format_kali_result(self.result(
            timed_out=True, exit_code=None))
        self.assertIn("Kali task failed", text)
        self.assertIn("Step 1 [timed out]", text)
        text = agent._format_kali_result(self.result(exit_code=2,
                                                     stderr="no route"))
        self.assertIn("Kali task failed", text)
        self.assertIn("Step 1 [exit 2]", text)
        self.assertIn("no route", text)

    def test_output_is_capped(self):
        text = agent._format_kali_result(self.result(stdout="y" * 9000))
        self.assertIn("y" * 1500, text)
        self.assertNotIn("y" * 1501, text)


class KaliScopeFailureTests(unittest.TestCase):
    def test_every_scope_failure_is_a_string(self):
        for exc in (kali_bridge.KaliUnavailable("not reachable"),
                    kali_bridge.KaliPolicyError("outside scope"),
                    kali_bridge.KaliProtocolError("bad json"),
                    kali_bridge.KaliError("boom")):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(agent.kali_bridge, "scope",
                                       side_effect=exc):
                    text = agent.kali_scope("")
                self.assertIsInstance(text, str)
                self.assertTrue(text.startswith("ERROR"), text)

    def test_an_empty_scope_says_none_and_unknown(self):
        with mock.patch.object(agent.kali_bridge, "scope", return_value={}):
            text = agent.kali_scope("")
        self.assertIn("networks none", text)
        self.assertIn("domains none", text)
        self.assertIn("public denied", text)
        self.assertIn("expires unknown", text)

    def test_public_targets_are_reported_as_allowed(self):
        with mock.patch.object(agent.kali_bridge, "scope",
                               return_value={"allow_public": True}):
            text = agent.kali_scope("")
        self.assertIn("public allowed", text)


class KaliStepCapDriftTests(unittest.TestCase):
    """R5-A: agent.py owns the client cap, and it must mirror the wrapper."""

    def load_wrapper(self):
        # Test-only load by path: production code never imports the Kali-side
        # wrapper, which is deployed on the VM, not on this machine. The module
        # must be in sys.modules while it executes so its dataclasses resolve.
        spec = importlib.util.spec_from_file_location(
            "kestrel_run_drift_guard", ROOT / "kali" / "kestrel_run.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        return module

    def test_the_client_cap_mirrors_the_wrapper(self):
        wrapper = self.load_wrapper()
        self.assertEqual(agent.KALI_MAX_STEPS, wrapper.MAX_STEPS)
        self.assertEqual(agent.KALI_MAX_STEPS, 8)

    def test_the_timeout_bounds_mirror_the_wrapper(self):
        wrapper = self.load_wrapper()
        schema = agent.TOOL_REGISTRY.get("kali_task").input_schema
        step = schema["properties"]["steps"]["items"]["properties"]["timeout"]
        self.assertEqual(step["maximum"], wrapper.MAX_STEP_TIMEOUT)
        self.assertEqual(schema["properties"]["total_timeout"]["maximum"],
                         wrapper.MAX_TOTAL_TIMEOUT)

    def test_the_schema_step_cap_matches_the_constant(self):
        schema = agent.TOOL_REGISTRY.get("kali_task").input_schema
        self.assertEqual(schema["properties"]["steps"]["maxItems"],
                         agent.KALI_MAX_STEPS)


class KaliRegistryShapeTests(unittest.TestCase):
    def test_effects_and_timeouts(self):
        scope = agent.TOOL_REGISTRY.get("kali_scope")
        self.assertEqual(scope.effect_class, "read_only")
        self.assertEqual(scope.timeout, 20)
        task = agent.TOOL_REGISTRY.get("kali_task")
        self.assertEqual(task.effect_class, "system_control")
        self.assertEqual(task.timeout, 1800)

    def test_kali_scope_takes_no_input_text(self):
        spec = agent.TOOL_REGISTRY.get("kali_scope")
        spec.validate("")
        with self.assertRaises(InputValidationError):
            spec.validate("192.168.56.0/24")

    def test_kali_task_schema_rejects_bad_shapes(self):
        spec = agent.TOOL_REGISTRY.get("kali_task")
        for value in (
                {"objective": "scan", "steps": []},
                {"objective": "", "steps": [{"argv": ["nmap"]}]},
                {"objective": None, "steps": [{"argv": ["nmap"]}]},
                {"objective": "scan", "steps": [{"argv": []}]},
                {"objective": "scan", "steps": [{"argv": ["nmap", ""]}]},
                {"objective": "scan",
                 "steps": [{"argv": ["nmap"], "timeout": True}]},
                {"objective": "scan",
                 "steps": [{"argv": ["nmap"], "timeout": 601}]},
                {"objective": "scan",
                 "steps": [{"argv": ["nmap"]}] * (agent.KALI_MAX_STEPS + 1)},
                {"objective": "scan", "steps": [{"argv": ["nmap"]}],
                 "total_timeout": 1801},
                {"objective": "scan", "steps": [{"argv": ["nmap"]}],
                 "total_timeout": False},
                {"objective": "scan",
                 "steps": [{"argv": ["nmap"], "extra": 1}]},
                {"objective": "scan", "steps": [{"argv": ["nmap"]}],
                 "extra": 1},
        ):
            with self.subTest(value=value):
                with self.assertRaises(InputValidationError):
                    spec.validate(value)

    def test_kali_task_is_not_a_string_input_tool(self):
        self.assertNotIn("kali_scope", agent.OBJECT_INPUT_TOOLS)
        specs = native_tools.build_specs(["kali_scope"], agent.TOOL_REGISTRY)
        parameters = specs[0]["function"]["parameters"]
        self.assertEqual(parameters["properties"]["input"]["type"], "string")


class KaliDispatchTests(KaliFixture):
    """The registry entry, not just the function, gates the real dispatcher."""

    def setUp(self):
        super().setUp()
        agent._clear_turn_route()
        self.addCleanup(agent._clear_turn_route)
        for name, value in (("GROUP_MODE", False),
                            ("note_tool_result", mock.Mock())):
            patcher = mock.patch.object(agent, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_dispatcher_validates_before_the_function(self):
        with mock.patch.object(agent.kali_bridge, "check") as check, \
             mock.patch.object(agent.kali_bridge, "task") as task:
            text = agent.execute_tool({"tool": "kali_task",
                                       "input": {"objective": "scan"}})
        check.assert_not_called()
        task.assert_not_called()
        self.assertTrue(text.startswith(
            "ERROR: Invalid input for kali_task. Expected "), text)

    def test_the_dispatcher_runs_an_approved_task(self):
        with mock.patch.object(agent.kali_bridge, "check",
                               return_value=self.check), \
             mock.patch.object(agent.kali_bus, "request_approval",
                               return_value=True), \
             mock.patch.object(agent.kali_bridge, "task",
                               return_value=self.result) as task:
            text = agent.execute_tool({"tool": "kali_task",
                                       "input": self.input})
        task.assert_called_once_with("scan", self.steps, total_timeout=30)
        self.assertIn("80/tcp open", text)

    def test_the_dispatcher_never_executes_a_denied_task(self):
        with mock.patch.object(agent.kali_bridge, "check",
                               return_value=self.check), \
             mock.patch.object(agent.kali_bus, "request_approval",
                               return_value=False), \
             mock.patch.object(agent.kali_bridge, "task") as task:
            text = agent.execute_tool({"tool": "kali_task",
                                       "input": self.input})
        task.assert_not_called()
        self.assertIn("not approved", text)

    def test_the_dispatcher_reads_the_scope(self):
        with mock.patch.object(agent.kali_bridge, "scope",
                               return_value={"networks": ["192.168.56.0/24"],
                                             "domains": [],
                                             "allow_public": False,
                                             "expires": "2027-12-31"}):
            text = agent.execute_tool({"tool": "kali_scope", "input": ""})
        self.assertIn("192.168.56.0/24", text)


if __name__ == "__main__":
    unittest.main()