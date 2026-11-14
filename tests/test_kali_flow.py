"""The whole Kali flow, faked: validate, check, approve, execute.

Run: python -m unittest tests.test_kali_flow -v

This is the integration proof for the Kali tool path. The contract is:

    kali_task validates locally
      -> kali_bridge.check
      -> build plan (risk + targets + the validated steps)
      -> kali_bus.request_approval
      -> only when that is True, kali_bridge.task

The tests here prove the order of those calls, not just their occurrence, and
that every way of not approving -- a policy denial, a denial, a headless run,
an approval that times out -- leaves `task` uncalled. The timeout case drives
the REAL bus and the REALLY registered relay instead of mocking
`request_approval`, so the deny-on-timeout path is the shipped one.

Nothing here touches the network or a real ssh: every `kali_bridge` entry
point is patched and the one test that lets `check` through refuses before it
would open a connection.
"""
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent
import kali_bridge
import kali_bus

# Generous bound for one fake call plus one short wait. Never an exact timing.
SLACK_SECONDS = 30


class _Recorder:
    """A fake bridge entry point that logs which step of the flow it served."""

    def __init__(self, calls, name, payload):
        self.calls = calls
        self.name = name
        self.payload = payload
        self.args = None

    def __call__(self, *args, **kwargs):
        self.calls.append(self.name)
        self.args = (args, kwargs)
        return self.payload


