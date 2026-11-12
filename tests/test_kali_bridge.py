import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import kali_bridge


def write_config(root, **overrides):
    data = {
        "host": "192.168.56.20",
        "port": 22,
        "user": "kestrel",
        "key_path": "C:/keys/id_ed25519",
        "known_hosts": "C:/keys/known_hosts",
        "connect_timeout": 10,
    }
    data.update(overrides)
    path = Path(root) / "kali.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class FakeRunner:
    def __init__(self, payload=None, returncode=0, stderr="", stdout=None):
        self.payload = payload
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        stdout = self.stdout
        if stdout is None:
            stdout = json.dumps(self.payload)
        return subprocess.CompletedProcess(
            argv,
            self.returncode,
            stdout=stdout,
            stderr=self.stderr,
        )


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_loads_required_fields(self):
        config = kali_bridge.load_config(write_config(self.temp.name))
        self.assertEqual(config.host, "192.168.56.20")
        self.assertEqual(config.user, "kestrel")
        self.assertEqual(config.port, 22)
        self.assertEqual(config.connect_timeout, 10)

    def test_rejects_missing_fields(self):
        path = Path(self.temp.name) / "kali.json"
        path.write_text("{}", encoding="utf-8")
        with self.assertRaises(kali_bridge.KaliUnavailable):
            kali_bridge.load_config(path)

    def test_rejects_non_object_config(self):
        path = Path(self.temp.name) / "kali.json"
        path.write_text("[]", encoding="utf-8")
        with self.assertRaises(kali_bridge.KaliUnavailable):
            kali_bridge.load_config(path)

    def test_rejects_invalid_port(self):
        with self.assertRaises(kali_bridge.KaliUnavailable):
            kali_bridge.load_config(write_config(self.temp.name, port=70000))

    def test_expands_environment_variables(self):
        path = write_config(
            self.temp.name,
            key_path="%KALI_TEST_ROOT%/id_ed25519",
            known_hosts="%KALI_TEST_ROOT%/known_hosts",
        )
        with mock.patch.dict(os.environ, {"KALI_TEST_ROOT": "C:/expanded"}):
            config = kali_bridge.load_config(path)
        self.assertEqual(config.key_path, "C:/expanded/id_ed25519")
        self.assertEqual(config.known_hosts, "C:/expanded/known_hosts")

    def test_config_path_uses_environment_override(self):
        with mock.patch.dict(
                os.environ, {kali_bridge.CONFIG_ENV: "C:/custom/kali.json"}):
            self.assertEqual(kali_bridge.config_path(),
                             Path("C:/custom/kali.json"))

    def test_builds_a_strict_ssh_command(self):
        config = kali_bridge.load_config(write_config(self.temp.name))
        argv = kali_bridge.ssh_argv(config)
        self.assertEqual(argv[0], "ssh")
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertIn("IdentitiesOnly=yes", argv)
        self.assertIn("UserKnownHostsFile=C:/keys/known_hosts", argv)
        self.assertIn("ConnectTimeout=10", argv)
        self.assertIn("kestrel@192.168.56.20", argv)
        self.assertIn("-T", argv)
        self.assertNotIn("-A", argv)
        self.assertFalse(any(value.lower().startswith("forwardagent=yes")
                             for value in argv))


class CallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = kali_bridge.load_config(write_config(self.temp.name))

    def sent_request(self, runner):
        sent = runner.calls[0][1]["input"]
        self.assertIsInstance(sent, bytes)
        return json.loads(sent.decode("utf-8"))

    def test_scope_sends_scope_mode(self):
        runner = FakeRunner({"ok": True, "scope": {"version": 1}})
        result = kali_bridge.scope(config=self.config, runner=runner)
        self.assertEqual(result, {"version": 1})
        request = self.sent_request(runner)
        self.assertEqual(request, {"version": 1, "mode": "scope"})
        self.assertEqual(runner.calls[0][0],
                         kali_bridge.ssh_argv(self.config))
        self.assertEqual(runner.calls[0][1]["timeout"], 15)

    def test_check_sends_steps_without_execution(self):
        runner = FakeRunner({"ok": True, "risk": "recon",
                             "targets": ["192.168.56.10"]})
        steps = [{"argv": ["nmap", "-sV", "192.168.56.10"], "timeout": 5}]
        result = kali_bridge.check("scan", steps, config=self.config,
                                   runner=runner)
        self.assertEqual(result["risk"], "recon")
        self.assertEqual(result["targets"], ["192.168.56.10"])
        request = self.sent_request(runner)
        self.assertEqual(request["mode"], "check")
        self.assertEqual(request["steps"], steps)
        self.assertEqual(request["objective"], "scan")
        self.assertEqual(request["total_timeout"], 300)
        self.assertEqual(runner.calls[0][1]["timeout"], 40)

    def test_task_marks_desktop_approval(self):
        runner = FakeRunner({"ok": True, "results": [], "risk": "recon"})
        kali_bridge.task("scan", [{"argv": ["nmap", "192.168.56.10"]}],
                         config=self.config, runner=runner)
        request = self.sent_request(runner)
        self.assertEqual(request["mode"], "execute")
        self.assertEqual(request["approval"], "desktop")
        self.assertEqual(runner.calls[0][1]["timeout"], 340)

    def test_task_returns_additive_result_fields_unchanged(self):
        payload = {
            "ok": True,
            "risk": "recon",
            "targets": ["192.168.56.10"],
            "read_errors": ["scope.json: missing"],
            "stderr_sha256": "0" * 64,
            "results": [
                {
                    "argv": ["nmap", "-sV", "192.168.56.10"],
                    "exit_code": 0,
                    "timed_out": False,
                    "truncated": False,
                    "stdout": "22/tcp open ssh",
                    "stderr": "",
                },
            ],
        }
        runner = FakeRunner(payload)
        result = kali_bridge.task(
            "scan", [{"argv": ["nmap", "-sV", "192.168.56.10"]}],
            config=self.config, runner=runner)
        self.assertEqual(result, payload)

    def test_policy_denial_is_distinct(self):
        runner = FakeRunner({"ok": False, "denied": True,
                             "reason": "target 8.8.8.8 is outside scope"},
                            returncode=2)
        with self.assertRaises(kali_bridge.KaliPolicyError) as caught:
            kali_bridge.check("bad", [{"argv": ["nmap", "8.8.8.8"]}],
                              config=self.config, runner=runner)
        self.assertIn("outside scope", str(caught.exception))

    def test_internal_error_is_not_a_policy_denial(self):
        runner = FakeRunner({"ok": False,
                             "error": "audit log unavailable"})
        with self.assertRaises(kali_bridge.KaliProtocolError) as caught:
            kali_bridge.task("scan", [{"argv": ["nmap", "192.168.56.10"]}],
                             config=self.config, runner=runner)
        self.assertIn("audit log unavailable", str(caught.exception))
        self.assertNotIsInstance(caught.exception,
                                 kali_bridge.KaliPolicyError)

    def test_failure_without_a_reason_uses_a_fallback(self):
        runner = FakeRunner({"ok": False})
        with self.assertRaises(kali_bridge.KaliProtocolError) as caught:
            kali_bridge.scope(config=self.config, runner=runner)
        self.assertIn("Kali refused the request", str(caught.exception))

    def test_invalid_json_is_a_protocol_error(self):
        runner = FakeRunner(stdout="{not json")
        with self.assertRaises(kali_bridge.KaliProtocolError):
            kali_bridge.scope(config=self.config, runner=runner)

    def test_non_object_response_is_a_protocol_error(self):
        runner = FakeRunner(stdout="[]")
        with self.assertRaises(kali_bridge.KaliProtocolError):
            kali_bridge.scope(config=self.config, runner=runner)

    def test_scope_success_without_scope_is_a_protocol_error(self):
        runner = FakeRunner({"ok": True})
        with self.assertRaises(kali_bridge.KaliProtocolError):
            kali_bridge.scope(config=self.config, runner=runner)

    def test_nonzero_exit_without_stdout_is_unavailable(self):
        runner = FakeRunner(stdout="", returncode=255,
                            stderr="ssh: connect to host: Connection refused")
        with self.assertRaises(kali_bridge.KaliUnavailable) as caught:
            kali_bridge.scope(config=self.config, runner=runner)
        self.assertIn("Connection refused", str(caught.exception))

    def test_missing_ssh_is_unavailable(self):
        def missing(*args, **kwargs):
            raise FileNotFoundError("ssh")
        with self.assertRaises(kali_bridge.KaliUnavailable):
            kali_bridge.scope(config=self.config, runner=missing)

    def test_timeout_is_unavailable(self):
        def timing_out(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
        with self.assertRaises(kali_bridge.KaliUnavailable):
            kali_bridge.scope(config=self.config, runner=timing_out)

    def test_os_error_is_unavailable(self):
        def broken(*args, **kwargs):
            raise PermissionError("denied")
        with self.assertRaises(kali_bridge.KaliUnavailable):
            kali_bridge.scope(config=self.config, runner=broken)

    def test_non_utf8_output_is_a_protocol_error(self):
        runner = FakeRunner(stdout=b'{"ok": true, "x": "\xff"}')
        with self.assertRaises(kali_bridge.KaliProtocolError):
            kali_bridge.scope(config=self.config, runner=runner)

    def test_byte_responses_are_decoded_as_utf8(self):
        payload = {"ok": True, "scope": {"note": "café — 日本語"}}
        runner = FakeRunner(stdout=json.dumps(payload).encode("utf-8"))
        result = kali_bridge.scope(config=self.config, runner=runner)
        self.assertEqual(result, {"note": "café — 日本語"})

    def test_unicode_objective_is_sent_as_utf8_bytes(self):
        objective = "enumerate café — naïve 日本語"
        runner = FakeRunner({"ok": True, "risk": "recon", "targets": []})
        kali_bridge.check(objective, [{"argv": ["nmap", "192.168.56.10"]}],
                          config=self.config, runner=runner)
        sent = runner.calls[0][1]["input"]
        self.assertIsInstance(sent, bytes)
        request = json.loads(sent.decode("utf-8"))
        self.assertEqual(request["objective"], objective)
        self.assertIsNone(runner.calls[0][1].get("text"))


class ExceptionContractTests(unittest.TestCase):
    def test_all_errors_subclass_kali_error(self):
        for error in (kali_bridge.KaliUnavailable,
                      kali_bridge.KaliPolicyError,
                      kali_bridge.KaliProtocolError):
            self.assertTrue(issubclass(error, kali_bridge.KaliError))
        self.assertTrue(issubclass(kali_bridge.KaliError, RuntimeError))

    def test_policy_error_is_not_unavailable(self):
        self.assertFalse(issubclass(kali_bridge.KaliPolicyError,
                                    kali_bridge.KaliUnavailable))
        self.assertFalse(issubclass(kali_bridge.KaliProtocolError,
                                    kali_bridge.KaliPolicyError))


if __name__ == "__main__":
    unittest.main()
