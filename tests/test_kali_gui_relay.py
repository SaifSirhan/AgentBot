"""The Qt approval relay: a worker thread asks, the GUI thread answers.

`kali_bridge.check()`/`task()` block on the agent's worker thread, so the
confirmation dialog has to appear on the GUI thread while the worker waits. The
relay does that with a queued Qt signal plus an Event, and every path out of it
that is not an explicit "Yes" must return False: a timeout, a closed window, a
slot that raises, a reply given after the wait already gave up.

Run: python -m unittest tests.test_kali_gui_relay -v
"""
import contextlib
import io
import os
import sys
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtCore import QObject, Slot  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import kali_bus  # noqa: E402
from agent_gui_qt import _KaliRelay  # noqa: E402


class _Receiver(QObject):
    """Slot-owning receiver: a plain lambda would not prove the thread hop."""

    def __init__(self, approved):
        super().__init__()
        self.approved = approved
        self.threads = []
        self.plans = []

    @Slot(object, object)
    def on_request(self, plan, finish):
        self.threads.append(threading.current_thread())
        self.plans.append(plan)
        finish(self.approved)


class _Collecting(threading.Thread):
    def __init__(self, relay, plan, timeout):
        super().__init__(daemon=True)
        self.relay = relay
        self.plan = plan
        self.timeout = timeout
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = self.relay.request(self.plan, self.timeout)
        except BaseException as exc:  # pragma: no cover - a raise is a failure
            self.error = exc


class KaliRelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        kali_bus.unregister()

    def tearDown(self):
        kali_bus.unregister()

    def _ask(self, approved):
        relay = _KaliRelay()
        receiver = _Receiver(approved)
        relay.approval_requested.connect(receiver.on_request)
        result = []
        worker = threading.Thread(
            target=lambda: result.append(relay.request({"objective": "x"}, 2)))
        worker.start()
        while worker.is_alive():
            self.app.processEvents()
            worker.join(0.01)
        return receiver, result

    def _pump_until(self, predicate, limit=2.0):
        deadline = time.monotonic() + limit
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)

    def _pump(self):
        """Deliver everything pending, DeferredDelete included.

        `deleteLater` only queues the destruction; without this the object is
        still alive and a "destroyed window" test proves nothing.
        """
        from PySide6.QtCore import QCoreApplication, QEvent
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_the_request_is_answered_on_the_gui_thread(self):
        receiver, result = self._ask(True)
        self.assertEqual(result, [True])
        self.assertEqual(receiver.threads, [threading.main_thread()])

    def test_a_queued_slot_can_deny(self):
        receiver, result = self._ask(False)
        self.assertEqual(result, [False])
        self.assertEqual(receiver.threads, [threading.main_thread()])

    def test_the_plan_reaches_the_gui_thread(self):
        receiver, _ = self._ask(True)
        self.assertEqual(receiver.plans[0]["objective"], "x")

    def test_the_answer_is_coerced_to_a_bool(self):
        for answer in (1, "yes", [0], 0, None, ""):
            with self.subTest(answer=answer):
                relay = _KaliRelay()
                receiver = _Receiver(answer)
                relay.approval_requested.connect(receiver.on_request)
                worker = _Collecting(relay, {"objective": "x"}, 2)
                worker.start()
                self._pump_until(lambda: not worker.is_alive())
                self.assertIs(worker.result, bool(answer))

    def test_a_plain_slot_that_raises_during_the_emit_still_denies(self):
        """A connected plain callable whose raise reaches the emit is a deny.

        Measured here (PySide6 6.x): a lambda connected with AutoConnection
        runs in the emitting thread and its raise surfaces from `emit`, which
        `request` catches. The deny must be bounded whatever Qt does with the
        exception; the queued `@Slot` case is
        `test_a_connected_slot_that_raises_during_queued_delivery_still_denies`.
        """
        relay = _KaliRelay()
        called = []

        def boom(plan, finish):
            called.append(plan)
            raise RuntimeError("dialog blew up")

        relay.approval_requested.connect(boom)
        started = time.monotonic()
        # Qt prints the swallowed raise; capture it so the suite stays quiet
        # and assert on it, which proves the slot really ran and really raised.
        noisy = io.StringIO()
        with contextlib.redirect_stderr(noisy):
            approved = relay.request({"objective": "x"}, 0.2)
        self.assertFalse(approved)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(len(called), 1)
        self.assertIn("dialog blew up", noisy.getvalue())

    def test_an_unconnected_relay_denies_after_its_timeout(self):
        """No dialog is attached, so nothing can answer; the wait still ends in
        a deny, no later than the timeout."""
        started = time.monotonic()
        self.assertFalse(_KaliRelay().request({"objective": "x"}, 0.1))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_a_negative_timeout_is_a_deny_not_a_raise(self):
        """T4-5: the wait must not raise or block forever on a bad timeout.

        `Event.wait` converts a negative timeout on some Python builds and
        would raise ValueError on others, and it blocks forever on `None`;
        `max(0.0, float(timeout))` turns the negatives into an immediate
        deny, and a timeout with no numeric meaning into the same immediate
        deny (the dialog is never asked, so nothing can answer). A late Yes
        must not resurrect any of it.
        """
        for timeout in (-1, -0.5, float("-inf")):
            with self.subTest(timeout=timeout):
                relay = _KaliRelay()
                finishes = []
                relay.approval_requested.connect(
                    lambda plan, finish: finishes.append(finish))
                started = time.monotonic()
                self.assertIs(relay.request({"objective": "x"}, timeout), False)
                self.assertLess(time.monotonic() - started, 1.0)
                self.assertEqual(len(finishes), 1)  # the dialog was asked
                finishes[0](True)  # the user finally clicks Yes: too late
                self.assertFalse(kali_bus.request_approval(
                    {"objective": "x"}, timeout))
        for timeout in (None, "not a timeout", object()):
            with self.subTest(timeout=timeout):
                relay = _KaliRelay()
                finishes = []
                relay.approval_requested.connect(
                    lambda plan, finish: finishes.append(finish))
                started = time.monotonic()
                self.assertIs(relay.request({"objective": "x"}, timeout), False)
                self.assertLess(time.monotonic() - started, 1.0)
                self.assertEqual(finishes, [])  # nothing was asked
                self.assertFalse(kali_bus.request_approval(
                    {"objective": "x"}, timeout))

    def test_numeric_timeouts_are_honoured(self):
        """The guard accepts what a timeout legitimately is: a number, or a
        numeric string, used as given above the `>= 0` floor."""
        for timeout, floor in ((0, 0.0), (0.05, 0.05), ("0.05", 0.05)):
            with self.subTest(timeout=timeout):
                relay = _KaliRelay()
                relay.approval_requested.connect(lambda plan, finish: None)
                started = time.monotonic()
                self.assertIs(relay.request({"objective": "x"}, timeout), False)
                elapsed = time.monotonic() - started
                # Windows' timer granularity lets Event.wait return a few ms
                # early (measured: wait(0.05) returned 0.047 in 8/200 runs), so
                # the floor carries that slack. An ignored timeout returns in
                # ~0.0001s, which this still separates cleanly.
                self.assertGreaterEqual(elapsed, max(0.0, floor - 0.02))
                # The ceiling only has to prove "not forever": a tight one
                # measures the scheduler, not the relay, and flakes on a busy
                # machine. The floor above is what pins the honoured timeout.
                self.assertLess(elapsed, floor + 10.0)

    def test_a_finish_that_raises_is_not_swallowed(self):
        """The relay's `finish` never raises in practice (it is one lock and
        one dict assignment), so if it ever did the raise must surface to the
        crash log rather than be relabelled as a dialog failure that gets
        retried. Pin the contract: the dialog handler does not catch it."""
        from PySide6.QtWidgets import QMessageBox
        import agent_gui_qt as g
        original = g.QMessageBox.question
        g.QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Yes)
        self.addCleanup(lambda: setattr(g.QMessageBox, "question",
                                        staticmethod(original)))
        view = g.ChatView()
        self.addCleanup(view.deleteLater)
        seen = []

        def finish(approved):
            seen.append(approved)
            raise RuntimeError("waiter is gone")

        with self.assertRaises(RuntimeError):
            view._show_kali_approval({"objective": "x"}, finish)
        self.assertEqual(seen, [True])  # the answer was delivered first
        kali_bus.unregister()

    def test_no_listener_times_out_and_denies(self):
        """Headless: nothing pumped the queue, so the wait expires as a deny."""
        relay = _KaliRelay()
        started = time.monotonic()
        self.assertFalse(relay.request({"objective": "x"}, 0.05))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_a_late_answer_after_the_timeout_is_a_deny(self):
        """finish() arriving after wait() gave up must not approve anything."""
        relay = _KaliRelay()
        finishes = []
        relay.approval_requested.connect(
            lambda plan, finish: finishes.append(finish))
        self.assertFalse(relay.request({"objective": "x"}, 0.05))
        self.assertEqual(len(finishes), 1)
        finishes[0](True)  # the user finally clicked Yes — too late
        # A second request on the same relay must not inherit that late Yes.
        worker = _Collecting(relay, {"objective": "y"}, 0.05)
        worker.start()
        self._pump_until(lambda: len(finishes) == 2)
        worker.join(2)
        self.assertIs(worker.result, False)
        finishes[1](True)  # and its own answer, too late, changes nothing
        self.assertIs(worker.result, False)

    def test_an_uncopyable_plan_denies_at_once(self):
        """A plan that cannot be copied never reaches a dialog: deny now, not
        after the timeout (the wait is 5s, so <1s proves the emit path)."""
        class Hostile:
            objective = "x"

            def __iter__(self):
                raise RuntimeError("no copy for you")

        relay = _KaliRelay()
        asked = []
        relay.approval_requested.connect(
            lambda plan, finish: asked.append(plan))
        started = time.monotonic()
        self.assertFalse(relay.request(Hostile(), 5))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(asked, [])
        self.assertFalse(kali_bus.request_approval(Hostile(), 5))

    def test_the_finish_handle_is_single_use(self):
        """A denied request cannot be flipped later by a stray finish()."""
        relay = _KaliRelay()
        finishes = []
        relay.approval_requested.connect(
            lambda plan, finish: finishes.append(finish))
        worker = _Collecting(relay, {"objective": "x"}, 2)
        worker.start()
        self._pump_until(lambda: len(finishes) == 1)
        finishes[0](False)
        worker.join(2)
        self.assertIs(worker.result, False)
        finishes[0](True)
        self.assertIs(worker.result, False)

    def test_a_slow_gui_still_lets_the_worker_go(self):
        """A modal-ish dialog that never answers: the wait is still bounded."""
        relay = _KaliRelay()
        seen = []
        relay.approval_requested.connect(
            lambda plan, finish: seen.append(finish))  # never calls finish
        started = time.monotonic()
        self.assertFalse(relay.request({"objective": "x"}, 0.1))
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(len(seen), 1)

    def test_a_destroyed_window_leaves_the_worker_bounded(self):
        """The window really is destroyed before the worker asks.

        The relay is `deleteLater`ed AND the DeferredDelete event is processed,
        so its C++ object is gone (proved by the raise on connect below) by the
        time the worker calls `request`. That request is still a bounded deny,
        not a crash or a hang.
        """
        relay = _KaliRelay()
        kali_request = relay.request
        kali_bus.register(kali_request)
        relay.deleteLater()  # the window went away; queued signal has no target
        kali_bus.unregister(kali_request)  # what ChatView.destroyed runs
        self._pump()  # process DeferredDelete: this is a real destruction
        self.assertFalse(kali_bus.is_attached())
        # The relay is destroyed now: connecting to it raises, so the bounded
        # deny below cannot just be "the timeout of a live relay with no
        # receiver" (which other tests already cover).
        with self.assertRaises(RuntimeError):
            relay.approval_requested.connect(lambda plan, finish: finish(True))

        # No listener at all now: the next request is an immediate deny.
        self.assertFalse(kali_bus.request_approval({"objective": "x"}, 0.2))
        # And the destroyed relay itself is still a bounded deny.
        worker = _Collecting(relay, {"objective": "x"}, 0.1)
        started = time.monotonic()
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertIsNone(worker.error)
        self.assertIs(worker.result, False)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_a_connected_slot_that_raises_during_queued_delivery_still_denies(self):
        """T4-3: a connected `@Slot` that raises on the GUI thread.

        The plan is emitted from the worker, so Qt queues the call; when the GUI
        thread runs it the slot raises, Qt prints and swallows it, `finish` is
        never called, and the worker's own wait expires as a deny.
        """
        relay = _KaliRelay()
        raised = []

        class _Exploding(QObject):
            @Slot(object, object)
            def on_request(self, plan, finish):
                raised.append(threading.current_thread())
                raise RuntimeError("slot blew up on the GUI thread")

        exploding = _Exploding()  # keep a reference: it must outlive delivery
        self.addCleanup(exploding.deleteLater)
        relay.approval_requested.connect(exploding.on_request)
        worker = _Collecting(relay, {"objective": "x"}, 0.3)
        started = time.monotonic()
        worker.start()
        # The raise is printed by Qt while the GUI thread pumps; capture it so
        # the suite stays quiet, then assert it to prove the slot ran.
        noisy = io.StringIO()
        with contextlib.redirect_stderr(noisy):
            self._pump_until(lambda: bool(raised) or not worker.is_alive(),
                             limit=5)
        worker.join(5)
        elapsed = time.monotonic() - started

        self.assertEqual(raised, [threading.main_thread()])  # it did raise
        self.assertIn("slot blew up on the GUI thread", noisy.getvalue())
        self.assertFalse(worker.is_alive())
        self.assertIsNone(worker.error)  # the raise did not reach the caller
        self.assertIs(worker.result, False)
        # Denied by the worker's own timeout, not after some other answer.
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 2.0)


