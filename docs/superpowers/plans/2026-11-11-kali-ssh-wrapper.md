# Kali SSH Wrapper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Kestrel one approved, policy-enforced way to run security tools in a Kali lab over SSH.

**Architecture:** Kestrel plans argv steps and sends them to a forced-command Python wrapper in Kali. The wrapper validates scope, binary allowlist, flags, and risk, executes without a shell, and returns JSON. A Qt dialog is the only approval surface; headless runs are refused.

**Tech Stack:** Python 3.11 standard library, Windows OpenSSH, PySide6, unittest, Kali rolling.

**Spec:** `docs/superpowers/specs/2026-11-11-kali-ssh-wrapper-design.md`

## Global Constraints

- Wrapper and Kestrel client use the Python standard library only; add no pip packages.
- Execute `argv` lists with `shell=False`; never pass a shell string.
- Kali owns the authoritative scope; Kestrel's scope read is advisory for planning.
- Default deny: public targets, non-allowlisted binaries, target-list files, destructive flags.
- `kali_task` sends `mode: "check"`, asks for Qt approval, then sends `mode: "execute"`. A denied or headless approval never executes.
- Static prompt must stay under 4,100 tokens. Kali tools are exposed only on security turns; never raise the budget.
- Authorization is limited to the user's own lab, CTF targets, and systems they own. No public targets in tests.
- Commit only files listed in each task. Do not bundle unrelated working-tree changes.
- No second AI and no phone work in this plan.

---

### Task 1: Kali policy wrapper and executor

**Files:**
- Create: `kali/kestrel_run.py`
- Create: `kali/tests/test_kestrel_run.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `load_scope(path) -> Scope`, `validate_steps(steps, scope) -> (risk, targets)`, `run_step(argv, timeout) -> dict`, `run_steps(steps, total_timeout) -> list[dict]`, `main(scope_path=None, audit_path=None) -> int`, exceptions `PolicyError` and `ProtocolError`.

- [ ] **Step 1: Write the failing wrapper tests**

Create `kali/tests/test_kestrel_run.py`:

> **Superseded by the hardening amendment (fix round 1):** this block is the original brief; `kali/kestrel_run.py` and `kali/tests/test_kestrel_run.py` on branch `kali-ssh-wrapper` are authoritative.

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest discover -s kali/tests -v`

Expected: `ModuleNotFoundError: No module named 'kestrel_run'`.

- [ ] **Step 3: Write the minimal wrapper**

Create `kali/kestrel_run.py`:

> **Superseded by the hardening amendment (fix round 1):** this block is the original brief; `kali/kestrel_run.py` on branch `kali-ssh-wrapper` is authoritative.

```python
#!/usr/bin/env python3
"""Kestrel's forced-command policy wrapper for the Kali lab."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SCOPE = Path(os.environ.get("KESTREL_SCOPE", "/etc/kestrel/scope.json"))
DEFAULT_AUDIT = Path(os.environ.get("KESTREL_AUDIT",
                                   "/var/log/kestrel/audit.jsonl"))
MAX_STEPS = 8
MAX_STEP_TIMEOUT = 600
MAX_TOTAL_TIMEOUT = 1800
MAX_STREAM_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024

RECON = frozenset({"nmap", "whatweb", "dig", "host", "whois", "curl",
                   "openssl", "searchsploit"})
INTRUSIVE = frozenset({"ffuf", "gobuster", "feroxbuster", "nuclei",
                       "nikto", "masscan", "nc"})
LOCAL_SAFE = frozenset({"searchsploit", "openssl"})
DENIED_BINARIES = frozenset({"bash", "sh", "zsh", "dash", "python3", "python",
                             "perl", "ruby", "rm", "dd", "mkfs", "mount",
                             "systemctl", "crontab"})
TIERS = {"recon": 0, "intrusive": 1, "credential": 2, "exploitation": 3}
DESTRUCTIVE_FLAGS = {
    "sqlmap": ("--os-shell", "--os-pwn", "--os-cmd", "--file-write",
               "--file-dest", "--sql-shell"),
    "curl": ("-T", "--upload-file"),
    "nc": ("-e", "--exec", "-c"),
}
DENIED_FLAGS = {
    # curl proxies and remote-name writes are exfiltration or write paths.
    "curl": ("-O", "--remote-name", "--output-dir", "-x", "--proxy",
             "--socks4", "--socks4a", "--socks5", "--socks5-hostname",
             "--preproxy", "--resolve", "--connect-to", "--unix-socket",
             "--abstract-unix-socket", "--dns-servers", "--dns-interface",
             "--dns-ipv4-addr", "--dns-ipv6-addr"),
    "ffuf": ("-x", "--proxy"),
    "gobuster": ("--proxy",),
    "feroxbuster": ("--proxy",),
    "nikto": ("-useproxy",),
    "nuclei": ("-proxy", "-t", "-templates", "--templates",
               "-workflow", "--workflow", "-code", "-headless"),
    "whatweb": ("--proxy", "--custom-plugin"),
    "nmap": ("--proxies", "--datadir", "--script-args-file",
             "--script-updatedb"),
    # Dynamic engines/providers load native code; s_client can use a proxy.
    "openssl": ("-engine", "-provider", "-provider-path", "-proxy"),
}
DANGEROUS_NSE = frozenset({"exploit", "brute", "dos", "malware", "fuzzer",
                           "external", "broadcast"})
ALLOWED_SUBCOMMANDS = {"openssl": frozenset({"s_client", "x509"})}
TARGET_LIST_FLAGS = {
    "nmap": ("-iL",),
    "nuclei": ("-l",),
    "ffuf": ("-request",),
    "curl": ("-K", "--config"),
}
# Flags whose next value is a path, not a target, so it is not scope-checked.
PATH_FLAGS = {
    "ffuf": ("-w", "--wordlist"),
    "gobuster": ("-w", "--wordlist"),
    "feroxbuster": ("-w", "--wordlist"),
    "openssl": ("-in", "-CAfile", "-cert", "-key", "-untrusted"),
}
OUTPUT_ROOT = Path(os.environ.get("KESTREL_OUTPUT", "/tmp/kestrel"))
OUTPUT_PATH_FLAGS = {
    "nmap": ("-oN", "-oX", "-oG", "-oA", "-oS"),
    "ffuf": ("-o",),
    "gobuster": ("-o",),
    "feroxbuster": ("--output", "-o"),
    "nuclei": ("--output", "-output", "-o"),
    "nikto": ("--output", "-output", "-o"),
    "masscan": ("-oX", "-oJ", "-oG", "-oL"),
    "curl": ("--output", "-o"),
    "whatweb": ("--log-brief", "--log-verbose", "--log-errors",
                "--log-json", "--log-xml"),
    "openssl": ("-out", "-keylogfile", "-writerand"),
}
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SHELL_CHARS = frozenset(";|&$`><\n\r")
HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}"
    r"(?:\.[A-Za-z0-9-]{1,63})+$"
)


class PolicyError(RuntimeError):
    """The request violates the lab policy."""


class ProtocolError(RuntimeError):
    """The request is not valid protocol version 1."""


@dataclass(frozen=True)
class Scope:
    expires: str
    allow_public: bool
    networks: tuple
    domains: tuple

    def as_dict(self):
        return {
            "version": 1,
            "expires": self.expires,
            "allow_public": self.allow_public,
            "networks": [str(network) for network in self.networks],
            "domains": list(self.domains),
        }


