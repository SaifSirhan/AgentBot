import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import config

with mock.patch.object(config, "load_config", return_value=dict(config.DEFAULTS)):
    import agent


class BatteryHealthTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(agent, "note_tool_result")
        patcher.start()
        self.addCleanup(patcher.stop)
        agent._clear_turn_route()
        self.addCleanup(agent._clear_turn_route)
        patcher = mock.patch.object(agent, "GROUP_MODE", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_report(self, xml, error=None):
        reports = []

        def powercfg(command, **kwargs):
            self.assertEqual(command, ["powercfg", "/batteryreport", "/xml",
                                       "/output", "battery.xml"])
            self.assertFalse(kwargs.get("shell", False))
            self.assertGreater(kwargs["timeout"], 0)
            report = Path(kwargs["cwd"]) / "battery.xml"
            self.assertNotEqual(report.parent, Path.cwd())
            reports.append(report)
            report.write_text(xml, encoding="utf-8")
            if error is not None:
                raise error
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(agent.subprocess, "run", side_effect=powercfg):
            result = agent.execute_tool({"tool": "check_battery_health", "input": ""})
        self.assertEqual(len(reports), 1, result)
        self.assertFalse(reports[0].exists(), "Battery XML must always be removed")
        return result

    def test_reports_health_from_namespaced_installed_battery(self):
        result = self.run_report('''<BatteryReport xmlns="http://schemas.microsoft.com/battery/2012">
            <Batteries><Battery><DesignCapacity>50000</DesignCapacity>
            <FullChargeCapacity>42500</FullChargeCapacity></Battery></Batteries>
            <CapacityHistory><Entry><DesignCapacity>1</DesignCapacity>
            <FullChargeCapacity>1</FullChargeCapacity></Entry></CapacityHistory>
            </BatteryReport>''')
        self.assertEqual(result,
                         "Battery Health: 85% (Design: 50,000 mWh, Current: 42,500 mWh)")

    def test_multiple_batteries_use_combined_capacity(self):
        result = self.run_report('''<BatteryReport><Batteries>
            <Battery><DesignCapacity>50000</DesignCapacity>
            <FullChargeCapacity>42500</FullChargeCapacity></Battery>
            <Battery><DesignCapacity>10000</DesignCapacity>
            <FullChargeCapacity>2500</FullChargeCapacity></Battery>
            </Batteries></BatteryReport>''')
        self.assertEqual(result,
                         "Battery Health: 75% (Design: 60,000 mWh, Current: 45,000 mWh)")

    def test_invalid_or_missing_capacity_is_an_error_and_cleans_up(self):
        for capacities in ("", "<DesignCapacity>0</DesignCapacity><FullChargeCapacity>10</FullChargeCapacity>",
                           "<DesignCapacity>50000</DesignCapacity>",
                           "<DesignCapacity>unknown</DesignCapacity><FullChargeCapacity>10</FullChargeCapacity>",
                           "<DesignCapacity>50000</DesignCapacity><FullChargeCapacity>-1</FullChargeCapacity>"):
            with self.subTest(capacities=capacities):
                result = self.run_report(
                    f"<BatteryReport><Batteries><Battery>{capacities}</Battery></Batteries></BatteryReport>")
                self.assertTrue(result.startswith("ERROR:"), result)
                self.assertNotIn("Battery Health:", result)

    def test_no_battery_and_malformed_xml_are_errors(self):
        for xml in ("<BatteryReport><Batteries/></BatteryReport>", "<broken"):
            with self.subTest(xml=xml):
                self.assertTrue(self.run_report(xml).startswith("ERROR:"))

    def test_powercfg_failure_preserves_stderr_and_cleans_up(self):
        result = self.run_report("partial", subprocess.CalledProcessError(
            1, "powercfg", stderr="Access is denied."))
        self.assertIn("ERROR:", result)
        self.assertIn("Access is denied.", result)

    def test_timeout_and_missing_executable_are_errors_and_clean_up(self):
        for error in (subprocess.TimeoutExpired("powercfg", 30),
                      FileNotFoundError("powercfg was not found")):
            with self.subTest(error=error):
                result = self.run_report("partial", error)
                self.assertTrue(result.startswith("ERROR:"), result)

    def test_battery_goal_routes_to_native_tool_and_parses(self):
        agent._set_turn_route("check my battery health")
        action, error = agent.parse_action(
            '{"tool":"check_battery_health","input":""}')
        self.assertIsNone(error)
        self.assertEqual(action["tool"], "check_battery_health")
        self.assertIn("check_battery_health", agent.TOOL_DESCRIPTIONS)

    def test_group_mode_cannot_expose_local_battery_health(self):
        with mock.patch.object(agent, "GROUP_MODE", True), \
                mock.patch.object(agent.subprocess, "run") as run:
            result = agent.execute_tool({"tool": "check_battery_health", "input": ""})
        self.assertIn("disabled in group chats", result)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
