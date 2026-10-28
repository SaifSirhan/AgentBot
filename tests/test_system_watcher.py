import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
import system_watcher

with mock.patch.object(config, "load_config", return_value=dict(config.DEFAULTS)):
    import agent


class SystemWatcherTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(agent, "note_tool_result")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.threads = []
        real_thread = threading.Thread
        self.thread_type = real_thread

        def make_thread(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            self.threads.append(thread)
            return thread

        patcher = mock.patch.object(system_watcher, "threading", SimpleNamespace(Thread=make_thread))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(system_watcher, "Notification")
        self.notification = patcher.start()
        self.addCleanup(patcher.stop)

    def wait_for_watch(self):
        self.assertEqual(len(self.threads), 1)
        self.assertTrue(self.threads[0].daemon)
        self.threads[0].join(timeout=2)
        self.assertFalse(self.threads[0].is_alive(), "One-shot watcher did not terminate")

    def test_battery_polls_every_30_seconds_until_full_then_alerts_once(self):
        samples = [SimpleNamespace(percent=value) for value in (98, 98, 99.5, 100)]
        with mock.patch.object(system_watcher.psutil, "sensors_battery", side_effect=samples), \
                mock.patch.object(system_watcher.time, "sleep") as sleep:
            result = system_watcher.start_watch("battery", 100, "Battery is fully charged!")
            self.wait_for_watch()
        self.assertIn("Watching battery", result)
        self.assertEqual(sleep.call_args_list, [mock.call(30), mock.call(30)])
        self.notification.assert_called_once_with(
            app_id="Agent", title="System monitor", msg="Battery is fully charged!", duration="short")
        self.notification.return_value.show.assert_called_once_with()

    def test_cpu_crossing_threshold_triggers_without_exact_equality(self):
        with mock.patch.object(system_watcher.psutil, "cpu_percent", side_effect=[89, 92]) as cpu, \
                mock.patch.object(system_watcher.time, "sleep") as sleep:
            result = system_watcher.start_watch("cpu", 90, "CPU usage is high")
            self.wait_for_watch()
        self.assertIn("Watching cpu", result)
        self.assertEqual(cpu.call_args_list, [mock.call(interval=1), mock.call(interval=1)])
        sleep.assert_called_once_with(30)
        self.notification.return_value.show.assert_called_once_with()

    def test_ram_at_threshold_alerts_immediately(self):
        with mock.patch.object(system_watcher.psutil, "virtual_memory",
                               side_effect=[SimpleNamespace(percent=80)]), \
                mock.patch.object(system_watcher.time, "sleep") as sleep:
            result = system_watcher.start_watch(" RAM ", "80", "RAM is high")
            self.wait_for_watch()
        self.assertIn("Watching ram", result)
        sleep.assert_not_called()
        self.notification.return_value.show.assert_called_once_with()

    def test_start_returns_while_daemon_is_waiting(self):
        entered = threading.Event()
        release = threading.Event()

        def wait(_seconds):
            entered.set()
            if not release.wait(timeout=2):
                raise RuntimeError("Test did not release watcher")

        with mock.patch.object(system_watcher.psutil, "virtual_memory", side_effect=[
                SimpleNamespace(percent=40), SimpleNamespace(percent=90)]), \
                mock.patch.object(system_watcher.time, "sleep", side_effect=wait):
            try:
                result = system_watcher.start_watch("ram", 90, "High RAM")
                self.assertIn("Watching ram", result)
                self.assertTrue(entered.wait(timeout=1))
                self.assertTrue(self.threads[0].is_alive())
                self.assertTrue(self.threads[0].daemon)
                self.notification.assert_not_called()
            finally:
                release.set()
                for thread in self.threads:
                    thread.join(timeout=2)
        self.notification.return_value.show.assert_called_once_with()

    def test_invalid_requests_do_not_start_threads(self):
        cases = [
            ("disk", 90, "alert"), (None, 90, "alert"),
            ("cpu", -1, "alert"), ("cpu", 101, "alert"),
            ("cpu", "not a number", "alert"), ("cpu", "nan", "alert"),
            ("ram", float("inf"), "alert"), ("cpu", True, "alert"),
            ("ram", None, "alert"), ("ram", 90, "  "), ("ram", 90, None),
        ]
        for args in cases:
            with self.subTest(args=args):
                result = system_watcher.start_watch(*args)
                self.assertTrue(result.startswith("ERROR:"), result)
        self.assertEqual(self.threads, [])
        self.notification.assert_not_called()

    def test_no_battery_fails_before_starting_thread(self):
        with mock.patch.object(system_watcher.psutil, "sensors_battery", return_value=None):
            result = system_watcher.start_watch("battery", 100, "Charged")
        self.assertIn("ERROR:", result)
        self.assertIn("battery", result.lower())
        self.assertEqual(self.threads, [])

    def test_missing_notification_backend_is_reported(self):
        with mock.patch.object(system_watcher, "Notification", None):
            result = system_watcher.start_watch("cpu", 90, "High CPU")
        self.assertIn("ERROR:", result)
        self.assertEqual(self.threads, [])

    def test_sampling_failure_is_logged_and_stops_without_notification(self):
        with mock.patch.object(system_watcher.psutil, "virtual_memory",
                               side_effect=RuntimeError("Sensor unavailable")), \
                self.assertLogs("system_watcher", level="ERROR") as logs:
            system_watcher.start_watch("ram", 80, "RAM alert")
            self.wait_for_watch()
        self.assertIn("Sensor unavailable", "\n".join(logs.output))
        self.notification.assert_not_called()

    def test_task_loop_starts_real_watcher_through_dispatch(self):
        replies = iter([
            '{"tool":"watch_system_condition","input":"ram|80|RAM is high"}',
            'DONE',
            '{"achieved":true,"evidence":"Background RAM watch registered"}',
        ])
        with mock.patch.object(agent, "GROUP_MODE", False), \
                mock.patch.object(agent, "ask_llm_direct", side_effect=lambda _: next(replies)), \
                mock.patch.object(system_watcher.psutil, "virtual_memory",
                                  side_effect=[SimpleNamespace(percent=80)]):
            step_log = agent.run_agent_turn("/task notify me when RAM reaches 80%", [])
            self.wait_for_watch()
        self.assertIn("Watching ram for 80%", "\n".join(step_log))
        self.assertIn("Goal independently verified", "\n".join(step_log))
        self.notification.return_value.show.assert_called_once_with()

    def test_battery_disappearing_stops_and_logs_error(self):
        with mock.patch.object(system_watcher.psutil, "sensors_battery",
                               side_effect=[SimpleNamespace(percent=50), None]), \
                self.assertLogs("system_watcher", level="ERROR") as logs:
            system_watcher.start_watch("battery", 100, "Charged")
            self.wait_for_watch()
        self.assertIn("Battery is no longer available", "\n".join(logs.output))
        self.notification.assert_not_called()

    def test_battery_sensor_failure_is_reported_before_startup(self):
        with mock.patch.object(system_watcher.psutil, "sensors_battery",
                               side_effect=OSError("Battery sensor unavailable")):
            result = system_watcher.start_watch("battery", 100, "Charged")
        self.assertIn("ERROR: Cannot read battery status: Battery sensor unavailable", result)
        self.assertEqual(self.threads, [])

    def test_thread_start_failure_does_not_claim_a_watch_is_active(self):
        with mock.patch.object(self.thread_type, "start", side_effect=RuntimeError("Cannot create thread")):
            result = system_watcher.start_watch("ram", 80, "High RAM")
        self.assertIn("ERROR: Cannot start system watcher: Cannot create thread", result)
        self.notification.assert_not_called()

    def test_notification_failure_is_logged_and_not_retried(self):
        self.notification.return_value.show.side_effect = RuntimeError("Toast service unavailable")
        with mock.patch.object(system_watcher.psutil, "virtual_memory",
                               return_value=SimpleNamespace(percent=90)), \
                self.assertLogs("system_watcher", level="ERROR") as logs:
            system_watcher.start_watch("ram", 80, "RAM alert")
            self.wait_for_watch()
        self.assertIn("Toast service unavailable", "\n".join(logs.output))
        self.notification.return_value.show.assert_called_once_with()


class SystemWatcherToolTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(agent, "note_tool_result")
        patcher.start()
        self.addCleanup(patcher.stop)
        agent._clear_turn_route()
        self.addCleanup(agent._clear_turn_route)
        patcher = mock.patch.object(agent, "GROUP_MODE", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_dispatch_passes_condition_target_and_complete_message(self):
        with mock.patch.object(system_watcher, "start_watch", return_value="Watching battery") as start:
            result = agent.execute_tool({
                "tool": "watch_system_condition", "input": "battery|100|Charged | unplug now"})
        self.assertEqual(result, "Watching battery")
        start.assert_called_once_with("battery", "100", "Charged | unplug now")

    def test_malformed_tool_input_cannot_start_a_watcher(self):
        for value in ("", "battery|100", {"condition": "cpu"}):
            with self.subTest(value=value), \
                    mock.patch.object(system_watcher, "start_watch") as start:
                result = agent.execute_tool({"tool": "watch_system_condition", "input": value})
            self.assertTrue(result.startswith("ERROR:"), result)
            start.assert_not_called()

    def test_notification_requests_route_to_watcher(self):
        for goal in ("notify me when battery reaches 100%", "notify me when CPU goes above 90%",
                     "watch RAM usage and alert at 80%"):
            with self.subTest(goal=goal):
                agent._set_turn_route(goal)
                action, error = agent.parse_action(
                    '{"tool":"watch_system_condition","input":"cpu|90|High CPU"}')
                self.assertIsNone(error)
                self.assertEqual(action["tool"], "watch_system_condition")
        self.assertIn("watch_system_condition", agent.TOOL_DESCRIPTIONS)

    def test_group_mode_cannot_start_a_system_watcher(self):
        with mock.patch.object(agent, "GROUP_MODE", True), \
                mock.patch.object(system_watcher, "start_watch") as start:
            result = agent.execute_tool({"tool": "watch_system_condition", "input": "cpu|90|High CPU"})
        self.assertIn("disabled in group chats", result)
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