class KaliDialogTests(unittest.TestCase):
    """The ChatView slot: default No, any failure is a No."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _view(self):
        import agent_gui_qt as g
        return g.ChatView()

    def _stub_question(self, answer):
        import agent_gui_qt as g
        calls = []

        def question(parent, title, text, buttons, default):
            calls.append({"parent": parent, "title": title, "text": text,
                          "buttons": buttons, "default": default})
            if isinstance(answer, BaseException):
                raise answer
            return answer

        original = g.QMessageBox.question
        g.QMessageBox.question = staticmethod(question)
        self.addCleanup(lambda: setattr(g.QMessageBox, "question",
                                        staticmethod(original)))
        return calls

    def test_the_default_button_is_no(self):
        from PySide6.QtWidgets import QMessageBox
        view = self._view()
        self.addCleanup(view.deleteLater)
        calls = self._stub_question(QMessageBox.StandardButton.No)
        answers = []
        view._show_kali_approval(
            {"objective": "scan", "risk": "recon",
             "steps": [{"argv": ["nmap", "192.168.56.10"]}]},
            answers.append)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["default"], QMessageBox.StandardButton.No)
        self.assertEqual(
            calls[0]["buttons"],
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        self.assertEqual(calls[0]["title"], "Kali lab approval")
        self.assertIn("nmap 192.168.56.10", calls[0]["text"])
        self.assertEqual(answers, [False])

    def test_yes_approves_and_no_denies(self):
        view = self._view()
        self.addCleanup(view.deleteLater)
        from PySide6.QtWidgets import QMessageBox
        for answer, expected in (
                (QMessageBox.StandardButton.Yes, True),
                (QMessageBox.StandardButton.No, False),
                (QMessageBox.StandardButton.Cancel, False)):
            with self.subTest(answer=answer):
                self._stub_question(answer)
                answers = []
                view._show_kali_approval({"objective": "x"}, answers.append)
                self.assertEqual(answers, [expected])

    def test_a_raising_dialog_denies(self):
        view = self._view()
        self.addCleanup(view.deleteLater)
        self._stub_question(RuntimeError("no display"))
        answers = []
        view._show_kali_approval({"objective": "x"}, answers.append)
        self.assertEqual(answers, [False])

    def test_a_plan_that_breaks_the_formatter_denies(self):
        view = self._view()
        self.addCleanup(view.deleteLater)

        class Hostile(dict):
            def get(self, key, default=None):
                raise RuntimeError("boom")

        self._stub_question(None)
        answers = []
        view._show_kali_approval(Hostile(), answers.append)
        self.assertEqual(answers, [False])

    def test_the_chatview_registers_and_unregisters_the_relay(self):
        view = self._view()
        self.assertTrue(kali_bus.is_attached())
        listener = kali_bus._listener
        self.assertIs(listener.__self__, view._kali_relay)
        view.deleteLater()
        self._pump()
        self.assertFalse(kali_bus.is_attached())
        kali_bus.unregister()

    def _pump(self):
        self.app.processEvents()
        from PySide6.QtCore import QCoreApplication, QEvent
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main(verbosity=2)
