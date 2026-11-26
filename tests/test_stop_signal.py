"""Cancellation signal tests for turns, the autonomous loop, and Python.

Run: python -m unittest tests.test_stop_signal -v
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent
import python_sandbox

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
    @classmethod
    def setUpClass(cls):
        cls.sandbox_temp = tempfile.TemporaryDirectory()
        roots = Path(cls.sandbox_temp.name) / "roots"
        roots.mkdir()
        cls.sandbox_env = mock.patch.dict(os.environ, {
            "LOCALAPPDATA": str(Path(cls.sandbox_temp.name) / "local"),
            "PYTHON_SANDBOX_ROOTS": str(roots),
        })
        cls.sandbox_env.start()
        _, _, sandbox_state = python_sandbox._profile_identity()
        cls.identity_patch = mock.patch.object(
            python_sandbox, "_profile_identity",
            return_value=("Kestrel.PythonSandbox.Tests.StopSignal",
                          "S-1-test", sandbox_state))
        cls.identity_patch.start()
        import config
        cls.config_patch = mock.patch.object(
            config, "CONFIG_PATH", str(Path(cls.sandbox_temp.name) / "config.json"))
        cls.memory_patch = mock.patch.object(
            config, "memory_dir", return_value=str(Path(cls.sandbox_temp.name) / "memory"))
        cls.config_patch.start()
        cls.memory_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.memory_patch.stop()
        cls.config_patch.stop()
        cls.identity_patch.stop()
        cls.sandbox_env.stop()
        cls.sandbox_temp.cleanup()

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
        with mock.patch.object(agent, "get_valid_action") as planner, \
                mock.patch("rag_tool.search_documents", return_value=""):
            step_log = agent.run_agent_turn("hello", [], stop_event=event)
        planner.assert_not_called()
        self.assertEqual(final_message(step_log), CANCELLED)

    def test_stop_after_the_llm_prevents_tool_execution(self):
        event = threading.Event()

        def planner(_input, _context):
            event.set()
            return {"tool": "read_file", "input": "notes.txt"}, None

        with mock.patch.object(agent, "get_valid_action", side_effect=planner), \
                mock.patch("rag_tool.search_documents", return_value=""), \
                mock.patch.object(agent, "execute_tool") as execute:
            step_log = agent.run_agent_turn("hello", [], stop_event=event)
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
        run.assert_called_once_with("do it", on_step=None, stop_event=event,
                                    conversation_history=[], explicit_resume=True)

    def test_autonomous_preset_stop_returns_the_error(self):
        event = threading.Event()
        event.set()
        step_log = agent._run_autonomous_goal("goal", stop_event=event)
        self.assertIn(CANCELLED, "\n".join(step_log))

    def test_stop_terminates_a_running_python_process(self):
        import python_sandbox

        event = threading.Event()
        agent._set_stop_event(event)
        original_start = python_sandbox._start_process
        started_at = []

        def start_and_schedule_cancel(*args, **kwargs):
            handles = original_start(*args, **kwargs)
            started_at.append(time.monotonic())
            threading.Timer(0.3, event.set).start()
            return handles

        with mock.patch.object(python_sandbox, "_start_process",
                               side_effect=start_and_schedule_cancel):
            out = agent.execute_python("import time; time.sleep(30)")
        self.assertEqual(out, CANCELLED)
        self.assertTrue(started_at)
        self.assertLess(time.monotonic() - started_at[0], 5)

    def test_cancelled_python_does_not_count_as_a_failed_run(self):
        agent.reset_python_failures()
        event = threading.Event()
        agent._set_stop_event(event)
        threading.Timer(0.3, event.set).start()
        agent.execute_python("import time; time.sleep(30)")
        self.assertEqual(agent.python_failure_count(), 0)


if __name__ == "__main__":
    unittest.main()
