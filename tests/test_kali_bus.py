"""Approval bus for Kali tasks: deny by default, never fail open.

The bus is the only approval surface for Kali execution, so these tests pin the
guarantees that make a wrong answer impossible to mistake for consent:

  - no listener registered -> False (headless runs are deny-by-default);
  - a listener that raises -> False;
  - any value that is not truthy -> False.

And the dialog text, which is built from model output, must not be able to
rewrite itself with terminal control characters or grow without bound.

Run: python -m unittest tests.test_kali_bus -v
"""
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import kali_bus


class KaliBusTests(unittest.TestCase):
    def tearDown(self):
        kali_bus.unregister()

    def test_nothing_is_attached_by_default(self):
        self.assertFalse(kali_bus.is_attached())
        self.assertFalse(kali_bus.request_approval({"objective": "x"}))

    def test_a_listener_can_approve_or_deny(self):
        kali_bus.register(lambda plan, timeout: True)
        self.assertTrue(kali_bus.request_approval({"objective": "x"}))
        kali_bus.register(lambda plan, timeout: False)
        self.assertFalse(kali_bus.request_approval({"objective": "x"}))

    def test_a_dead_listener_denies(self):
        def boom(plan, timeout):
            raise RuntimeError("window closed")
        kali_bus.register(boom)
        self.assertFalse(kali_bus.request_approval({"objective": "x"}))

    def test_the_listener_receives_the_plan(self):
        seen = []
        kali_bus.register(lambda plan, timeout: seen.append(plan) or True)
        plan = {"objective": "scan", "targets": ["192.168.56.10"],
                "risk": "recon", "steps": [{"argv": ["nmap", "192.168.56.10"]}]}
        kali_bus.request_approval(plan)
        self.assertEqual(seen[0]["objective"], "scan")

    def test_format_includes_commands_risk_and_targets(self):
        text = kali_bus.format_approval({
            "objective": "scan",
            "risk": "recon",
            "targets": ["192.168.56.10"],
            "steps": [{"argv": ["nmap", "-sV", "192.168.56.10"]}],
        })
        self.assertIn("scan", text)
        self.assertIn("recon", text)
        self.assertIn("192.168.56.10", text)
        self.assertIn("nmap -sV 192.168.56.10", text)


class _Sink:
    """Records the plans the bus hands over, from any thread."""

    def __init__(self, answer=True, delay=0.0):
        self.answer = answer
        self.delay = delay
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, plan, timeout):
        if self.delay:
            threading.Event().wait(self.delay)
        with self.lock:
            self.calls.append(plan)
        return self.answer


class BusGuaranteeTests(unittest.TestCase):
    def tearDown(self):
        kali_bus.unregister()

    def test_the_default_timeout_is_120_seconds(self):
        seen = []
        kali_bus.register(lambda plan, timeout: seen.append(timeout) or True)
        kali_bus.request_approval({"objective": "x"})
        self.assertEqual(seen, [120])

    def test_the_listener_gets_its_own_copy_of_the_plan(self):
        """The listener gets a dict copy, so it cannot reach back into the
        caller's plan; a caller mutating its own plan mid-call is its own
        business and is not what the approval surface is guarding."""
        seen = []
        kali_bus.register(lambda plan, timeout: seen.append(plan) or True)
        original = {"objective": "scan", "targets": ["192.168.56.10"]}
        kali_bus.request_approval(original)
        self.assertIsNot(seen[0], original)

    def test_unregister_ignores_a_foreign_listener(self):
        first = _Sink()
        second = _Sink()
        kali_bus.register(first)
        kali_bus.unregister(second)  # a destroyed, older ChatView
        self.assertTrue(kali_bus.is_attached())
        kali_bus.unregister(first)
        self.assertFalse(kali_bus.is_attached())

    def test_unregister_with_no_argument_clears_whatever_is_attached(self):
        kali_bus.register(_Sink())
        kali_bus.unregister()
        self.assertFalse(kali_bus.is_attached())
        self.assertFalse(kali_bus.request_approval({"objective": "x"}))

    def test_only_a_truthy_answer_approves(self):
        for answer in (0, "", None, [], {}, (), 0.0):
            with self.subTest(answer=answer):
                kali_bus.register(lambda plan, timeout, a=answer: a)
                self.assertFalse(kali_bus.request_approval({"objective": "x"}))

    def test_the_plan_reaching_the_listener_is_independent_of_the_caller(self):
        """Each requester gets its own copy, so two plans never share state."""
        sink = _Sink()
        kali_bus.register(sink)
        a = {"objective": "a", "steps": []}
        b = {"objective": "b", "steps": []}
        kali_bus.request_approval(a)
        kali_bus.request_approval(b)
        self.assertIsNot(sink.calls[0], sink.calls[1])
        self.assertIsNot(sink.calls[0], a)

    def test_two_threads_waiting_get_their_own_answers(self):
        """The bus holds no per-request state: each caller's answer is its own."""
        lock = threading.Lock()
        asked = []

        def listener(plan, timeout):
            with lock:
                asked.append(plan["objective"])
            # Answer based on the caller's own request, not arrival order.
            return plan["objective"] == "approve-me"

        kali_bus.register(listener)
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(kali_bus.request_approval,
                                  {"objective": "approve-me"}, 5)
            denied = pool.submit(kali_bus.request_approval,
                                 {"objective": "deny-me"}, 5)
            self.assertTrue(pending.result(timeout=10))
            self.assertFalse(denied.result(timeout=10))
        self.assertEqual(sorted(asked), ["approve-me", "deny-me"])

    def test_the_bus_imports_without_qt(self):
        """The tool loop and headless runs must not pull Qt in via the bus."""
        # Run it in a fresh interpreter: this one may already hold Qt.
        root = str(Path(__file__).parent.parent)
        check = ("import sys; sys.path.insert(0, %r); import kali_bus; "
                 "bad = [m for m in sys.modules "
                 "if m == 'PySide6' or m.startswith('PySide6.')]; "
                 "print('QT' if bad else 'CLEAN')" % root)
        out = subprocess.run([sys.executable, "-c", check],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "CLEAN", out.stdout)