class KaliFlowTests(unittest.TestCase):
    def setUp(self):
        kali_bus.unregister()
        self.steps = [{"argv": ["nmap", "-sV", "192.168.56.10"],
                       "timeout": 10}]
        self.input = {"objective": "scan", "steps": self.steps,
                      "total_timeout": 30}
        self.check = {"ok": True, "risk": "recon",
                      "targets": ["192.168.56.10"], "steps": []}
        self.result = {
            "ok": True, "risk": "recon", "targets": ["192.168.56.10"],
            "results": [{
                "argv": ["nmap", "-sV", "192.168.56.10"],
                "exit_code": 0, "stdout": "22/tcp open ssh",
                "stderr": "", "timed_out": False, "truncated": False,
            }],
        }

    def tearDown(self):
        kali_bus.unregister()

    # -- the brief's cases ---------------------------------------------------

    def test_approval_is_required_before_execute(self):
        with mock.patch.object(agent.kali_bridge, "check",
                               return_value=self.check), \
             mock.patch.object(agent.kali_bus, "request_approval",
                               return_value=True) as approve, \
             mock.patch.object(agent.kali_bridge, "task",
                               return_value=self.result) as task:
            text = agent.kali_task(self.input)

        approve.assert_called_once()
        task.assert_called_once()
        self.assertIn("22/tcp open ssh", text)

    def test_denial_never_executes(self):
        with mock.patch.object(agent.kali_bridge, "check",
                               return_value=self.check), \
             mock.patch.object(agent.kali_bus, "request_approval",
                               return_value=False), \
             mock.patch.object(agent.kali_bridge, "task") as task:
            text = agent.kali_task(self.input)

        task.assert_not_called()
        self.assertIn("not approved", text)

    def test_headless_never_executes(self):
        with mock.patch.object(agent.kali_bridge, "check",
                               return_value=self.check), \
             mock.patch.object(agent.kali_bridge, "task") as task:
            text = agent.kali_task(self.input)

        task.assert_not_called()
        self.assertIn("not approved", text)

    # -- beyond the brief: order, real timeout, policy, group mode, plan ----

    def test_the_three_collaborators_run_in_order(self):
        """check -> approval -> task, as a sequence.

        Counting calls would pass even if `task` ran before the dialog was
        ever shown (put `task(steps)` above the `request_approval` guard and
        every assert_called_once test still passes). The list comparison pins
        the order; the single call per collaborator is pinned too.
        """
        calls = []
        with mock.patch.object(agent.kali_bridge, "check",
                               side_effect=_Recorder(calls, "check",
                                                     self.check)), \
             mock.patch.object(agent.kali_bus, "request_approval",
                               side_effect=_Recorder(calls, "approve", True)), \
             mock.patch.object(agent.kali_bridge, "task",
                               side_effect=_Recorder(calls, "task",
                                                     self.result)):
            agent.kali_task(self.input)

        self.assertEqual(calls, ["check", "approve", "task"])

    def test_a_timed_out_approval_never_executes(self):
        """The REAL bus and the REAL relay, never answered, is a deny.

        `kali_task` asks the bus with its own default wait, so only that
        default is patched down to a fraction of a second -- exactly the value
        `kali_task` would have passed. `kali_bus.request_approval` is NOT
        mocked: the shipped body runs, the shipped listener is registered, and
        the shipped relay's `event.wait` is what expires. The bus's default is
        called out here because it is the one argument this test changes.
        """
        from PySide6.QtCore import QObject, Slot
        from PySide6.QtWidgets import QApplication

        from agent_gui_qt import _KaliRelay

        app = QApplication.instance() or QApplication([])
        asked = []

        class _NeverAnswers(QObject):
            @Slot(object, object)
            def on_request(self, plan, finish):
                # The dialog was shown but nobody clicked: `finish` is never
                # called, so only the relay's wait can end this request.
                asked.append(plan)

        receiver = _NeverAnswers()
        relay = _KaliRelay()
        relay.approval_requested.connect(receiver.on_request)
        kali_bus.register(relay.request)
        self.addCleanup(kali_bus.unregister, relay.request)
        self.assertTrue(kali_bus.is_attached())

        tiny = 0.05
        real_ask = kali_bus.request_approval
        self.assertEqual(real_ask.__defaults__, (120,))
        # `patch.object` on a callable delegates its own `__defaults__` as an
        # attribute value, so the function object (and its real body) is
        # patched in place rather than replaced.
        with mock.patch.object(agent.kali_bridge, "check",
                               return_value=self.check),              mock.patch.object(agent.kali_bridge, "task") as task,              mock.patch.object(real_ask, "__defaults__", (tiny,)):
            started = time.monotonic()
            text = agent.kali_task(self.input)
            elapsed = time.monotonic() - started
            # Between polls the worker's `event.wait(tiny)` expires;
            # delivering the queued signal here only proves the dialog was
            # reached. Nobody ever answers it.
            deadline = time.monotonic() + max(0.0, SLACK_SECONDS - elapsed)
            while not asked and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.002)
            app.processEvents()

        self.assertEqual(real_ask.__defaults__, (120,))  # restored
        task.assert_not_called()
        self.assertIn("not approved", text)
        # Generous ceiling, never an exact timing: the real default would
        # still leave this blocked long past the ceiling.
        self.assertLess(elapsed, SLACK_SECONDS)

    def test_a_policy_denial_never_opens_the_dialog(self):
        with mock.patch.object(
                agent.kali_bridge, "check",
                side_effect=kali_bridge.KaliPolicyError(
                    "outside the approved scope")), \
             mock.patch.object(agent.kali_bus, "request_approval") as approve, \
             mock.patch.object(agent.kali_bridge, "task") as task:
            text = agent.kali_task(self.input)

        approve.assert_not_called()
        task.assert_not_called()
        self.assertIn("outside the approved scope", text)

    def test_group_mode_refuses_kali_task(self):
        """Group chats get no Kali at all: not the dialog, not the VM.

        `SAFE_GROUP_TOOLS` excludes kali_task today; this pins the refusal at
        the dispatcher so an edit to that set cannot silently enable it.
        """
        note = mock.patch.object(agent, "note_tool_result", mock.Mock())
        note.start()
        self.addCleanup(note.stop)
        agent._clear_turn_route()
        self.addCleanup(agent._clear_turn_route)
        with mock.patch.object(agent, "GROUP_MODE", True), \
             mock.patch.object(agent.kali_bridge, "check") as check, \
             mock.patch.object(agent.kali_bus, "request_approval") as approve, \
             mock.patch.object(agent.kali_bridge, "task") as task:
            text = agent.execute_tool({"tool": "kali_task",
                                       "input": self.input})

        check.assert_not_called()
        approve.assert_not_called()
        task.assert_not_called()
        self.assertIn("disabled in group chats", text)

    def test_the_approved_plan_is_what_runs(self):
        """The steps executed are the ones approved, and the plan is honest.

        The plan handed to the dialog must carry the wrapper's risk and
        targets (a plan with no risk reads as "unknown" and a plan with no
        targets shows "none", which would hide what is at stake), and `task`
        must receive the same validated step list -- not the raw input, which
        could differ in shape from what check evaluated.
        """
        plans = []
        with mock.patch.object(agent.kali_bridge, "check",
                               return_value=self.check) as check, \
             mock.patch.object(agent.kali_bus, "request_approval",
                               side_effect=lambda plan: bool(plans.append(
                                   plan)) or True), \
             mock.patch.object(agent.kali_bridge, "task",
                               return_value=self.result) as task:
            agent.kali_task(self.input)

        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertEqual(plan["risk"], "recon")
        self.assertEqual(plan["targets"], ["192.168.56.10"])
        self.assertEqual(plan["objective"], "scan")
        # Same step list for check, dialog and execution: one object, so a
        # later widening of the run cannot slip in between approve and run.
        validated = check.call_args[0][1]
        check.assert_called_once_with("scan", validated, total_timeout=30)
        self.assertIs(plan["steps"], validated)
        task.assert_called_once_with("scan", plan["steps"], total_timeout=30)
        self.assertIs(task.call_args[0][1], validated)
        self.assertEqual(task.call_args[1], {"total_timeout": 30})
        self.assertEqual(validated, [{"argv": ["nmap", "-sV", "192.168.56.10"],
                                      "timeout": 10}])


if __name__ == "__main__":
    unittest.main()
