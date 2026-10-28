"""Cancellation signal tests for turns, the autonomous loop, and Python.

Run: python -m unittest tests.test_stop_signal -v
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent

CANCELLED = "ERROR: Task cancelled by user."


def final_message(step_log):
    for line in reversed(step_log or []):
        if line.startswith("Action: chat(") and "-> Result: " in line:
            text = line.split("-> Result: ", 1)[-1]
            return text[4:] if text.startswith("AI: ") else text
        if line.startswith("Done:"):
            return line[5:].strip()
    return None


class StopSignalTests(unittest.TestCase):
    def setUp(self):
        agent._clear_turn_route()
        agent._clear_stop_event()
        self.addCleanup(agent._clear_turn_route)
        self.addCleanup(agent._clear_stop_event)
        p = mock.patch.object(agent, "GROUP_MODE", False)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch("rag_tool.search_documents", return_value="")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(agent, "_generate_skill_from_turn", return_value="")
        p.start()
        self.addCleanup(p.stop)

    def test_preset_stop_skips_the_llm_and_returns_the_error(self):
        event = threading.Event()
        event.set()
        with mock.patch.object(agent, "get_valid_action") as planner:
            step_log = agent._run_agent_turn_impl("hello", [], stop_event=event)
        planner.assert_not_called()
        self.assertEqual(final_message(step_log), CANCELLED)

    def test_stop_after_the_llm_prevents_tool_execution(self):
        event = threading.Event()

        def planner(_input, _context):
            event.set()
            return {"tool": "read_file", "input": "notes.txt"}, None

        with mock.patch.object(agent, "get_valid_action", side_effect=planner), \
                mock.patch.object(agent, "execute_tool") as execute:
            step_log = agent._run_agent_turn_impl("hello", [], stop_event=event)
        execute.assert_not_called()
        self.assertEqual(final_message(step_log), CANCELLED)

    def test_run_agent_turn_forwards_the_event_to_the_normal_loop(self):
        event = threading.Event()
        with mock.patch.object(agent, "_run_agent_turn_impl",
                               return_value=["ok"]) as impl:
            self.assertEqual(agent.run_agent_turn("hello", [], stop_event=event),
                             ["ok"])
        impl.assert_called_once_with("hello", [], on_step=None, stop_event=event)

    def test_run_agent_turn_forwards_the_event_to_the_autonomous_loop(self):
        event = threading.Event()
        with mock.patch.object(agent, "_run_autonomous_goal",
                               return_value=["ok"]) as run:
            self.assertEqual(agent.run_agent_turn("/task do it", [], stop_event=event),
                             ["ok"])
        run.assert_called_once_with("do it", on_step=None, stop_event=event)

    def test_autonomous_preset_stop_returns_the_error(self):
        event = threading.Event()
        event.set()
        step_log = agent._run_autonomous_goal("goal", stop_event=event)
        self.assertIn(CANCELLED, "\n".join(step_log))

    def test_stop_terminates_a_running_python_process(self):
        event = threading.Event()
        agent._set_stop_event(event)
        threading.Timer(0.3, event.set).start()
        started = time.monotonic()
        out = agent.execute_python("import time; time.sleep(30)")
        self.assertEqual(out, CANCELLED)
        self.assertLess(time.monotonic() - started, 5)

    def test_cancelled_python_does_not_count_as_a_failed_run(self):
        agent.reset_python_failures()
        event = threading.Event()
        agent._set_stop_event(event)
        threading.Timer(0.3, event.set).start()
        agent.execute_python("import time; time.sleep(30)")
        self.assertEqual(agent.python_failure_count(), 0)


if __name__ == "__main__":
    unittest.main()