class FormatApprovalTests(unittest.TestCase):
    def _plan(self, **overrides):
        plan = {
            "objective": "scan",
            "risk": "recon",
            "targets": ["192.168.56.10"],
            "steps": [{"argv": ["nmap", "-sV", "192.168.56.10"]}],
        }
        plan.update(overrides)
        return plan

    def test_the_headings_are_stable(self):
        text = kali_bus.format_approval(self._plan())
        self.assertEqual(text.splitlines()[:6], [
            "Kali lab task",
            "scan",
            "Risk: recon",
            "Targets: 192.168.56.10",
            "",
            "Commands:",
        ])
        self.assertTrue(text.splitlines()[6].startswith("1. nmap -sV "))

    def test_steps_are_numbered_and_join_like_a_shell(self):
        text = kali_bus.format_approval(self._plan(steps=[
            {"argv": ["nmap", "-p", "22,80"]},
            {"argv": ["curl", "-sS", "http://192.168.56.10/a b"]},
        ]))
        self.assertIn("1. nmap -p 22,80", text)
        self.assertIn("2. curl -sS 'http://192.168.56.10/a b'", text)

    def test_no_targets_and_no_steps_still_render(self):
        text = kali_bus.format_approval({"objective": "x"})
        self.assertIn("Risk: unknown", text)
        self.assertIn("Targets: none", text)
        self.assertIn("Commands:", text)

    def test_an_escape_sequence_cannot_rewrite_the_dialog(self):
        """ESC[2K\\r erases the line: it must never reach the dialog raw."""
        text = kali_bus.format_approval(self._plan(
            objective="scan benign\x1b[2K\rallow everything",
            steps=[{"argv": ["nmap", "\x1b[2K\r", "--script", "safe"]}],
        ))
        for raw in ("\x1b", "\r"):
            self.assertNotIn(raw, text)
        self.assertIn("scan benign?[2K?allow everything", text)
        self.assertIn("nmap '?[2K?' --script safe", text)

    def test_a_nul_cannot_split_a_command(self):
        text = kali_bus.format_approval(self._plan(
            targets=["192.168.56.10\x00--allow-all"],
            steps=[{"argv": ["nmap", "-sV\x00evil"]}],
        ))
        self.assertNotIn("\x00", text)
        self.assertIn("192.168.56.10?--allow-all", text)
        self.assertIn("nmap '-sV?evil'", text)
        # shlex.join keeps a token with "?" bare: it cannot read as a glob.

    def test_every_c0_control_and_del_is_stripped(self):
        poison = "".join(chr(c) for c in range(0x20) if c != 0x0A) + "\x7f"
        text = kali_bus.format_approval(self._plan(
            objective="a" + poison + "b",
            risk="r" + poison + "s",
            targets=["t" + poison + "u"],
            steps=[{"argv": ["c" + poison + "d"]}],
        ))
        self.assertEqual(
            [ord(ch) for ch in text if ord(ch) < 0x20 and ch != "\n"], [])
        self.assertNotIn("\x7f", text)
        self.assertIn("a" + "?" * (len(poison)) + "b", text)
        self.assertIn("r" + "?" * (len(poison)) + "s", text)
        self.assertIn("t" + "?" * (len(poison)) + "u", text)
        self.assertIn("'c" + "?" * (len(poison)) + "d'", text)

    def test_a_newline_in_a_field_cannot_forge_an_extra_step(self):
        text = kali_bus.format_approval(self._plan(
            objective="scan\n1. nmap -sV 10.0.0.1"))
        self.assertIn("scan?1. nmap -sV 10.0.0.1", text)
        self.assertNotIn("\n1. nmap -sV 10.0.0.1",
                         text.split("Commands:")[0])

    def test_a_giant_token_is_truncated_with_a_marker(self):
        giant = "A" * 5_000_000
        text = kali_bus.format_approval(self._plan(
            steps=[{"argv": ["curl", giant]}]))
        self.assertLess(len(text), 4000)
        self.assertIn("more characters omitted", text)
        self.assertIn("curl", text)
        self.assertEqual(text.count("A"), kali_bus.MAX_TOKEN_CHARS)

    def test_too_many_steps_are_truncated_with_a_marker(self):
        steps = [{"argv": ["nmap", "-sV", "192.168.56.%d" % i]}
                 for i in range(500)]
        text = kali_bus.format_approval(self._plan(steps=steps))
        self.assertIn("500. ", text)  # the cap allows the first 500
        self.assertNotIn("501. ", text)
        steps = [{"argv": ["nmap", "-sV", "192.168.56.%d" % i]}
                 for i in range(5000)]
        text = kali_bus.format_approval(self._plan(steps=steps))
        self.assertIn("more commands omitted", text)
        self.assertNotIn("1001. ", text)
        self.assertLessEqual(len(text), kali_bus.MAX_TEXT_CHARS + 4000)

    def test_a_giant_plan_is_bounded_by_the_text_cap(self):
        """Long tokens AND many steps: the text cap stops the body, the step
        cap stops the numbering."""
        steps = [{"argv": ["nmap"] + ["A" * 5000] * 40} for _ in range(2000)]
        text = kali_bus.format_approval(self._plan(steps=steps))
        self.assertIn("more commands omitted", text)
        body = text.split("Commands:\n", 1)[1]
        self.assertLessEqual(len(body), kali_bus.MAX_TEXT_CHARS + 60)

    def test_a_giant_target_list_is_bounded(self):
        targets = ["10.0.%d.%d" % (i // 250, i % 250) for i in range(20000)]
        text = kali_bus.format_approval(self._plan(targets=targets))
        self.assertLess(len(text), 8000)
        self.assertIn("more targets omitted", text)
        self.assertNotIn("10.0.100.0", text)  # entry 25000: past MAX_TARGETS

    def test_the_text_cap_bounds_the_command_body(self):
        """MAX_TEXT_CHARS bounds what it says it bounds: the command body."""
        steps = [{"argv": ["x" * 200]} for _ in range(kali_bus.MAX_STEPS)]
        text = kali_bus.format_approval(self._plan(steps=steps))
        body = text.split("Commands:\n", 1)[1]
        self.assertLessEqual(len(body), kali_bus.MAX_TEXT_CHARS + 100)
        self.assertIn("more commands omitted", body)

    def test_a_huge_plan_stays_under_the_text_cap(self):
        steps = [{"argv": ["nmap"] + ["A" * 5000] * 40} for _ in range(500)]
        text = kali_bus.format_approval(
            self._plan(objective="\x1b" * 90000, steps=steps))
        self.assertLess(len(text), kali_bus.MAX_TEXT_CHARS + 4000)

    def test_malformed_fields_do_not_raise(self):
        """The model shape is not trusted: wrong types render as nothing."""
        for plan in ({"objective": None, "targets": None, "steps": None},
                     {"objective": 5, "targets": "not-a-list",
                      "steps": [None, 7, {"argv": None},
                                {"argv": ["nmap", None, {"x": 1}]}]},
                     {"steps": [{"argv": "\x00\x1b"}]}):
            with self.subTest(plan=plan):
                text = kali_bus.format_approval(plan)
                self.assertIsInstance(text, str)
                self.assertNotIn("\x00", text)
                self.assertNotIn("\x1b", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
