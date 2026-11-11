import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import kestrel_run as kr  # noqa: E402


FUTURE = "2099-01-01T00:00:00Z"


def write_scope(root, **overrides):
    data = {
        "version": 1,
        "expires": FUTURE,
        "allow_public": False,
        "networks": ["192.168.56.0/24"],
        "domains": ["lab.local", "*.lab.local"],
    }
    data.update(overrides)
    path = Path(root) / "scope.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_loads_a_valid_scope(self):
        scope = kr.load_scope(write_scope(self.temp.name))
        self.assertEqual(str(scope.networks[0]), "192.168.56.0/24")
        self.assertFalse(scope.allow_public)

    def test_rejects_an_expired_scope(self):
        path = write_scope(self.temp.name, expires="2000-01-01T00:00:00Z")
        with self.assertRaises(kr.PolicyError):
            kr.load_scope(path)

    def test_rejects_malformed_scope(self):
        path = Path(self.temp.name) / "scope.json"
        path.write_text("{}", encoding="utf-8")
        with self.assertRaises(kr.PolicyError):
            kr.load_scope(path)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.scope = kr.load_scope(write_scope(self.temp.name))

    def validate(self, argv):
        return kr.validate_steps([{"argv": argv, "timeout": 5}], self.scope)

    def test_allows_an_in_scope_private_ip(self):
        risk, targets = self.validate(["nmap", "-sV", "192.168.56.10"])
        self.assertEqual(risk, "recon")
        self.assertEqual(targets, ["192.168.56.10"])

    def test_denies_a_public_ip(self):
        with self.assertRaises(kr.PolicyError):
            self.validate(["nmap", "-sV", "8.8.8.8"])

    def test_denies_an_out_of_scope_private_ip(self):
        with self.assertRaises(kr.PolicyError):
            self.validate(["nmap", "-sV", "10.0.0.5"])

    def test_requires_the_whole_network_to_be_in_scope(self):
        self.validate(["nmap", "192.168.56.0/24"])
        with self.assertRaises(kr.PolicyError):
            self.validate(["nmap", "192.168.56.0/16"])

    def test_allows_a_wildcard_domain(self):
        self.validate(["curl", "http://app.lab.local/"])

    def test_denies_a_public_domain(self):
        with self.assertRaises(kr.PolicyError):
            self.validate(["curl", "http://example.com/"])

    def test_denies_a_non_allowlisted_binary(self):
        with self.assertRaises(kr.PolicyError):
            self.validate(["bash", "-c", "id"])

    def test_denies_target_list_files(self):
        with self.assertRaises(kr.PolicyError):
            self.validate(["nmap", "-iL", "targets.txt", "192.168.56.10"])

    def test_denies_curl_uploads(self):
        with self.assertRaises(kr.PolicyError):
            self.validate(["curl", "-T", "secret.txt", "http://app.lab.local/"])

    def test_denies_shell_control_characters(self):
        with self.assertRaises(kr.PolicyError):
            self.validate(["nmap", "192.168.56.10;id"])

    def test_returns_the_highest_risk(self):
        risk, _ = kr.validate_steps([
            {"argv": ["nmap", "-sV", "192.168.56.10"], "timeout": 5},
            {"argv": ["ffuf", "-w", "/tmp/w", "-u", "http://app.lab.local/FUZZ"],
             "timeout": 5},
        ], self.scope)
        self.assertEqual(risk, "intrusive")

    def test_searchsploit_needs_no_target(self):
        risk, targets = self.validate(["searchsploit", "apache"])
        self.assertEqual(risk, "recon")
        self.assertEqual(targets, [])

    def test_path_flags_are_not_targets(self):
        risk, targets = self.validate([
            "ffuf", "-w", "/usr/share/wordlists/x",
            "-u", "http://app.lab.local/FUZZ", "-o", "/tmp/kestrel/out.json",
        ])
        self.assertEqual(risk, "intrusive")
        self.assertEqual(targets, ["app.lab.local"])

    def test_output_paths_must_stay_under_the_output_root(self):
        self.validate(["nmap", "-sV", "-oN", "/tmp/kestrel/scan.txt",
                       "192.168.56.10"])
        self.validate(["whatweb",
                       "--log-brief=/tmp/kestrel/whatweb.log",
                       "http://app.lab.local/"])
        for argv in (
            ["nmap", "-sV", "-oN", "/home/kestrel/.ssh/authorized_keys",
             "192.168.56.10"],
            ["ffuf", "-w", "/tmp/w", "-u", "http://app.lab.local/FUZZ",
             "-o", "/var/log/kestrel/audit.jsonl"],
            ["whatweb", "--log-brief=/home/kestrel/x",
             "http://app.lab.local/"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(kr.PolicyError):
                    self.validate(argv)

    def test_relative_outputs_resolve_under_the_output_root(self):
        self.validate(["nmap", "-sV", "-oN", "scan.txt", "192.168.56.10"])
        with self.assertRaises(kr.PolicyError):
            self.validate(["nmap", "-sV", "-oN", "../../etc/passwd",
                           "192.168.56.10"])

    def test_denies_curl_remote_name_and_proxies(self):
        for argv in (
            ["curl", "-O", "http://app.lab.local/x"],
            ["curl", "-sO", "http://app.lab.local/x"],
            ["curl", "-x", "http://8.8.8.8:8080",
             "http://app.lab.local/"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(kr.PolicyError):
                    self.validate(argv)

    def test_resolver_and_host_port_targets_are_validated(self):
        self.validate(["dig", "@192.168.56.10", "lab.local"])
        self.validate(["openssl", "s_client", "-connect",
                       "192.168.56.10:443"])
        for argv in (
            ["dig", "@8.8.8.8", "lab.local"],
            ["openssl", "s_client", "-connect", "8.8.8.8:443"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(kr.PolicyError):
                    self.validate(argv)

    def test_openssl_subcommands_are_limited(self):
        self.validate(["openssl", "x509", "-in", "/tmp/kestrel/cert.pem",
                       "-noout"])
        with self.assertRaises(kr.PolicyError):
            self.validate(["openssl", "enc", "-in", "/etc/hostname",
                           "-out", "/tmp/kestrel/x"])

    def test_nmap_scripts_are_limited_to_builtin_names(self):
        self.validate(["nmap", "-sV", "--script", "safe,http-title",
                       "192.168.56.10"])
        for argv in (
            ["nmap", "-sV", "--script", "exploit", "192.168.56.10"],
            ["nmap", "-sV", "--script", "/tmp/kestrel/evil",
             "192.168.56.10"],
            ["nmap", "-sV", "--datadir", "/tmp/kestrel", "192.168.56.10"],
            ["nmap", "-sV", "--script-args-file", "/tmp/kestrel/args",
             "192.168.56.10"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(kr.PolicyError):
                    self.validate(argv)

    def test_custom_nuclei_templates_and_proxies_are_denied(self):
        for argv in (
            ["nuclei", "-u", "http://app.lab.local/", "-t",
             "/tmp/kestrel/evil.yaml"],
            ["nuclei", "-u", "http://app.lab.local/", "-code"],
            ["nuclei", "-u", "http://app.lab.local/", "-proxy",
             "http://8.8.8.8:8080"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(kr.PolicyError):
                    self.validate(argv)

    def test_scanner_proxy_flags_are_denied(self):
        for argv in (
            ["ffuf", "-w", "/tmp/w", "-u", "http://app.lab.local/FUZZ",
             "-x", "http://8.8.8.8:8080"],
            ["gobuster", "dir", "-u", "http://app.lab.local/", "-w",
             "/tmp/w", "--proxy", "http://8.8.8.8:8080"],
            ["nikto", "-h", "192.168.56.10", "-useproxy",
             "http://8.8.8.8:8080"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(kr.PolicyError):
                    self.validate(argv)

    def test_the_child_environment_is_minimal(self):
        env = kr._safe_env()
        self.assertEqual(env["HOME"], str(kr.OUTPUT_ROOT))
        self.assertNotIn("http_proxy", env)
        self.assertNotIn("https_proxy", env)


class ExecutionTests(unittest.TestCase):
    def test_timeout_kills_the_process(self):
        started = time.monotonic()
        result = kr.run_step(
            [sys.executable, "-c", "import time; time.sleep(5)"], 0.3)
        self.assertTrue(result["timed_out"])
        self.assertLess(time.monotonic() - started, 3.0)

    def test_output_is_capped(self):
        result = kr.run_step(
            [sys.executable, "-c", "print('x' * 400000)"], 10)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["stdout"]), kr.MAX_STREAM_BYTES)

    def test_a_failed_step_stops_the_sequence(self):
        results = kr.run_steps([
            {"argv": [sys.executable, "-c", "raise SystemExit(2)"],
             "timeout": 5},
            {"argv": [sys.executable, "-c", "print('never')"], "timeout": 5},
        ], 30)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["exit_code"], 2)

    def test_two_steps_run_in_order(self):
        results = kr.run_steps([
            {"argv": [sys.executable, "-c", "print('one')"], "timeout": 5},
            {"argv": [sys.executable, "-c", "print('two')"], "timeout": 5},
        ], 30)
        self.assertEqual(
            [result["stdout"].strip() for result in results],
            ["one", "two"],
        )


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.scope = write_scope(self.temp.name)
        self.audit = Path(self.temp.name) / "audit.jsonl"
        self.env = {
            "KESTREL_SCOPE": str(self.scope),
            "KESTREL_AUDIT": str(self.audit),
        }

    def run_cli(self, payload):
        return subprocess.run(
            [sys.executable, str(ROOT / "kestrel_run.py")],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=dict(os.environ, **self.env),
            timeout=30,
        )

    def test_scope_mode_returns_the_scope(self):
        completed = self.run_cli({"version": 1, "mode": "scope"})
        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertIn("192.168.56.0/24", payload["scope"]["networks"])

    def test_check_mode_does_not_execute(self):
        completed = self.run_cli({
            "version": 1,
            "mode": "check",
            "objective": "scan",
            "steps": [{"argv": ["nmap", "-sV", "192.168.56.10"],
                       "timeout": 5}],
            "total_timeout": 10,
        })
        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["risk"], "recon")
        self.assertNotIn("results", payload)

    def test_denied_binary_exits_two(self):
        completed = self.run_cli({
            "version": 1,
            "mode": "execute",
            "objective": "bad",
            "steps": [{"argv": ["bash", "-c", "id"], "timeout": 5}],
            "total_timeout": 10,
        })
        self.assertEqual(completed.returncode, 2)
        self.assertTrue(json.loads(completed.stdout)["denied"])

    def test_audit_contains_hashes_not_stdout(self):
        self.run_cli({
            "version": 1,
            "mode": "check",
            "objective": "scan",
            "steps": [{"argv": ["nmap", "-sV", "192.168.56.10"],
                       "timeout": 5}],
            "total_timeout": 10,
        })
        record = json.loads(self.audit.read_text().splitlines()[0])
        self.assertEqual(record["event"], "check")
        self.assertNotIn("stdout", record)
