"""Tests for audit_tools.py using a fake agent module.

Run: python -m unittest tests.test_audit_tools -v
"""
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import audit_tools  # noqa: E402


def fake_agent(tools):
    module = types.ModuleType("fake_agent")
    module.VALID_TOOLS = set(tools)
    for name, value in tools.items():
        setattr(module, name, value)
    return module


class FakeVolume:
    def GetMasterVolumeLevelScalar(self):
        return 0.4

    def GetMute(self):
        return 0


class AuditToolTests(unittest.TestCase):
    def test_callable_tool_is_ok(self):
        module = fake_agent({"alpha": lambda: None})
        self.assertEqual(audit_tools.audit_tool("alpha", module), ("OK", ""))

    def test_absent_tool_is_missing(self):
        module = fake_agent({"alpha": lambda: None})
        status, detail = audit_tools.audit_tool("beta", module)
        self.assertEqual(status, "MISSING")
        self.assertIn("beta", detail)

    def test_non_callable_implementation_is_missing(self):
        module = fake_agent({"alpha": "not a function"})
        status, detail = audit_tools.audit_tool("alpha", module)
        self.assertEqual(status, "MISSING")
        self.assertIn("not callable", detail)

    def test_special_targets_resolve_aliases(self):
        module = fake_agent({"remember": None})
        module.remember_fact = lambda fact: "ok"
        self.assertEqual(
            audit_tools.audit_tool("remember", module), ("OK", ""))

    def test_special_target_missing_is_reported(self):
        module = fake_agent({"load_skill": None})
        status, detail = audit_tools.audit_tool("load_skill", module)
        self.assertEqual(status, "MISSING")
        self.assertIn("load_skill_tool", detail)

    def test_volume_probe_reports_a_broken_audio_device(self):
        def broken_interface():
            raise AttributeError(
                "'AudioDevice' object has no attribute 'Activate'")

        module = fake_agent({"set_volume": lambda value: ""})
        module.PYCAW_AVAILABLE = True
        module._get_volume_interface = broken_interface

        status, detail = audit_tools.audit_tool("set_volume", module)

        self.assertEqual(status, "ERROR")
        self.assertIn("Activate", detail)

    def test_volume_probe_reads_without_changing_the_device(self):
        module = fake_agent({"set_volume": lambda value: ""})
        module.PYCAW_AVAILABLE = True
        module._get_volume_interface = lambda: FakeVolume()

        self.assertEqual(
            audit_tools.audit_tool("set_volume", module), ("OK", ""))

    def test_volume_probe_reports_missing_pycaw(self):
        module = fake_agent({"set_volume": lambda value: ""})
        module.PYCAW_AVAILABLE = False

        status, detail = audit_tools.audit_tool("set_volume", module)

        self.assertEqual(status, "ERROR")
        self.assertIn("pycaw", detail)

    def test_media_probe_checks_keys_without_pressing_them(self):
        module = fake_agent({"media_control": lambda value: ""})
        module.pyautogui = types.SimpleNamespace(
            KEYBOARD_KEYS=["playpause", "nexttrack", "prevtrack"])

        status, detail = audit_tools.audit_tool("media_control", module)

        self.assertEqual(status, "ERROR")
        self.assertIn("stop", detail)

    def test_media_probe_passes_when_all_keys_exist(self):
        module = fake_agent({"media_control": lambda value: ""})
        module.pyautogui = types.SimpleNamespace(
            KEYBOARD_KEYS=["playpause", "nexttrack", "prevtrack", "stop"])

        self.assertEqual(
            audit_tools.audit_tool("media_control", module), ("OK", ""))

    def test_audit_covers_every_valid_tool_in_sorted_order(self):
        module = fake_agent({
            "zeta": lambda: None,
            "alpha": lambda: None,
            "missing": None,
        })

        results = audit_tools.audit(module)

        self.assertEqual(
            [name for name, _status, _detail in results],
            ["alpha", "missing", "zeta"])
        statuses = {name: status for name, status, _ in results}
        self.assertEqual(statuses["alpha"], "OK")
        self.assertEqual(statuses["missing"], "MISSING")


if __name__ == "__main__":
    unittest.main()
