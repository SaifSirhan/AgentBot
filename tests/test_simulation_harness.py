import tempfile
import unittest
from pathlib import Path

from tests import simulation_harness as harness


class SimulationHarnessTests(unittest.TestCase):
    def test_seeded_generator_makes_100_reproducible_varied_tasks(self):
        tasks = harness.generate_tasks(seed=30)
        self.assertEqual(len(tasks), 100)
        self.assertEqual(tasks, harness.generate_tasks(seed=30))
        self.assertNotEqual(tasks, harness.generate_tasks(seed=31))
        categories = {task["category"] for task in tasks}
        self.assertTrue({"files", "desktop", "web", "stremio"} <= categories)
        self.assertEqual(len({task["id"] for task in tasks}), 100)

    def test_mixed_run_reports_injected_failures_and_keeps_running(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "simulation_report.txt"
            result = harness.run_simulation(report, seed=30, count=20)
            text = report.read_text(encoding="utf-8")
        self.assertEqual(len(result["cases"]), 20)
        self.assertEqual(result["summary"]["ok"], 12)
        self.assertEqual(result["summary"]["permission_errors"], 2)
        self.assertEqual(result["summary"]["failed_tool_calls"], 2)
        self.assertEqual(result["summary"]["crashes"], 1)
        self.assertEqual(result["summary"]["empty_responses"], 1)
        self.assertEqual(result["summary"]["malformed_responses"], 1)
        self.assertEqual(result["summary"]["cancelled"], 1)
        self.assertEqual(result["summary"]["unexpected_outcomes"], 0)
        self.assertIn("OFFLINE", text)
        self.assertIn("not exhaustive", text)
        self.assertIn("SUMMARY", text)
        self.assertIn("injected", text.lower())

    def test_real_parser_and_turn_loop_produce_tool_evidence(self):
        task = harness.generate_tasks(seed=30, count=1)[0]
        task["fault"] = None
        with harness.isolated_agent() as agent:
            result = harness.run_case(agent, task)
        self.assertEqual(result["issues"], [])
        self.assertEqual([call["tool"] for call in result["tools"]],
                         [action["tool"] for action in task["actions"]])
        self.assertTrue(any(line.startswith("Action:") for line in result["steps"]))
        self.assertGreater(result["model_calls"], 0)

    def test_empty_model_response_cannot_be_scored_as_success(self):
        task = harness.generate_tasks(seed=30, count=1)[0]
        task["fault"] = "empty"
        with harness.isolated_agent() as agent:
            result = harness.run_case(agent, task)
        self.assertIn("empty_responses", result["issues"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["tools"], [])

    def test_sandbox_blocks_real_process_and_network_paths(self):
        import subprocess
        import socket
        with harness.isolated_agent():
            with self.assertRaises(harness.SimulationSafetyError):
                subprocess.Popen(["notepad"])
            with self.assertRaises(harness.SimulationSafetyError):
                socket.create_connection(("127.0.0.1", 1))

    def test_report_does_not_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "simulation_report.txt"
            report.write_text("keep me", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                harness.run_simulation(report, count=1)
            self.assertEqual(report.read_text(encoding="utf-8"), "keep me")


if __name__ == "__main__":
    unittest.main()