def load_scope(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PolicyError("scope file is missing or invalid") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise PolicyError("scope file must have version 1")
    expires = data.get("expires")
    if not isinstance(expires, str):
        raise PolicyError("scope expiry is missing")
    try:
        expiry = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicyError("scope expiry is invalid") from exc
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expiry.astimezone(timezone.utc):
        raise PolicyError("scope has expired")
    networks = data.get("networks", [])
    domains = data.get("domains", [])
    if not isinstance(networks, list) or not isinstance(domains, list):
        raise PolicyError("scope networks and domains must be lists")
    try:
        parsed_networks = tuple(
            ipaddress.ip_network(str(item), strict=False) for item in networks
        )
    except ValueError as exc:
        raise PolicyError("scope contains an invalid network") from exc
    return Scope(
        expires=expires,
        allow_public=data.get("allow_public") is True,
        networks=parsed_networks,
        domains=tuple(str(item).lower().rstrip(".") for item in domains),
    )


def _public_ip(address):
    return not (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_multicast or address.is_reserved
        or address.is_unspecified
    )


def _private_domain(domain):
    return any(
        domain == suffix or domain.endswith("." + suffix)
        for suffix in ("local", "lab", "lan", "internal", "home")
    )


def _domain_allowed(domain, scope):
    for pattern in scope.domains:
        if pattern.startswith("*."):
            if domain.endswith(pattern[1:]):
                return True
        elif domain == pattern:
            return True
    return False


def _target_from_token(token):
    raw = token.strip()
    if not raw:
        return None
    if raw.startswith("@"):
        raw = raw[1:]
    if not raw:
        return None
    if raw.startswith("["):
        end = raw.find("]")
        if end == -1:
            return None
        try:
            return ("ip", ipaddress.ip_address(raw[1:end]))
        except ValueError:
            return None
    if "://" in raw:
        try:
            raw = urllib.parse.urlsplit(raw).hostname or ""
        except ValueError:
            return None
    if raw.count(":") == 1:
        host, port = raw.rsplit(":", 1)
        if host and port.isdigit():
            raw = host
    try:
        if "/" in raw:
            return ("network", ipaddress.ip_network(raw, strict=False))
        return ("ip", ipaddress.ip_address(raw))
    except ValueError:
        pass
    if HOST_RE.match(raw):
        return ("domain", raw.lower().rstrip("."))
    return None


def target_tokens(argv):
    binary = argv[0]
    path_flags = (tuple(PATH_FLAGS.get(binary, ()))
                  + tuple(OUTPUT_PATH_FLAGS.get(binary, ())))
    skip_next = False
    targets = []
    for token in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if token in path_flags:
            skip_next = True
            continue
        if any(token.startswith(flag + "=") for flag in path_flags):
            continue
        if token.startswith("-"):
            continue
        target = _target_from_token(token)
        if target is not None:
            targets.append(target)
    return targets


def _validate_target(kind, value, scope):
    if kind == "ip":
        if _public_ip(value) and not scope.allow_public:
            raise PolicyError("public IP %s is outside scope" % value)
        if not any(value in network for network in scope.networks):
            raise PolicyError("target %s is outside scope" % value)
        return str(value)
    if kind == "network":
        if _public_ip(value.network_address) and not scope.allow_public:
            raise PolicyError("public network %s is outside scope" % value)
        if not any(value.subnet_of(network) for network in scope.networks):
            raise PolicyError("target %s is outside scope" % value)
        return str(value)
    if not _private_domain(value) and not scope.allow_public:
        raise PolicyError("public domain %s is outside scope" % value)
    if not _domain_allowed(value, scope):
        raise PolicyError("target %s is outside scope" % value)
    return value


def _output_paths(argv, binary):
    flags = sorted(OUTPUT_PATH_FLAGS.get(binary, ()), key=len, reverse=True)
    for index, arg in enumerate(argv):
        for flag in flags:
            if arg == flag:
                yield argv[index + 1] if index + 1 < len(argv) else ""
                break
            if flag.startswith("--") and arg.startswith(flag + "="):
                yield arg.split("=", 1)[1]
                break
            if (not flag.startswith("--") and arg.startswith(flag)
                    and len(arg) > len(flag)):
                yield arg[len(flag):]
                break


def _output_path_ok(path):
    if not path or "~" in path or "\x00" in path:
        return False
    target = Path(path)
    if not target.is_absolute():
        target = Path(OUTPUT_ROOT) / target
    try:
        resolved = target.resolve()
        root = Path(OUTPUT_ROOT).resolve()
    except (OSError, RuntimeError):
        return False
    return resolved == root or root in resolved.parents


def _safe_env():
    env = {
        "PATH": SAFE_PATH,
        "HOME": str(OUTPUT_ROOT),
        "TMPDIR": str(OUTPUT_ROOT),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    # Windows needs these to start a child; they are absent on Kali.
    for key in ("SYSTEMROOT", "WINDIR"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def classify(argv):
    binary = argv[0]
    if "/" in binary or "\\" in binary:
        raise PolicyError("binary paths are not allowed")
    if binary in DENIED_BINARIES:
        raise PolicyError("binary %r is not allowed" % binary)
    if binary in RECON:
        tier = "recon"
    elif binary in INTRUSIVE:
        tier = "intrusive"
    else:
        raise PolicyError("binary %r is not allowed" % binary)

    subcommands = ALLOWED_SUBCOMMANDS.get(binary)
    if subcommands is not None and (
            len(argv) < 2 or argv[1] not in subcommands):
        raise PolicyError(
            "%s subcommand must be one of %s"
            % (binary, ", ".join(sorted(subcommands))))

    for flag in DENIED_FLAGS.get(binary, ()):
        if flag in argv or any(
                arg.startswith(flag + "=") for arg in argv):
            raise PolicyError("flag %s is not allowed" % flag)
    if binary == "curl" and any(
            re.match(r"^-[^-]*[xO]", arg) for arg in argv):
        raise PolicyError("curl -O and proxy short flags are not allowed")

    for flag in DESTRUCTIVE_FLAGS.get(binary, ()):
        if flag in argv:
            raise PolicyError("flag %s is not allowed" % flag)
    for flag in TARGET_LIST_FLAGS.get(binary, ()):
        if flag in argv or any(arg.startswith(flag + "=") for arg in argv):
            raise PolicyError(
                "flag %s is not allowed; write targets directly" % flag)
    if binary == "nmap" and any(
            arg == "--script-args" or arg.startswith("--script-args=")
            for arg in argv):
        raise PolicyError("--script-args is not allowed")
    for path in _output_paths(argv, binary):
        if not _output_path_ok(path):
            raise PolicyError(
                "output paths must be under %s" % OUTPUT_ROOT)
    for arg in argv:
        if any(character in arg for character in SHELL_CHARS):
            raise PolicyError("shell control characters are not allowed")

    if binary == "curl":
        if any(flag in argv for flag in (
                "-d", "--data", "--data-binary", "-F", "--form",
                "-T", "--upload-file")):
            tier = "intrusive"
        for flag in ("-X", "--request"):
            if flag in argv:
                index = argv.index(flag)
                method = argv[index + 1].upper() if index + 1 < len(argv) else ""
                if method not in ("GET", "HEAD", ""):
                    tier = "intrusive"
    if binary == "nmap":
        for index, arg in enumerate(argv):
            if arg == "--script" or arg.startswith("--script="):
                value = (
                    argv[index + 1]
                    if arg == "--script" and index + 1 < len(argv)
                    else arg.split("=", 1)[1]
                )
                if not re.fullmatch(
                        r"[A-Za-z0-9_*-]+(?:,[A-Za-z0-9_*-]+)*", value):
                    raise PolicyError(
                        "only comma-separated built-in NSE names are allowed")
                names = {part.lower() for part in value.split(",")}
                if names & DANGEROUS_NSE:
                    raise PolicyError(
                        "exploitation, brute, dos, and malware NSE scripts "
                        "are not allowed")
                tier = "intrusive"
    return tier


def validate_steps(steps, scope):
    highest = "recon"
    targets = []
    for step in steps:
        argv = step["argv"]
        tier = classify(argv)
        found = target_tokens(argv)
        needs_target = (argv[0] not in LOCAL_SAFE
                        or (argv[0] == "openssl"
                            and len(argv) > 1
                            and argv[1] == "s_client"))
        if not found and needs_target:
            raise PolicyError("%s requires a target" % argv[0])
        for kind, value in found:
            targets.append(_validate_target(kind, value, scope))
        if TIERS[tier] > TIERS[highest]:
            highest = tier
    return highest, sorted(set(targets))


def _read_stream(stream, limit, holder):
    total = 0
    chunks = []
    while True:
        chunk = stream.read(8192)
        if not chunk:
            break
        total += len(chunk)
        if total <= limit:
            chunks.append(chunk)
        else:
            remaining = limit - (total - len(chunk))
            if remaining > 0:
                chunks.append(chunk[:remaining])
    holder["text"] = "".join(chunks)
    holder["truncated"] = total > limit


def _stop_process(process):
    if hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
        except ProcessLookupError:
            return
    process.kill()


def run_step(argv, timeout):
    started = time.monotonic()
    try:
        Path(OUTPUT_ROOT).mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise PolicyError("could not create %s: %s" % (OUTPUT_ROOT, exc)) from exc
    try:
        process = subprocess.Popen(
            argv,
            shell=False,
            cwd=str(OUTPUT_ROOT),
            env=_safe_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise PolicyError("could not start %s: %s" % (argv[0], exc)) from exc

    stdout = {}
    stderr = {}
    readers = [
        threading.Thread(target=_read_stream,
                         args=(process.stdout, MAX_STREAM_BYTES, stdout),
                         daemon=True),
        threading.Thread(target=_read_stream,
                         args=(process.stderr, MAX_STREAM_BYTES, stderr),
                         daemon=True),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _stop_process(process)
        process.wait()
    for reader in readers:
        reader.join(2)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()

    output = stdout.get("text", "")
    errors = stderr.get("text", "")
    return {
        "argv": list(argv),
        "exit_code": None if timed_out else process.returncode,
        "stdout": output,
        "stderr": errors,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "timed_out": timed_out,
        "truncated": bool(stdout.get("truncated") or stderr.get("truncated")),
        "stdout_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
    }


def run_steps(steps, total_timeout):
    deadline = time.monotonic() + total_timeout
    results = []
    total_output = 0
    for step in steps:
        remaining = int(min(step["timeout"],
                            max(1, deadline - time.monotonic())))
        result = run_step(step["argv"], remaining)
        allowed = max(0, MAX_TOTAL_BYTES - total_output)
        if len(result["stdout"]) + len(result["stderr"]) > allowed:
            result["stdout"] = result["stdout"][:allowed]
            result["truncated"] = True
        total_output += len(result["stdout"]) + len(result["stderr"])
        results.append(result)
        if result["timed_out"] or result["exit_code"] not in (0, None):
            break
    return results


def append_audit(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def parse_request(raw):
    try:
        request = json.loads(raw)
    except ValueError as exc:
        raise ProtocolError("request must be valid JSON") from exc
    if not isinstance(request, dict) or request.get("version") != 1:
        raise ProtocolError("request must use protocol version 1")
    return request


def _respond(status, payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    return status


def _ok(**payload):
    return _respond(0, {"ok": True, **payload})


def _denied(reason, status=2):
    return _respond(status, {"ok": False, "denied": True, "reason": reason})


def main(scope_path=None, audit_path=None):
    scope_path = scope_path or DEFAULT_SCOPE
    audit_path = audit_path or DEFAULT_AUDIT
    try:
        request = parse_request(sys.stdin.read())
        scope = load_scope(scope_path)
    except ProtocolError as exc:
        return _denied(str(exc), 3)
    except PolicyError as exc:
        return _denied(str(exc))

    mode = request.get("mode", "execute")
    if mode not in ("scope", "check", "execute"):
        return _denied("unknown mode %r" % mode, 3)
    if mode == "scope":
        append_audit(audit_path, {"event": "scope",
                                  "scope": scope.as_dict()})
        return _ok(scope=scope.as_dict())

    steps = request.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > MAX_STEPS:
        return _denied("steps must contain 1-%d commands" % MAX_STEPS, 3)
    normalized = []
    for step in steps:
        if not isinstance(step, dict):
            return _denied("each step must be an object", 3)
        argv = step.get("argv")
        if (not isinstance(argv, list) or not argv
                or not all(isinstance(arg, str) and arg for arg in argv)):
            return _denied("argv must be a non-empty list of strings", 3)
        timeout = step.get("timeout", 120)
        if (not isinstance(timeout, int) or isinstance(timeout, bool)
                or not 1 <= timeout <= MAX_STEP_TIMEOUT):
            return _denied(
                "step timeout must be 1-%d seconds" % MAX_STEP_TIMEOUT, 3)
        normalized.append({"argv": argv, "timeout": timeout})

    total_timeout = request.get("total_timeout", 300)
    if (not isinstance(total_timeout, int) or isinstance(total_timeout, bool)
            or not 1 <= total_timeout <= MAX_TOTAL_TIMEOUT):
        return _denied(
            "total_timeout must be 1-%d seconds" % MAX_TOTAL_TIMEOUT, 3)
    objective = request.get("objective", "")
    if not isinstance(objective, str) or not objective.strip():
        return _denied("objective is required", 3)

    try:
        risk, targets = validate_steps(normalized, scope)
    except PolicyError as exc:
        return _denied(str(exc))

    if mode == "check":
        append_audit(audit_path, {
            "event": "check",
            "ts": datetime.now(timezone.utc).isoformat(),
            "objective_sha256": hashlib.sha256(
                objective.encode("utf-8")).hexdigest(),
            "risk": risk,
            "targets": targets,
            "argv": [step["argv"] for step in normalized],
        })
        return _ok(risk=risk, targets=targets,
                   steps=[step["argv"] for step in normalized])

    results = run_steps(normalized, total_timeout)
    append_audit(audit_path, {
        "event": "execute",
        "ts": datetime.now(timezone.utc).isoformat(),
        "objective_sha256": hashlib.sha256(
            objective.encode("utf-8")).hexdigest(),
        "approval": str(request.get("approval", "unknown")),
        "risk": risk,
        "targets": targets,
        "steps": normalized,
        "results": [{
            "exit_code": result["exit_code"],
            "duration_ms": result["duration_ms"],
            "timed_out": result["timed_out"],
            "truncated": result["truncated"],
            "stdout_sha256": result["stdout_sha256"],
        } for result in results],
    })
    return _ok(results=results, risk=risk, targets=targets)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest discover -s kali/tests -v`

Expected: all wrapper tests pass on Windows or Kali.

- [ ] **Step 5: Commit**

```bash
git add kali/kestrel_run.py kali/tests/test_kestrel_run.py
git commit -m "feat(kali): add policy-enforced SSH wrapper"
```

#### Hardening amendment (fix round 1)

Review found the brief's mechanism bypassable; the controller ruled that the
spec's default-deny guarantee outranks it. The two code blocks above are
superseded — `kali/kestrel_run.py` on this branch is authoritative. The
hardened policy (commit `fix(kali): fail closed on unvalidated tokens, paths,
and scripts`):

- **R1 — Fail-closed token walk.** Every token is accounted for: a bare token
  must parse as a target or be the value of a known value-taking flag
  (`VALUE_FLAGS`), else refusal — no silent drops. `--flag=value` and bundled
  short flags are expanded and checked; unknown flags stay permitted but
  their values must still pass R2/R3. Tokens with `,`, `*`, or `_` are
  refused; one trailing dot is stripped from an FQDN before matching.
- **R2 — Structural path confinement.** Any path-like value (write flags and
  unknown flags alike) must resolve under `OUTPUT_ROOT` after symlink
  resolution; the root itself is refused. Known read flags
  (ffuf/gobuster/feroxbuster `-w`/`--wordlist`, openssl
  `-in`/`-CAfile`/`-cert`/`-key`/`-untrusted`) may also resolve under
  `/usr/share/wordlists`, `/usr/share/seclists`, `/usr/share/nmap`,
  `/usr/share/whatweb`, `/etc/ssl/certs` — nothing else. The write-flag table
  stays as an extra layer and gains curl `-D`/`--dump-header`,
  `-c`/`--cookie-jar`, `--trace`, `--trace-ascii`, `--stderr` and openssl
  `-sess_out`.
- **R3 — URL rules.** Only `http`/`https` URLs with a hostname are targets.
  Redirect following is denied (curl `-L`/`--location`/`--location-trusted`,
  ffuf `-r`, feroxbuster `-r`/`--redirects`, nuclei `-follow-redirects`,
  whatweb `--follow-redirect`), as are random/list inputs (nmap `-iR`,
  `--excludefile`, `-D` decoys; dig `-f`; masscan `-iL`/`--excludefile`) on
  top of the existing `-iL`/`-l`/`-request`/`-K` denials.
- **R4 — NSE allowlist.** `--script` accepts a comma-separated list where
  every name is a built-in name (no `*`, no path) and is either a category
  (`default`, `safe`, `version`, `discovery`) or on the fixed `NSE_ALLOWLIST`
  (banner, http-title, http-headers, http-server-header, http-robots.txt,
  http-favicon, ssl-cert, ssl-enum-ciphers, ssh-hostkey, ssh2-enum-algos,
  ftp-anon, smb-os-discovery, smb-protocols, dns-service-discovery,
  mysql-info). `DANGEROUS_NSE` stays as a second layer; `--script-args`
  values are path-confined and scope-checked (`newtargets` refused).
- **R5 — No environment configuration.** `KESTREL_SCOPE`/`KESTREL_AUDIT`/
  `KESTREL_OUTPUT` reads are deleted; `DEFAULT_SCOPE`, `DEFAULT_AUDIT`,
  `OUTPUT_ROOT` are constants. Tests patch module attributes and pass
  `main(scope_path=..., audit_path=...)`.
- **R6 — Control characters.** NUL and every C0 control byte are refused in
  any argv token, alongside the existing shell-metacharacter denial.
- **R7 — OUTPUT_ROOT sanity.** Refuse to run if `OUTPUT_ROOT` is a symlink
  or, on POSIX, exists with a different owner or a mode other than `0700`.

Test count: 63 (33 original + 30 hardening tests), green on Windows including
`-W error::ResourceWarning`; the controller probe's 19 bypass cases are all
DENIED and its three controls are unchanged.

#### Hardening amendment (fix round 2)

A scoped re-review found that round 1's rewritten walk still let a flag value
fall through uninspected when it parsed as neither a target nor a path, so
`VALUE_FLAGS` membership alone licensed destination-bearing flags. The
round-1 R1 clause "or be the value of a known value-taking flag" is superseded
by R8 below. Commit `fix(kali): refuse unvalidated flag values and restore
legitimate forms`:

- **R8 — No silently ignored flag value.** Every value ends in exactly one of:
  a validated in-scope target, a confined path, or a non-destination licensed
  by an explicit value class (ports, record types, headers, methods, timeouts,
  script names, aggression levels). A host-shaped value (fullmatch
  `[A-Za-z0-9._:*@+-]+` plus `.`, `:` or `@`) must parse or be refused;
  comma-separated values are checked part by part; userinfo is stripped to the
  host; scheme-only URLs get the http/https rule. `--script-args` values go
  through the same inspection and argument names must be identifiers.
- **R9 — Relay and protocol-control flags denied outright**, and removed from
  `VALUE_FLAGS`: nmap `-b`, `--dns-servers`, `-S`, `--ip-options`; dig `-b`;
  curl `--proto`, `--proto-default`, `--proto-redir`, `--max-redirs`,
  `-Q`/`--quote`, `-P`/`--ftp-port`, `-t`/`--telnet-option`, `-U`/`--proxy-user`;
  whatweb `--max-redirects`. Every earlier denial is kept.
- **R10 — Destination flags always parse as targets** and can never be
  licensed as non-destinations: curl `--url`, whatweb `-u`/`--url`, nuclei
  `-u`, ffuf `-u`, dig `-q`, masscan `--range`. `--range` validates both ends
  as in-scope IPs in one network.
- **R11 — Legitimate forms restored**: dig/host positional record types
  (A, AAAA, MX, TXT, NS, SOA, CNAME, PTR, SRV, ANY, CAA — not AXFR/IXFR), dig
  `+options` (never destinations, attached values still checked), masscan
  `--range`, and nuclei's single-dash long flags (`-output`) matched whole
  before any short-flag expansion.
- **R12 — Hermetic CLI test**: the environment test runs the real entry point
  with temp scope/audit paths, so no test can write to `/etc/kestrel` or
  `/var/log/kestrel` on an installed Kali host.

Test count: 76 (63 + 13 round-2 tests), green on Windows including
`-W error::ResourceWarning`. Round-1 probe: 19/19 bypass cases DENIED, three
controls unchanged. Round-2 probe: 8/8 must-DENY cases DENIED, 11/11 must-ALLOW
cases ALLOWED.

---

### Task 2: Kali install assets and operator setup

**Files:**
- Create: `kali/install_kestrel_run.sh`
- Create: `kali/scope.example.json`
- Create: `kali/logrotate.kestrel`
- Create: `kali/README.md`
- Create: `kali/tests/test_install_assets.py`

**Interfaces:**
- Consumes: `kali/kestrel_run.py`.
- Produces: a repeatable Kali install with `/usr/local/bin/kestrel-run`, `/etc/kestrel/scope.json`, `/var/log/kestrel/audit.jsonl`, and a forced-command `authorized_keys` line.

- [ ] **Step 1: Write the failing asset tests**

Create `kali/tests/test_install_assets.py`:

```python
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class InstallAssetTests(unittest.TestCase):
    def test_assets_exist(self):
        for name in ("kestrel_run.py", "install_kestrel_run.sh",
                     "scope.example.json", "logrotate.kestrel", "README.md"):
            self.assertTrue((ROOT / name).is_file(), name)

    def test_installer_uses_the_expected_paths_and_modes(self):
        text = (ROOT / "install_kestrel_run.sh").read_text(encoding="utf-8")
        for needle in ("/usr/local/bin/kestrel-run", "/etc/kestrel",
                       "/var/log/kestrel", "/tmp/kestrel", "chmod 0600",
                       "/etc/logrotate.d/kestrel", "cap_net_raw",
                       "chown -R root:root /home/kestrel",
                       "/home/kestrel/.bashrc"):
            self.assertIn(needle, text)

    def test_logrotate_targets_the_audit_log(self):
        text = (ROOT / "logrotate.kestrel").read_text(encoding="utf-8")
        self.assertIn("/var/log/kestrel/audit.jsonl", text)
        self.assertIn("rotate 12", text)

    def test_scope_example_is_private_and_versioned(self):
        data = json.loads((ROOT / "scope.example.json").read_text(
            encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertFalse(data["allow_public"])
        self.assertIn("192.168.56.0/24", data["networks"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest discover -s kali/tests -v`

Expected: missing asset failures.

- [ ] **Step 3: Add the installer, scope example, and README**

Create `kali/install_kestrel_run.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install -o root -g root -m 0755 "$HERE/kestrel_run.py" /usr/local/bin/kestrel-run
install -d -o root -g root -m 0755 /etc/kestrel

if [ ! -e /etc/kestrel/scope.json ]; then
  install -o root -g root -m 0644 "$HERE/scope.example.json" /etc/kestrel/scope.json
fi

install -d -o kestrel -g kestrel -m 0700 /var/log/kestrel
touch /var/log/kestrel/audit.jsonl
chown kestrel:kestrel /var/log/kestrel/audit.jsonl
chmod 0600 /var/log/kestrel/audit.jsonl

install -d -o kestrel -g kestrel -m 0700 /tmp/kestrel

# The kestrel account must not rewrite its own SSH state or home; tool
# output is confined to /tmp/kestrel. No user-writable shell startup files,
# because sshd runs the forced command through the account's shell.
rm -f /home/kestrel/.bashrc /home/kestrel/.profile \
  /home/kestrel/.bash_logout
mkdir -p /home/kestrel/.ssh
chown -R root:root /home/kestrel
chmod 0755 /home/kestrel /home/kestrel/.ssh
if [ -f /home/kestrel/.ssh/authorized_keys ]; then
  chmod 0644 /home/kestrel/.ssh/authorized_keys
fi

if command -v masscan >/dev/null 2>&1; then
  setcap cap_net_raw+ep "$(command -v masscan)"
fi

install -o root -g root -m 0644 "$HERE/logrotate.kestrel" \
  /etc/logrotate.d/kestrel

echo "Installed /usr/local/bin/kestrel-run"
echo "Edit /etc/kestrel/scope.json, then install the forced command in"
echo "~kestrel/.ssh/authorized_keys. See kali/README.md."
```

Create `kali/logrotate.kestrel`:

```text
/var/log/kestrel/audit.jsonl {
    weekly
    rotate 12
    compress
    missingok
    notifempty
    create 0600 kestrel kestrel
}
```

Create `kali/scope.example.json`:

```json
{
  "version": 1,
  "expires": "2027-12-31T23:59:59Z",
  "allow_public": false,
  "networks": ["192.168.56.0/24"],
  "domains": ["lab.local", "*.lab.local"]
}
```

Create `kali/README.md`:

````markdown
# Kali lab setup

This is the host-side setup for Kestrel's SSH wrapper. Run it on the Kali VM,
not on Windows.

## 1. Create the account and install the tools

```bash
sudo apt update
sudo apt install -y openssh-server nmap whatweb dnsutils whois curl \
  ffuf gobuster feroxbuster nuclei nikto masscan netcat-traditional \
  exploitdb libcap2-bin
sudo useradd -m -s /bin/bash kestrel
sudo passwd -l kestrel
# Root-owned home with no user-writable shell startup files: sshd runs the
# forced command through this account's shell.
sudo rm -f /home/kestrel/.bashrc /home/kestrel/.profile \
  /home/kestrel/.bash_logout
sudo chown -R root:root /home/kestrel
sudo chmod 0755 /home/kestrel
sudo install -d -o root -g root -m 0755 /home/kestrel/.ssh
```

Masscan needs raw sockets; `install_kestrel_run.sh` grants only
`cap_net_raw`. Nmap runs unprivileged, so phase 1 uses `-sT`/`-sV`; `-sS`
and `-O` need root and are not enabled.

## 2. Install the wrapper

Copy `kali/` to the VM, then:

```bash
sudo bash install_kestrel_run.sh
```

## 3. Add the forced command

On Windows:

```powershell
mkdir "$env:APPDATA\Kestrel\kali"
ssh-keygen -t ed25519 -f "$env:APPDATA\Kestrel\kali\id_ed25519" -N ""
Get-Content "$env:APPDATA\Kestrel\kali\id_ed25519.pub"
```

On Kali, put this single line in `/home/kestrel/.ssh/authorized_keys`
(replace the key text):

```text
command="/usr/local/bin/kestrel-run",no-port-forwarding,no-agent-forwarding,no-pty,no-X11-forwarding ssh-ed25519 AAAA... kestrel@windows
```

```bash
sudo chown root:root /home/kestrel/.ssh/authorized_keys
sudo chmod 0644 /home/kestrel/.ssh/authorized_keys
sudo sshd -t
sudo systemctl restart ssh
```

Root ownership is deliberate: `kestrel` can read the public key but cannot
replace the forced command, even if an allowlisted tool writes a file.
Tools run with `HOME` and the working directory set to `/tmp/kestrel`, and
output paths are restricted to that directory.

## 4. Set the scope

Edit `/etc/kestrel/scope.json`. It is root-owned and Kestrel cannot change it.
The example allows only `192.168.56.0/24` and `*.lab.local` until
`2027-12-31`.

## 5. Pin the host key on Windows

Find the VM's host-only IP (`ip addr` on Kali), then:

```powershell
ssh-keyscan -p 22 192.168.56.20 > "$env:APPDATA\Kestrel\kali\known_hosts"
ssh -i "$env:APPDATA\Kestrel\kali\id_ed25519" -o UserKnownHostsFile="$env:APPDATA\Kestrel\kali\known_hosts" kestrel@192.168.56.20
```

Type this JSON and press Enter:

```json
{"version":1,"mode":"scope"}
```

Expected: `{"ok": true, "scope": {...}}`.

## 6. Kestrel config

Create `%APPDATA%\Kestrel\kali.json`:

```json
{
  "host": "192.168.56.20",
  "port": 22,
  "user": "kestrel",
  "key_path": "%APPDATA%\\Kestrel\\kali\\id_ed25519",
  "known_hosts": "%APPDATA%\\Kestrel\\kali\\known_hosts",
  "connect_timeout": 10
}
```

## 7. Live checks

From Kestrel:

- `kali_scope` returns the scope.
- `kali_task` with `nmap -sV 192.168.56.20` shows the approval dialog.
- Approve and verify the summary.
- Deny a second task and verify nothing runs.
- Try `nmap -sV 8.8.8.8` and `bash -c "id"`; both are refused before execution.
````

- [ ] **Step 4: Run the asset tests**

Run: `python -m unittest discover -s kali/tests -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add kali/install_kestrel_run.sh kali/scope.example.json \
  kali/logrotate.kestrel kali/README.md kali/tests/test_install_assets.py
git commit -m "docs(kali): add wrapper install and scope setup"
```

---

### Task 3: Kestrel SSH client

**Files:**
- Create: `kali_bridge.py`
- Create: `tests/test_kali_bridge.py`

**Interfaces:**
- Consumes: `mode: "scope"`, `mode: "check"`, and `mode: "execute"` wrapper responses.
- Produces: `KaliConfig`, `load_config(path=None) -> KaliConfig`, `ssh_argv(config) -> list[str]`, `scope(...) -> dict`, `check(objective, steps, total_timeout=300, ...) -> dict`, `task(objective, steps, total_timeout=300, ...) -> dict`; exceptions `KaliUnavailable`, `KaliPolicyError`, `KaliProtocolError`.

- [ ] **Step 1: Write the failing client tests**

Create `tests/test_kali_bridge.py`:

```python
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
    def __init__(self, payload, returncode=0, stderr=""):
        self.payload = payload
        self.returncode = returncode
        self.stderr = stderr
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            self.returncode,
            stdout=json.dumps(self.payload),
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

    def test_rejects_missing_fields(self):
        path = Path(self.temp.name) / "kali.json"
        path.write_text("{}", encoding="utf-8")
        with self.assertRaises(kali_bridge.KaliUnavailable):
            kali_bridge.load_config(path)

    def test_builds_a_strict_ssh_command(self):
        config = kali_bridge.load_config(write_config(self.temp.name))
        argv = kali_bridge.ssh_argv(config)
        self.assertEqual(argv[0], "ssh")
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertIn("IdentitiesOnly=yes", argv)
        self.assertIn("kestrel@192.168.56.20", argv)


class CallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = kali_bridge.load_config(write_config(self.temp.name))

    def test_scope_sends_scope_mode(self):
        runner = FakeRunner({"ok": True, "scope": {"version": 1}})
        result = kali_bridge.scope(config=self.config, runner=runner)
        self.assertEqual(result, {"version": 1})
        request = json.loads(runner.calls[0][1]["input"])
        self.assertEqual(request, {"version": 1, "mode": "scope"})

    def test_check_sends_steps_without_execution(self):
        runner = FakeRunner({"ok": True, "risk": "recon",
                             "targets": ["192.168.56.10"]})
        steps = [{"argv": ["nmap", "-sV", "192.168.56.10"], "timeout": 5}]
        result = kali_bridge.check("scan", steps, config=self.config,
                                   runner=runner)
        self.assertEqual(result["risk"], "recon")
        request = json.loads(runner.calls[0][1]["input"])
        self.assertEqual(request["mode"], "check")
        self.assertEqual(request["steps"], steps)

    def test_task_marks_desktop_approval(self):
        runner = FakeRunner({"ok": True, "results": [], "risk": "recon"})
        kali_bridge.task("scan", [{"argv": ["nmap", "192.168.56.10"]}],
                         config=self.config, runner=runner)
        request = json.loads(runner.calls[0][1]["input"])
        self.assertEqual(request["mode"], "execute")
        self.assertEqual(request["approval"], "desktop")

    def test_policy_denial_is_distinct(self):
        runner = FakeRunner({"ok": False, "denied": True,
                             "reason": "target 8.8.8.8 is outside scope"},
                            returncode=2)
        with self.assertRaises(kali_bridge.KaliPolicyError) as caught:
            kali_bridge.check("bad", [{"argv": ["nmap", "8.8.8.8"]}],
                              config=self.config, runner=runner)
        self.assertIn("outside scope", str(caught.exception))

    def test_invalid_json_is_a_protocol_error(self):
        runner = FakeRunner({})
        runner.payload = None
        with self.assertRaises(kali_bridge.KaliProtocolError):
            kali_bridge.scope(config=self.config, runner=runner)

    def test_missing_ssh_is_unavailable(self):
        def missing(*args, **kwargs):
            raise FileNotFoundError("ssh")
        with self.assertRaises(kali_bridge.KaliUnavailable):
            kali_bridge.scope(config=self.config, runner=missing)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_kali_bridge -v`

Expected: `ModuleNotFoundError: No module named 'kali_bridge'`.

- [ ] **Step 3: Write the client**

Create `kali_bridge.py`:

```python
"""Client for the Kali SSH policy wrapper."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

CONFIG_ENV = "KESTREL_KALI_CONFIG"
MAX_STEPS = 8


class KaliError(RuntimeError):
    """Base error for the Kali bridge."""


class KaliUnavailable(KaliError):
    """The VM, SSH client, or config is unavailable."""


class KaliPolicyError(KaliError):
    """The wrapper refused the task on policy grounds."""


class KaliProtocolError(KaliError):
    """The wrapper returned an unexpected response."""


@dataclass(frozen=True)
class KaliConfig:
    host: str
    port: int
    user: str
    key_path: str
    known_hosts: str
    connect_timeout: int = 10


def config_path(path=None):
    if path:
        return Path(path)
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override)
    return Path(os.environ.get("APPDATA", "")) / "Kestrel" / "kali.json"


def load_config(path=None):
    file_path = config_path(path)
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise KaliUnavailable(
            "Kali config is missing or invalid: %s" % file_path) from exc
    if not isinstance(data, dict):
        raise KaliUnavailable("Kali config must be an object")

    host = data.get("host")
    user = data.get("user")
    key_path = data.get("key_path")
    known_hosts = data.get("known_hosts")
    port = data.get("port", 22)
    connect_timeout = data.get("connect_timeout", 10)
    if not all(isinstance(value, str) and value
               for value in (host, user, key_path, known_hosts)):
        raise KaliUnavailable("Kali config is missing host, user, key_path, "
                              "or known_hosts")
    if (not isinstance(port, int) or isinstance(port, bool)
            or not 1 <= port <= 65535):
        raise KaliUnavailable("Kali config port is invalid")
    if (not isinstance(connect_timeout, int) or isinstance(connect_timeout, bool)
            or not 1 <= connect_timeout <= 120):
        raise KaliUnavailable("Kali config connect_timeout is invalid")
    return KaliConfig(
        host=host,
        port=port,
        user=user,
        key_path=os.path.expandvars(key_path),
        known_hosts=os.path.expandvars(known_hosts),
        connect_timeout=connect_timeout,
    )


def ssh_argv(config):
    return [
        "ssh",
        "-i", config.key_path,
        "-p", str(config.port),
        "-T",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "UserKnownHostsFile=" + config.known_hosts,
        "-o", "ConnectTimeout=%d" % config.connect_timeout,
        "%s@%s" % (config.user, config.host),
    ]


def _run(request, config, timeout, runner=None):
    runner = runner or subprocess.run
    argv = ssh_argv(config)
    try:
        completed = runner(
            argv,
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise KaliUnavailable("OpenSSH client was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise KaliUnavailable("Kali SSH call timed out") from exc
    except OSError as exc:
        raise KaliUnavailable("Kali SSH call failed: %s" % exc) from exc

    stdout = completed.stdout or ""
    if completed.returncode != 0 and not stdout.strip():
        detail = (completed.stderr or "").strip()[:300]
        raise KaliUnavailable(
            "Kali SSH exited %d: %s" % (completed.returncode, detail))
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError) as exc:
        raise KaliProtocolError("Kali returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise KaliProtocolError("Kali returned a non-object response")
    if payload.get("ok") is not True:
        reason = str(payload.get("reason") or "Kali refused the request")
        if payload.get("denied"):
            raise KaliPolicyError(reason)
        raise KaliProtocolError(reason)
    return payload


def scope(timeout=15, config=None, path=None, runner=None):
    config = config or load_config(path)
    return _run({"version": 1, "mode": "scope"}, config, timeout, runner)["scope"]


def check(objective, steps, total_timeout=300, config=None, path=None,
          runner=None):
    config = config or load_config(path)
    request = {
        "version": 1,
        "mode": "check",
        "objective": objective,
        "steps": steps,
        "total_timeout": total_timeout,
    }
    return _run(request, config, config.connect_timeout + 30, runner)


def task(objective, steps, total_timeout=300, config=None, path=None,
         runner=None):
    config = config or load_config(path)
    request = {
        "version": 1,
        "mode": "execute",
        "objective": objective,
        "steps": steps,
        "total_timeout": total_timeout,
        "approval": "desktop",
    }
    timeout = max(60, total_timeout + config.connect_timeout + 30)
    return _run(request, config, timeout, runner)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_kali_bridge -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add kali_bridge.py tests/test_kali_bridge.py
git commit -m "feat: add Kali SSH bridge client"
```

---

### Task 4: Approval bus and Qt relay

**Files:**
- Create: `kali_bus.py`
- Create: `tests/test_kali_bus.py`
- Create: `tests/test_kali_gui_relay.py`
- Modify: `agent_gui_qt.py` (import, `_KaliRelay`, ChatView registration, dialog slot)

**Interfaces:**
- Consumes: a plan dict `{objective, risk, targets, steps}`.
- Produces: `kali_bus.request_approval(plan, timeout=120) -> bool`, `kali_bus.format_approval(plan) -> str`, `_KaliRelay.request(plan, timeout) -> bool`.

- [ ] **Step 1: Write the failing bus and relay tests**

Create `tests/test_kali_bus.py`:

```python
import threading
import unittest

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
```

Create `tests/test_kali_gui_relay.py`:

```python
import threading
import unittest

from PySide6.QtCore import QObject, Slot
from PySide6.QtWidgets import QApplication

from agent_gui_qt import _KaliRelay


class _Receiver(QObject):
    """Slot-owning receiver: a plain lambda would not prove the thread hop."""

    def __init__(self, approved):
        super().__init__()
        self.approved = approved
        self.threads = []

    @Slot(object, object)
    def on_request(self, plan, finish):
        self.threads.append(threading.current_thread())
        finish(self.approved)


class KaliRelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

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

    def test_the_request_is_answered_on_the_gui_thread(self):
        receiver, result = self._ask(True)
        self.assertEqual(result, [True])
        self.assertEqual(receiver.threads, [threading.main_thread()])

    def test_a_queued_slot_can_deny(self):
        receiver, result = self._ask(False)
        self.assertEqual(result, [False])
        self.assertEqual(receiver.threads, [threading.main_thread()])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_kali_bus tests.test_kali_gui_relay -v`

Expected: missing module or missing `_KaliRelay`.

- [ ] **Step 3: Implement the bus**

Create `kali_bus.py`:

```python
"""Thread-safe approval hand-off for Kali tasks."""
from __future__ import annotations

import shlex
import threading

_lock = threading.Lock()
_listener = None


def register(listener):
    global _listener
    with _lock:
        _listener = listener


def unregister(listener=None):
    global _listener
    with _lock:
        if listener is None or _listener is listener:
            _listener = None


def is_attached():
    with _lock:
        return _listener is not None


def request_approval(plan, timeout=120):
    with _lock:
        listener = _listener
    if listener is None:
        return False
    try:
        return bool(listener(dict(plan), timeout))
    except Exception:
        return False


def format_approval(plan):
    lines = [
        "Kali lab task",
        str(plan.get("objective", "")),
        "Risk: %s" % plan.get("risk", "unknown"),
        "Targets: %s" % (", ".join(plan.get("targets", [])) or "none"),
        "",
        "Commands:",
    ]
    for index, step in enumerate(plan.get("steps", []), 1):
        argv = step.get("argv", []) if isinstance(step, dict) else []
        lines.append("%d. %s" % (index, shlex.join(str(arg) for arg in argv)))
    return "\n".join(lines)
```

- [ ] **Step 4: Add the Qt relay and dialog**

In `agent_gui_qt.py`, add `import kali_bus` next to `import sprite_bus` (around line 110).

Add after `_SpriteRelay` (around line 2322):

```python
class _KaliRelay(QObject):
    """Marshals a tool-thread approval request onto the GUI thread."""

    approval_requested = Signal(object, object)

    def request(self, plan, timeout):
        event = threading.Event()
        result = {"approved": False}

        def finish(approved):
            result["approved"] = bool(approved)
            event.set()

        self.approval_requested.emit(dict(plan), finish)
        return event.wait(timeout) and result["approved"]
```

In `ChatView.__init__`, immediately after the sprite relay registration
(around line 2386), add:

```python
        self._kali_relay = _KaliRelay(self)
        self._kali_relay.approval_requested.connect(self._show_kali_approval)
        kali_request = self._kali_relay.request
        kali_bus.register(kali_request)
        self.destroyed.connect(lambda *_: kali_bus.unregister(kali_request))
```

Add this method to `ChatView` near the other dialog helpers:

```python
    def _show_kali_approval(self, plan, finish):
        try:
            reply = QMessageBox.question(
                self,
                "Kali lab approval",
                kali_bus.format_approval(plan),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            finish(reply == QMessageBox.StandardButton.Yes)
        except Exception:
            finish(False)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m unittest tests.test_kali_bus tests.test_kali_gui_relay -v`

Expected: PASS. If Qt is unavailable in the shell, run the relay test through the
project's normal Qt test environment; the bus tests must pass anywhere.

- [ ] **Step 6: Commit**

```bash
git add kali_bus.py tests/test_kali_bus.py tests/test_kali_gui_relay.py \
  agent_gui_qt.py
git commit -m "feat: add desktop approval relay for Kali tasks"
```

---

### Task 5: Kestrel Kali tools and registration

**Files:**
- Modify: `agent.py` (imports, `kali_scope`, `kali_task`, `_format_kali_result`, registry specs, `VALID_TOOLS`, `OBJECT_INPUT_TOOLS`)
- Modify: `tests/test_tool_registry.py` (core tool set)
- Create: `tests/test_kali_tools.py`

**Interfaces:**
- Consumes: `kali_bridge.check/task/scope`, `kali_bus.request_approval`.
- Produces: `kali_task(inp) -> str`, `kali_scope(inp="") -> str`, registry entries `kali_task` and `kali_scope`.

- [ ] **Step 1: Write the failing tool tests**

Create `tests/test_kali_tools.py`:

```python
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent
import kali_bridge
import kali_bus
import native_tools
from tool_registry import InputValidationError


class KaliTaskTests(unittest.TestCase):
    def setUp(self):
        self.steps = [{"argv": ["nmap", "-sV", "192.168.56.10"],
                       "timeout": 10}]
        self.input = {"objective": "scan", "steps": self.steps,
                      "total_timeout": 30}
        self.check = {
            "ok": True,
            "risk": "recon",
            "targets": ["192.168.56.10"],
            "steps": [["nmap", "-sV", "192.168.56.10"]],
        }
        self.result = {
            "ok": True,
            "risk": "recon",
            "targets": ["192.168.56.10"],
            "results": [{
                "argv": ["nmap", "-sV", "192.168.56.10"],
                "exit_code": 0,
                "stdout": "80/tcp open http",
                "stderr": "",
                "timed_out": False,
                "truncated": False,
            }],
        }

    def test_checks_then_approves_then_executes(self):
        calls = []
        with mock.patch.object(agent.kali_bridge, "check",
                               side_effect=lambda *a, **k: calls.append("check")
                               or self.check), \
             mock.patch.object(agent.kali_bus, "request_approval",
                               side_effect=lambda *a, **k: calls.append("approve")
                               or True), \
             mock.patch.object(agent.kali_bridge, "task",
                               side_effect=lambda *a, **k: calls.append("task")
                               or self.result):
            text = agent.kali_task(self.input)

        self.assertEqual(calls, ["check", "approve", "task"])
        self.assertIn("80/tcp open http", text)

    def test_a_denied_dialog_never_executes(self):
        with mock.patch.object(agent.kali_bridge, "check",
                               return_value=self.check), \
             mock.patch.object(agent.kali_bus, "request_approval",
                               return_value=False), \
             mock.patch.object(agent.kali_bridge, "task") as task:
            text = agent.kali_task(self.input)

        task.assert_not_called()
        self.assertIn("not approved", text)

    def test_a_policy_denial_never_opens_the_dialog(self):
        with mock.patch.object(
                agent.kali_bridge, "check",
                side_effect=kali_bridge.KaliPolicyError("outside scope")), \
             mock.patch.object(agent.kali_bus, "request_approval") as approve:
            text = agent.kali_task(self.input)

        approve.assert_not_called()
        self.assertIn("outside scope", text)

    def test_bad_steps_are_rejected_before_ssh(self):
        for value in (

            {"objective": "scan", "steps": []},
            {"objective": "", "steps": self.steps},
            {"objective": "scan", "steps": [{"argv": "nmap"}]},
            {"objective": "scan", "steps": [{"argv": []}]},
            {"objective": "scan", "steps": [{"argv": ["nmap"],
                                             "timeout": 0}]},
        ):
            with self.subTest(value=value):
                with mock.patch.object(agent.kali_bridge, "check") as check:
                    text = agent.kali_task(value)
                check.assert_not_called()
                self.assertTrue(text.startswith("ERROR"))

    def test_a_missing_vm_is_honest(self):
        with mock.patch.object(
                agent.kali_bridge, "check",
                side_effect=kali_bridge.KaliUnavailable("not reachable")):
            text = agent.kali_task(self.input)
        self.assertTrue(text.startswith("ERROR"))
        self.assertIn("not reachable", text)


class KaliScopeTests(unittest.TestCase):
    def test_formats_the_scope(self):
        with mock.patch.object(agent.kali_bridge, "scope",
                               return_value={
                                   "networks": ["192.168.56.0/24"],
                                   "domains": ["*.lab.local"],
                                   "allow_public": False,
                                   "expires": "2027-12-31T23:59:59Z",
                               }):
            text = agent.kali_scope("")
        self.assertIn("192.168.56.0/24", text)
        self.assertIn("*.lab.local", text)
        self.assertIn("public", text)

    def test_a_missing_vm_is_honest(self):
        with mock.patch.object(
                agent.kali_bridge, "scope",
                side_effect=kali_bridge.KaliUnavailable("not reachable")):
            text = agent.kali_scope("")
        self.assertTrue(text.startswith("ERROR"))
        self.assertIn("not reachable", text)


class KaliSchemaTests(unittest.TestCase):
    def test_tools_are_registered(self):
        for name in ("kali_task", "kali_scope"):
            self.assertIn(name, agent.VALID_TOOLS)
            self.assertIsNotNone(agent.TOOL_REGISTRY.get(name))
        self.assertIn("kali_task", agent.OBJECT_INPUT_TOOLS)

    def test_kali_task_schema_requires_argv(self):
        spec = agent.TOOL_REGISTRY.get("kali_task")
        spec.validate({"objective": "scan",
                       "steps": [{"argv": ["nmap", "192.168.56.10"]}]})
        for value in ({}, {"objective": "scan"},
                      {"objective": "scan", "steps": [{}]}):
            with self.subTest(value=value):
                with self.assertRaises(InputValidationError):
                    spec.validate(value)

    def test_native_schema_keeps_the_step_shape(self):
        specs = native_tools.build_specs(["kali_task"],
                                         agent.TOOL_REGISTRY)
        parameters = specs[0]["function"]["parameters"]
        self.assertEqual(parameters["type"], "object")
        self.assertIn("steps", parameters["properties"])
        self.assertEqual(parameters["required"], ["objective", "steps"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_kali_tools -v`

Expected: missing `agent.kali_task`, registry, or prompt entries.

- [ ] **Step 3: Add the tool functions and specs**

In `agent.py`, add imports after `import musicapp_bridge`:

```python
import kali_bridge
import kali_bus
```

Add these functions after `musicapp_control`:

```python
def kali_scope(inp=""):
    """Return the Kali lab's allowed targets and expiry."""
    try:
        data = kali_bridge.scope()
    except kali_bridge.KaliUnavailable as exc:
        return "ERROR: %s" % exc
    except kali_bridge.KaliError as exc:
        return "ERROR: Kali scope failed: %s" % exc
    networks = ", ".join(data.get("networks", [])) or "none"
    domains = ", ".join(data.get("domains", [])) or "none"
    public = "allowed" if data.get("allow_public") else "denied"
    return ("Kali scope: networks %s; domains %s; public %s; expires %s."
            % (networks, domains, public, data.get("expires", "unknown")))


def _format_kali_result(result):
    results = result.get("results") or []
    if not results:
        return "Kali returned no results."
    failed = any(
        item.get("timed_out") or item.get("exit_code") not in (0, None)
        for item in results)
    lines = ["Kali task %s (risk %s)." %
             ("failed" if failed else "finished", result.get("risk", "unknown"))]
    for index, item in enumerate(results, 1):
        if item.get("timed_out"):
            status = "timed out"
        else:
            status = "ok" if item.get("exit_code") == 0 else "exit %s" % (
                item.get("exit_code"))
        output = (item.get("stdout") or "").strip()
        if not output:
            output = (item.get("stderr") or "").strip()
        if output:
            output = output[:1500]
        line = "Step %d [%s] %s" % (
            index, status, " ".join(item.get("argv", [])))
        if output:
            line += ": " + output
        lines.append(line)
    return "\n".join(lines)


def kali_task(inp):
    """Validate, approve, and run one Kali task."""
    data = inp if isinstance(inp, dict) else {}
    objective = str(data.get("objective", "")).strip()
    raw_steps = data.get("steps")
    if not objective:
        return "ERROR: kali_task needs an objective."
    if not isinstance(raw_steps, list) or not raw_steps:
        return "ERROR: kali_task needs at least one step."
    if len(raw_steps) > kali_bridge.MAX_STEPS:
        return ("ERROR: kali_task supports at most %d steps."
                % kali_bridge.MAX_STEPS)

    steps = []
    for raw in raw_steps:
        if not isinstance(raw, dict):
            return "ERROR: each Kali step must be an object."
        argv = raw.get("argv")
        if (not isinstance(argv, list) or not argv
                or not all(isinstance(arg, str) and arg for arg in argv)):
            return "ERROR: each Kali step needs a non-empty argv list."
        timeout = raw.get("timeout", 120)
        if (not isinstance(timeout, int) or isinstance(timeout, bool)
                or not 1 <= timeout <= 600):
            return "ERROR: each Kali step timeout must be 1-600 seconds."
        steps.append({"argv": argv, "timeout": timeout})

    total_timeout = data.get("total_timeout", 300)
    if (not isinstance(total_timeout, int) or isinstance(total_timeout, bool)
            or not 1 <= total_timeout <= 1800):
        return "ERROR: total_timeout must be 1-1800 seconds."

    try:
        check = kali_bridge.check(objective, steps,
                                  total_timeout=total_timeout)
    except kali_bridge.KaliUnavailable as exc:
        return "ERROR: %s" % exc
    except kali_bridge.KaliPolicyError as exc:
        return "ERROR: Kali policy refused the task: %s" % exc
    except kali_bridge.KaliError as exc:
        return "ERROR: Kali check failed: %s" % exc

    plan = {
        "objective": objective,
        "risk": check.get("risk", "unknown"),
        "targets": check.get("targets", []),
        "steps": steps,
    }
    if not kali_bus.request_approval(plan):
        return "Kali task was not approved; nothing ran."

    try:
        result = kali_bridge.task(objective, steps,
                                  total_timeout=total_timeout)
    except kali_bridge.KaliUnavailable as exc:
        return "ERROR: %s" % exc
    except kali_bridge.KaliPolicyError as exc:
        return "ERROR: Kali policy refused the task: %s" % exc
    except kali_bridge.KaliError as exc:
        return "ERROR: Kali task failed: %s" % exc
    return _format_kali_result(result)
```

Add to `VALID_TOOLS`:

```python
    "kali_task", "kali_scope",
```

Add to `OBJECT_INPUT_TOOLS`:

```python
OBJECT_INPUT_TOOLS = frozenset({
    "edit_docx_safe", "sprite_control", "musicapp_control", "kali_task",
})
```

Add the two `ToolSpec` entries before the closing `)` of `_build_tool_registry`:

```python
        ToolSpec(
            name="kali_scope",
            description="Show the Kali lab's allowed targets and expiry.",
            input_schema=_input_string(max_length=0),
            effect_class="read_only",
            timeout=20,
            func=kali_scope,
        ),
        ToolSpec(
            name="kali_task",
            description=(
                "Run an approved security task in the Kali lab. Input is an "
                "object with objective and steps[].steps[].argv."),
            input_schema={
                "type": "object",
                "properties": {
                    "objective": {"type": "string", "minLength": 1},
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "properties": {
                                "argv": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {"type": "string",
                                              "minLength": 1},
                                },
                                "timeout": {"type": "integer",
                                            "minimum": 1,
                                            "maximum": 600},
                            },
                            "required": ["argv"],
                            "additionalProperties": False,
                        },
                    },
                    "total_timeout": {"type": "integer",
                                      "minimum": 1,
                                      "maximum": 1800},
                },
                "required": ["objective", "steps"],
                "additionalProperties": False,
            },
            effect_class="system_control",
            timeout=1800,
            func=kali_task,
        ),
```

Update `tests/test_tool_registry.py`: add `"kali_task"` and `"kali_scope"`
to `CORE_TOOLS`, and rename
`test_the_twenty_core_tools_are_registered` to
`test_the_core_tools_are_registered` (the count is no longer twenty).

- [ ] **Step 4: Run the tool tests**

Run: `python -m unittest tests.test_kali_tools tests.test_tool_registry -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent.py tests/test_kali_tools.py tests/test_tool_registry.py
git commit -m "feat: add Kali task and scope tools"
```

---

### Task 6: Prompt routing, on-demand exposure, and manual

**Files:**
- Modify: `prompt_catalog.py` (`SECURITY_TOOLS`, descriptions, keywords, groups)
- Modify: `prompt_router.py` (`security` rule, intent pattern, `matching_intents`)
- Modify: `agent.py` (`_route_prompt` filters non-security turns; static fallback hides Kali)
- Modify: `tests/test_skills_integration.py` (static catalog count no longer includes on-demand tools)
- Modify: `tests/test_prompt_router.py` (non-security allowlist excludes Kali; security turn includes it)
- Modify: `tests/test_sticky_tools.py` (full-catalog assertions exclude on-demand tools)
- Modify: `tests/test_native_turn_wiring.py` (native array assertion excludes on-demand tools)
- Modify: `skills/core_tools/SKILL.md` (Kali manual)
- Create: `tests/test_kali_routing.py`

**Interfaces:**
- Consumes: `kali_task` and `kali_scope`.
- Produces: security intents expose Kali tools; non-security turns do not; static prompt budget stays under 4,100 tokens.

- [ ] **Step 1: Write the failing routing tests**

Create `tests/test_kali_routing.py`:

```python
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent
import prompt_catalog
import prompt_router


class RoutingTests(unittest.TestCase):
    def setUp(self):
        agent._clear_turn_route()

    def test_security_tools_are_advertised(self):
        for name in ("kali_task", "kali_scope"):
            self.assertIn(name, prompt_catalog.DESCRIPTIONS)
            self.assertIn(name, prompt_catalog.EXTRA_KEYWORDS)

    def test_security_tools_are_in_the_catalog(self):
        tools = prompt_catalog.build_tools(frozenset(agent.VALID_TOOLS))
        by_name = {tool.name: tool for tool in tools}
        self.assertIn("security", by_name["kali_task"].rule_groups)
        self.assertIn("security", by_name["kali_scope"].rule_groups)

    def test_security_intent_matches_kali_terms(self):
        self.assertIn("security",
                      prompt_router.matching_intents("run nmap on the lab"))
        self.assertIn("security",
                      prompt_router.matching_intents("kali scan the target"))
        self.assertNotIn("security",
                         prompt_router.matching_intents("hello there"))

    def test_security_turns_expose_kali_tools(self):
        prompt, selected = agent._route_prompt(
            "run nmap on 192.168.56.10")
        self.assertIn("kali_task", {tool.name for tool in selected})
        self.assertIn("kali_task", prompt)

    def test_normal_turns_hide_kali_tools(self):
        prompt, selected = agent._route_prompt("hello there")
        self.assertNotIn("kali_task", {tool.name for tool in selected})
        self.assertNotIn("kali_task", prompt)

    def test_static_prompt_stays_in_budget_and_hides_kali(self):
        with mock.patch.object(
                agent.memory, "get_user_model_block", return_value=""):
            prompt = agent._build_plan_prompt("hello", "")
        self.assertNotIn("kali_task", prompt)
        self.assertNotIn("kali_task", agent.TOOL_DESCRIPTIONS)
        self.assertLess(len(prompt) / 4, 4100)

    def test_security_prompt_mentions_the_task_tool(self):
        with mock.patch.object(
                agent.memory, "get_user_model_block", return_value=""):
            prompt = agent._build_plan_prompt(
                "run nmap on 192.168.56.10", "")
        self.assertIn("kali_task", prompt)
        self.assertIn("kali_scope", prompt)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_kali_routing -v`

Expected: missing `SECURITY_TOOLS`, `matching_intents`, or tools.

- [ ] **Step 3: Add the catalog and router changes**

In `prompt_catalog.py`, add below `MUSIC_TOOLS`:

```python
SECURITY_TOOLS = frozenset({"kali_task", "kali_scope"})
```

In `DESCRIPTIONS`:

```python
    "kali_task": (
        "Run an approved security task in the Kali lab: one objective and "
        "one or more argv steps."),
    "kali_scope": "Show the Kali lab's allowed targets, expiry, and public setting.",
```

In `EXTRA_KEYWORDS`:

```python
    "kali_task": ("kali", "security", "scan", "recon", "nmap", "pentest",
                  "vulnerability", "lab"),
    "kali_scope": ("kali", "scope", "targets", "lab", "security"),
```

In `build_tools`, add before the `FILE_TOOLS` branch:

```python
        elif name in SECURITY_TOOLS:
            groups = ["security"]
```

In `prompt_router.py`, add to `RULES`:

```python
    "security": (
        "Security lab: use kali_scope to read the allowed targets and "
        "kali_task to run an approved sequence. Load the core_tools manual "
        "first. Never target anything outside the returned scope; never use "
        "a shell string."
    ),
```

Add to `INTENT_PATTERNS`:

```python
    "security": (
        r"\b(?:kali|nmap|masscan|nuclei|ffuf|gobuster|feroxbuster|nikto|"
        r"sqlmap|metasploit|msfconsole|hydra|recon|pentest|"
        r"penetration\s+test|vulnerability\s+scan|security\s+test)\b",
    ),
```

Add this helper near `positive_tool_request`:

```python
def matching_intents(user_request):
    positive = positive_tool_request(user_request)
    return tuple(sorted(
        group for group, patterns in INTENT_PATTERNS.items()
        if any(re.search(pattern, positive, re.I | re.S)
               for pattern in patterns)
    ))
```

In `build_dynamic_prompt`, replace the whole intent block — from
`positive_request = positive_tool_request(user_request)` through the closing
brace of the `intents` set comprehension — with:

```python
    intents = set(matching_intents(user_request))
```

In `agent._route_prompt`, filter non-security turns:

```python
def _route_prompt(user_request):
    tools = prompt_catalog.build_tools(frozenset(VALID_TOOLS))
    if "security" not in prompt_router.matching_intents(user_request):
        tools = [
            tool for tool in tools
            if tool.name not in prompt_catalog.SECURITY_TOOLS
        ]
    routed = prompt_router.build_dynamic_prompt(
        user_request,
        tools,
        count_tokens=_estimate_prompt_tokens,
        classify_intents=_classify_unmatched_intents,
        max_tokens=MAX_PROMPT_TOKENS,
        reserve_tokens=200,
        tool_registry=TOOL_REGISTRY,
    )
    selected = list(routed.tools)
    prompt = routed.system_prompt.replace(
        NATIVE_TOOL_CALL_INSTRUCTION, JSON_TOOL_CALL_INSTRUCTION)
    load_skill = next(tool for tool in prompt_catalog.build_tools(
        frozenset(VALID_TOOLS)) if tool.name == "load_skill")
    if load_skill not in selected:
        selected.append(load_skill)
        prompt += "\n\n- load_skill: " + load_skill.description
    return prompt, tuple(selected)
```

In `agent.py`, also hide Kali tools from the static fallback list:

```python
_PROMPT_TOOL_NAMES = ", ".join(sorted(
    VALID_TOOLS - {"chat", "done"} - prompt_catalog.SECURITY_TOOLS))
```

In `tests/test_skills_integration.py`, change the static-catalog loop in
`test_prompt_index_points_at_the_core_tools_manual` to skip on-demand tools:

```python
        for name in (agent.VALID_TOOLS - {"chat", "done"}
                     - agent.prompt_catalog.SECURITY_TOOLS):
            self.assertIn(name, agent.TOOL_DESCRIPTIONS)
```

In `tests/test_prompt_router.py`, update
`test_turn_allowlist_exposes_every_catalog_tool` and add a security case:

```python
    def test_turn_allowlist_exposes_every_catalog_tool(self):
        self.agent._set_turn_route("watch a movie")
        allowed = self.agent._ROUTED.allowed
        self.assertEqual(allowed,
                         set(self.agent.VALID_TOOLS) - pc.SECURITY_TOOLS)
        self.assertIn("read_file", allowed)
        self.assertIn("search_web", allowed)
        action, error = self.agent.parse_action(
            '{"tool": "read_file", "input": "notes.txt"}')
        self.assertIsNone(error)
        self.assertEqual(action["tool"], "read_file")

    def test_security_turn_allowlist_includes_kali(self):
        self.agent._set_turn_route("run nmap on the lab")
        allowed = self.agent._ROUTED.allowed
        self.assertIn("kali_task", allowed)
        self.assertIn("kali_scope", allowed)
```

In `tests/test_sticky_tools.py`, change both full-catalog assertions in
`test_follow_up_turn_exposes_the_same_full_catalog`:

```python
        self.assertEqual(
            first_allowed[0],
            set(agent.VALID_TOOLS) - agent.prompt_catalog.SECURITY_TOOLS)
        ...
        self.assertEqual(
            second_allowed[0],
            set(agent.VALID_TOOLS) - agent.prompt_catalog.SECURITY_TOOLS)
```

In `tests/test_native_turn_wiring.py`, change
`test_tools_array_exposes_the_full_catalog`:

```python
        self.assertEqual(
            {spec["function"]["name"] for spec in tools},
            agent.VALID_TOOLS - {"chat", "done"}
            - agent.prompt_catalog.SECURITY_TOOLS,
        )
```

In `skills/core_tools/SKILL.md`, add a new `SECURITY LAB` section after
`MUSIC & MEDIA`:

```markdown
SECURITY LAB
  kali_scope() — show the Kali lab's allowed networks, domains, public
    setting, and expiry. Read this before planning a task.
  kali_task(object) — run one approved sequence in Kali. Input:
    {"objective": "Service scan",
     "steps": [{"argv": ["nmap", "-sV", "192.168.56.10"], "timeout": 120}],
     "total_timeout": 300}
    Each step is an argv list; never pass a shell string. The desktop dialog
    is the approval; headless runs are refused. Allowed phase 1:
    nmap, whatweb, dig, host, whois, curl, openssl, searchsploit,
    ffuf, gobuster, feroxbuster, nuclei, nikto, masscan, nc.
    Never target anything outside kali_scope. If the tool says the bridge or
    VM is unavailable, tell the user to start it; do not retry blindly.
```

- [ ] **Step 4: Run the routing, budget, and router tests**

Run:

```bash
python -m unittest tests.test_kali_routing tests.test_prompt_router -v
python -m unittest tests.test_skills_integration tests.test_sticky_tools \
  tests.test_native_turn_wiring -v
python -m unittest tests.test_user_model.PromptInjectionTests.test_prompt_budget_with_a_full_profile -v
```

Expected: all pass, and the static prompt still excludes `kali_task`.

- [ ] **Step 5: Commit**

```bash
git add prompt_catalog.py prompt_router.py agent.py \
  skills/core_tools/SKILL.md tests/test_kali_routing.py \
  tests/test_skills_integration.py tests/test_prompt_router.py \
  tests/test_sticky_tools.py tests/test_native_turn_wiring.py
git commit -m "feat: route Kali security turns and hide them on normal turns"
```

---

### Task 7: End-to-end fake flow and full-suite verification

**Files:**
- Create: `tests/test_kali_flow.py`

**Interfaces:**
- Consumes: `agent.kali_task`, `kali_bridge`, `kali_bus`.
- Produces: proof that check → approval → execute is ordered and that denial/headless never executes.

- [ ] **Step 1: Write the failing flow tests**

Create `tests/test_kali_flow.py`:

```python
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent
import kali_bus


class KaliFlowTests(unittest.TestCase):
    def setUp(self):
        kali_bus.unregister()
        self.steps = [{"argv": ["nmap", "-sV", "192.168.56.10"],
                       "timeout": 10}]
        self.input = {"objective": "scan", "steps": self.steps,
                      "total_timeout": 30}
        self.check = {"ok": True, "risk": "recon",
                      "targets": ["192.168.56.10"], "steps": []}
        self.result = {
            "ok": True, "risk": "recon", "targets": ["192.168.56.10"],
            "results": [{
                "argv": ["nmap", "-sV", "192.168.56.10"],
                "exit_code": 0, "stdout": "22/tcp open ssh",
                "stderr": "", "timed_out": False, "truncated": False,
            }],
        }

    def tearDown(self):
        kali_bus.unregister()

    def test_approval_is_required_before_execute(self):
        with mock.patch.object(agent.kali_bridge, "check",
                               return_value=self.check), \
             mock.patch.object(agent.kali_bus, "request_approval",
                               return_value=True) as approve, \
             mock.patch.object(agent.kali_bridge, "task",
                               return_value=self.result) as task:
            text = agent.kali_task(self.input)

        approve.assert_called_once()
        task.assert_called_once()
        self.assertIn("22/tcp open ssh", text)

    def test_denial_never_executes(self):
        with mock.patch.object(agent.kali_bridge, "check",
                               return_value=self.check), \
             mock.patch.object(agent.kali_bus, "request_approval",
                               return_value=False), \
             mock.patch.object(agent.kali_bridge, "task") as task:
            text = agent.kali_task(self.input)

        task.assert_not_called()
        self.assertIn("not approved", text)

    def test_headless_never_executes(self):
        with mock.patch.object(agent.kali_bridge, "check",
                               return_value=self.check), \
             mock.patch.object(agent.kali_bridge, "task") as task:
            text = agent.kali_task(self.input)

        task.assert_not_called()
        self.assertIn("not approved", text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the flow tests**

Run: `python -m unittest tests.test_kali_flow -v`

Expected: PASS. If they fail, fix the tool/approval ordering before continuing.

- [ ] **Step 3: Run the full Kestrel suite**

Run: `python -m unittest discover -s tests -t tests -p "test_*.py"`

Expected: all tests pass. Investigate any new failure rather than editing the
test to match a bug.

- [ ] **Step 4: Commit**

```bash
git add tests/test_kali_flow.py
git commit -m "test: cover Kali check-approve-execute flow"
```

---

### Task 8: Live Kali VM smoke

**Files:** none. This task provisions the environment and verifies the finished
stack. It is run by the operator because it needs the Kali VM and its SSH key.

- [ ] **Step 1: Provision the VM**

Create the Kali VM using the spec defaults (4 vCPU, 8 GB, 80 GB, host-only
`192.168.56.0/24`, NAT for updates). Take a `clean` snapshot. If a second lab
VM is available, put it on the same host-only network and use it as the scan
target; otherwise the Kali VM's own host-only IP is an authorized in-scope
target.

- [ ] **Step 2: Install SSH and the wrapper**

Follow `kali/README.md` sections 1–4. Expected: `sudo systemctl status ssh`
is active, and `/usr/local/bin/kestrel-run` exists.

- [ ] **Step 3: Pin the host key and verify scope mode**

On Windows:

```powershell
ssh-keyscan -p 22 192.168.56.20 > "$env:APPDATA\Kestrel\kali\known_hosts"
ssh -i "$env:APPDATA\Kestrel\kali\id_ed25519" -o UserKnownHostsFile="$env:APPDATA\Kestrel\kali\known_hosts" kestrel@192.168.56.20
```

Send:

```json
{"version":1,"mode":"scope"}
```

Expected: a JSON object with `"ok": true` and the `192.168.56.0/24` network.

- [ ] **Step 4: Verify the Kestrel client**

From `C:\Users\USER\AgentBot`:

```powershell
python -c "import kali_bridge; print(kali_bridge.scope())"
```

Expected: the same scope dictionary.

- [ ] **Step 5: Verify the approval path**

Start Kestrel, ask: “Run nmap -sV on 192.168.56.20.” The dialog must show the
objective, risk `recon`, target `192.168.56.20`, and the exact command.
Approve it; the tool result must summarize the scan. Deny a second task and
verify no process ran (`ps aux | grep nmap` on Kali is empty).

- [ ] **Step 6: Verify refusals**

Ask Kestrel to scan `8.8.8.8` and to run `bash -c "id"`. Both must be refused
before the dialog or before execution. Check `/var/log/kestrel/audit.jsonl`:
the approved task's check/execute records are present, the two refusals append
nothing, and no record contains raw stdout.

- [ ] **Step 7: Record the result**

Report the VM IP, Kali version, `nmap --version` first line, the approved
command, and the refusal messages. Do not push or commit VM images.

---

## Plan Self-Review

- **Spec coverage:** wrapper, scope, risk tiers, destructive flags, output-file
  confinement, proxy/NSE/template restrictions, check mode, Qt approval,
  headless denial, client, tools, routing, manual, audit, live smoke, and
  phases are covered.
- **Placeholder scan:** no TBD/TODO; every code step contains concrete code or
  exact commands.
- **Type consistency:** `steps` is always `[{argv: list[str], timeout: int}]`;
  `check` returns `risk` and `targets`; `task` returns `results`; `kali_bus`
  receives `{objective, risk, targets, steps}`.
- **Prompt budget:** Kali tools are filtered out of `_route_prompt` unless the
  security intent matches; the static prompt test remains under 4,100 tokens.
- **Known limit (egress):** a tool can follow a redirect or a hardcoded URL to
  an out-of-scope host. Phase 1 trusts the in-scope target; phase 2 should add
  VM-level egress filtering.
- **Known limit (audit):** tools run as the same `kestrel` account, so an
  unknown tool flag could still target a kestrel-writable file such as the
  audit log. Known output flags are restricted; phase 2 should run tools as a
  separate account or move audit to journald.
- **Known limit:** phase 1 is CLI-only and the advanced tiers are deliberately
  absent; that matches the spec.
