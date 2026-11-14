import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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


class TempOutputRoot(unittest.TestCase):
    """Hermetic base: every test confines writes to its own temporary root.

    R5: the wrapper no longer reads KESTREL_* from the environment, so tests
    patch the module attribute instead of relying on /tmp/kestrel.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        saved = kr.OUTPUT_ROOT
        kr.OUTPUT_ROOT = self.root
        self.addCleanup(setattr, kr, "OUTPUT_ROOT", saved)

    def out(self, name):
        return str(self.root / name)


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


class PolicyTests(TempOutputRoot):
    def setUp(self):
        super().setUp()
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

    def test_denies_nul_and_every_c0_control_character(self):
        # R6: Popen must never see an embedded NUL or other C0 byte.
        for control in ("\x00", "\x01", "\x07", "\x08", "\x09", "\x0b",
                        "\x0c", "\x1b", "\x1f"):
            with self.subTest(control=repr(control)):
                with self.assertRaises(kr.PolicyError):
                    self.validate(["nmap", "192.168.56.10",
                                   "8.8.8.8" + control])
        with self.assertRaises(kr.PolicyError):
            self.validate(["curl", "--unknown", "x\x00y",
                           "http://app.lab.local/"])

    def test_returns_the_highest_risk(self):
        risk, _ = kr.validate_steps([
            {"argv": ["nmap", "-sV", "192.168.56.10"], "timeout": 5},
            {"argv": ["ffuf", "-w", "/usr/share/wordlists/dirb/common.txt",
                      "-u", "http://app.lab.local/FUZZ"],
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
            "-u", "http://app.lab.local/FUZZ", "-o", self.out("out.json"),
        ])
        self.assertEqual(risk, "intrusive")
        self.assertEqual(targets, ["app.lab.local"])

    def test_output_paths_must_stay_under_the_output_root(self):
        self.validate(["nmap", "-sV", "-oN", self.out("scan.txt"),
                       "192.168.56.10"])
        self.validate(["whatweb",
                       "--log-brief=" + self.out("whatweb.log"),
                       "http://app.lab.local/"])
        for argv in (
            ["nmap", "-sV", "-oN", "/home/kestrel/.ssh/authorized_keys",
             "192.168.56.10"],
            ["ffuf", "-w", "/usr/share/wordlists/x",
             "-u", "http://app.lab.local/FUZZ",
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

    def test_output_names_may_not_be_an_address(self):
        # `-oN 8.8.8.8` would put an out-of-scope host into the argv while the
        # approved Targets line names only in-scope hosts, so a literal address
        # is refused. A domain-SHAPED name is still a filename: every
        # legitimate report name looks like `a.b`.
        for name in ("8.8.8.8", "fd00::1"):
            with self.subTest(name=name):
                with self.assertRaises(kr.PolicyError):
                    self.validate(["nmap", "-sV", "-oN", name,
                                   "192.168.56.10"])
        self.validate(["nmap", "-sV", "-oN", "report.lab.local.xml",
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
        self.validate(["openssl", "x509", "-in", self.out("cert.pem"),
                       "-noout"])
        with self.assertRaises(kr.PolicyError):
            self.validate(["openssl", "enc", "-in", "/etc/hostname",
                           "-out", self.out("x")])

    def test_nmap_scripts_are_limited_to_builtin_names(self):
        self.validate(["nmap", "-sV", "--script", "safe,http-title",
                       "192.168.56.10"])
        for argv in (
            ["nmap", "-sV", "--script", "exploit", "192.168.56.10"],
            ["nmap", "-sV", "--script", self.out("evil"),
             "192.168.56.10"],
            ["nmap", "-sV", "--datadir", self.temp.name, "192.168.56.10"],
            ["nmap", "-sV", "--script-args-file", self.out("args"),
             "192.168.56.10"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(kr.PolicyError):
                    self.validate(argv)

    def test_custom_nuclei_templates_and_proxies_are_denied(self):
        for argv in (
            ["nuclei", "-u", "http://app.lab.local/", "-t",
             self.out("evil.yaml")],
            ["nuclei", "-u", "http://app.lab.local/", "-code"],
            ["nuclei", "-u", "http://app.lab.local/", "-proxy",
             "http://8.8.8.8:8080"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(kr.PolicyError):
                    self.validate(argv)

    def test_scanner_proxy_flags_are_denied(self):
        for argv in (
            ["ffuf", "-w", "/usr/share/wordlists/x",
             "-u", "http://app.lab.local/FUZZ",
             "-x", "http://8.8.8.8:8080"],
            ["gobuster", "dir", "-u", "http://app.lab.local/", "-w",
             "/usr/share/wordlists/x", "--proxy", "http://8.8.8.8:8080"],
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


class WalkerRegressionTests(TempOutputRoot):
    """The controller's reproduced C1/C3/C4 bypasses, with the probe's argv."""

    def setUp(self):
        super().setUp()
        self.scope = kr.load_scope(write_scope(self.temp.name))

    def validate(self, argv):
        return kr.validate_steps([{"argv": argv, "timeout": 5}], self.scope)

    def denied(self, argv):
        with self.assertRaises(kr.PolicyError):
            self.validate(argv)

    def test_c1_unparseable_target_tokens_are_refused_not_dropped(self):
        for argv in (
            ["nmap", "192.168.56.10", "192.168.56.11,8.8.8.8"],
            ["nmap", "192.168.56.10", "10.0.0.*"],
            ["curl", "http://app.lab.local/", "http://example.com./"],
            ["curl", "http://app.lab.local/",
             "http://evil_host.example.com/"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)

    def test_c1_random_and_list_target_flags_are_refused(self):
        for argv in (
            ["nmap", "192.168.56.10", "-iR", "0"],
            ["dig", "@192.168.56.10", "-f", "/tmp/kestrel/q.txt", "lab.local"],
            ["masscan", "-iL", "/tmp/kestrel/t", "192.168.56.10", "-p80"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)

    def test_c3_unlisted_write_flags_cannot_escape_the_output_root(self):
        for argv in (
            ["curl", "-D", "/home/kestrel/.ssh/authorized_keys",
             "http://app.lab.local/"],
            ["curl", "-c", "/home/kestrel/.ssh/authorized_keys",
             "http://app.lab.local/"],
            ["curl", "--trace", "/home/kestrel/.bashrc",
             "http://app.lab.local/"],
            ["curl", "-so", "/home/kestrel/.ssh/authorized_keys",
             "http://app.lab.local/"],
            ["openssl", "s_client", "-connect", "192.168.56.10:443",
             "-sess_out", "/home/kestrel/.ssh/authorized_keys"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)

    def test_c3_wordlists_may_not_read_arbitrary_local_files(self):
        self.denied(["ffuf", "-w", "/etc/shadow",
                     "-u", "http://app.lab.local/FUZZ"])

    def test_c4_redirects_and_non_http_schemes_are_refused(self):
        for argv in (
            ["curl", "-L", "http://app.lab.local/"],
            ["curl", "file:///etc/passwd", "http://app.lab.local/"],
            ["ffuf", "-r", "-u", "http://app.lab.local/FUZZ",
             "-w", "/tmp/kestrel/w.txt"],
            # Same refusal with a legitimate wordlist, so the denial is
            # provably about -r and not about the wordlist path.
            ["ffuf", "-r", "-u", "http://app.lab.local/FUZZ",
             "-w", "/usr/share/wordlists/dirb/common.txt"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)

    def test_c4_url_valued_flags_cannot_smuggle_a_non_http_scheme(self):
        words = "/usr/share/wordlists/dirb/common.txt"
        self.validate(["curl", "--url", "http://app.lab.local/"])
        for argv in (
            ["curl", "--url", "file:///etc/passwd", "http://app.lab.local/"],
            ["curl", "--url=file:///etc/passwd", "http://app.lab.local/"],
            ["curl", "--url", "gopher://app.lab.local/",
             "http://app.lab.local/"],
            ["curl", "--url", "http:///etc/passwd", "http://app.lab.local/"],
            ["ffuf", "-w", words, "-u", "http://app.lab.local/FUZZ",
             "-u", "file:///etc/passwd"],
            ["nuclei", "-u", "http://app.lab.local/",
             "-u", "file:///etc/passwd"],
            ["nmap", "--script", "http-title", "--script-args",
             "http-title.url=file:///etc/passwd", "192.168.56.10"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)

    def test_r1_bundled_short_flags_are_expanded_and_checked(self):
        self.validate(["curl", "-so", self.out("f.txt"),
                       "http://app.lab.local/f.txt"])
        for argv in (
            ["curl", "-sO", "http://app.lab.local/f.txt"],
            ["curl", "-vL", "http://app.lab.local/"],
            ["curl", "-sD", "/home/kestrel/.bashrc", "http://app.lab.local/"],
            ["nmap", "-oN/home/kestrel/.bashrc", "192.168.56.10"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)

    def test_r1_equals_form_values_obey_the_same_rules(self):
        self.validate(["curl", "--output=" + self.out("f.txt"),
                       "http://app.lab.local/"])
        for argv in (
            ["curl", "--trace=/home/kestrel/.bashrc", "http://app.lab.local/"],
            ["curl", "--output=/home/kestrel/.bashrc",
             "http://app.lab.local/"],
            ["nmap", "--exclude=8.8.8.8", "192.168.56.10"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)

    def test_r1_unknown_flags_are_allowed_only_with_confined_values(self):
        self.validate(["curl", "--unknown-write", self.out("x"),
                       "http://app.lab.local/"])
        for argv in (
            ["curl", "--unknown-write", "/home/kestrel/.bashrc",
             "http://app.lab.local/"],
            ["curl", "--unknown-write=/home/kestrel/.bashrc",
             "http://app.lab.local/"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)

    def test_r1_flag_values_are_still_scope_checked(self):
        # R9 moved nmap -S to an outright denial, so the host-valued positive
        # is --exclude; the -S denial below is kept and strengthened.
        self.validate(["nmap", "--exclude", "192.168.56.11", "192.168.56.10"])
        for argv in (
            ["nmap", "-S", "8.8.8.8", "192.168.56.10"],
            ["nmap", "-S", "192.168.56.10", "192.168.56.11"],
            ["nmap", "--exclude", "8.8.8.8", "192.168.56.10"],
            ["dig", "-b", "8.8.8.8", "lab.local"],
            ["curl", "-e", "http://example.com/", "http://app.lab.local/"],
            ["openssl", "s_client", "-connect", "8.8.8.8:443"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)

    def test_r2_read_flags_may_use_the_read_only_dirs(self):
        for argv in (
            ["ffuf", "-w", "/usr/share/wordlists/dirb/common.txt",
             "-u", "http://app.lab.local/FUZZ"],
            ["gobuster", "dir", "-w",
             "/usr/share/seclists/Discovery/Web-Content/common.txt",
             "-u", "http://app.lab.local/"],
            ["feroxbuster", "--wordlist", "/usr/share/wordlists/x",
             "-u", "http://app.lab.local/"],
            ["openssl", "x509", "-in", "/etc/ssl/certs/ca-certificates.crt",
             "-noout"],
            ["ffuf", "-w", self.out("words.txt"),
             "-u", "http://app.lab.local/FUZZ"],
        ):
            with self.subTest(argv=argv):
                self.validate(argv)
        for argv in (
            ["openssl", "x509", "-in", "/etc/shadow", "-noout"],
            ["ffuf", "-w", "~/wordlists/x", "-u", "http://app.lab.local/FUZZ"],
            ["ffuf", "-w", "/usr/share/wordlists/../../etc/shadow",
             "-u", "http://app.lab.local/FUZZ"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)

    def test_r2_a_path_equal_to_the_output_root_is_refused(self):
        for argv in (
            ["curl", "-o", str(self.root), "http://app.lab.local/"],
            ["curl", "-o", ".", "http://app.lab.local/"],
            ["nmap", "-oN", str(self.root), "192.168.56.10"],
            ["ffuf", "-w", "/usr/share/wordlists/dirb/common.txt",
             "-u", "http://app.lab.local/FUZZ", "-od", str(self.root)],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)

    def test_r2_the_read_only_allowlist_is_fixed(self):
        for argv in (
            ["ffuf", "-w", "/usr/share/exploitdb/x",
             "-u", "http://app.lab.local/FUZZ"],
            ["openssl", "x509", "-in", "/usr/share/exploitdb/x", "-noout"],
            ["ffuf", "-w", "/usr/share/nmap/../exploitdb/x",
             "-u", "http://app.lab.local/FUZZ"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)

    def test_r2_symlinks_under_the_root_cannot_escape_it(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "authorized_keys"
            outside.write_text("ssh-rsa AAAA", encoding="utf-8")
            link = self.root / "link"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not available on this platform")
            self.denied(["nmap", "-sV", "-oN", str(link), "192.168.56.10"])

    def test_r3_only_http_and_https_urls_are_targets(self):
        for url in ("file:///etc/passwd", "ftp://app.lab.local/x",
                    "gopher://app.lab.local/", "dict://app.lab.local/",
                    "ldap://app.lab.local/", "http:///etc/passwd"):
            with self.subTest(url=url):
                self.denied(["curl", url, "http://app.lab.local/"])

    def test_r3_redirect_and_target_list_flags_are_denied(self):
        words = "/usr/share/wordlists/dirb/common.txt"
        for argv in (
            ["curl", "--location", "http://app.lab.local/"],
            ["curl", "--location-trusted", "http://app.lab.local/"],
            ["feroxbuster", "-u", "http://app.lab.local/", "-w", words, "-r"],
            ["feroxbuster", "-u", "http://app.lab.local/", "-w", words,
             "--redirects"],
            ["nuclei", "-u", "http://app.lab.local/", "-follow-redirects"],
            ["nuclei", "-u", "http://app.lab.local/", "-fr"],
            ["whatweb", "--follow-redirect=always", "http://app.lab.local/"],
            ["nmap", "192.168.56.10", "--excludefile", "/tmp/kestrel/x"],
            ["nmap", "192.168.56.10", "-D", "192.168.56.11"],
            ["masscan", "192.168.56.10", "-p80", "--excludefile",
             "/tmp/kestrel/x"],
            ["gobuster", "dir", "-u", "http://app.lab.local/", "-w", words,
             "-r"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)

    def test_r1_a_trailing_dot_host_is_normalized_then_scope_checked(self):
        self.validate(["curl", "http://app.lab.local./"])
        self.denied(["curl", "http://example.com./"])
        self.denied(["curl", "http://app.lab.local/", "http://example.com./"])


class FlagValuePolicyTests(TempOutputRoot):
    """R8-R11: no flag value is ignored, relay flags are denied outright, and
    the legitimate lab forms (record types, +options, ranges, -output) work."""

    def setUp(self):
        super().setUp()
        self.scope = kr.load_scope(write_scope(self.temp.name))

    def validate(self, argv):
        return kr.validate_steps([{"argv": argv, "timeout": 5}], self.scope)

    def denied(self, argv):
        with self.assertRaises(kr.PolicyError):
            self.validate(argv)

    def test_r9_relay_and_protocol_control_flags_are_denied(self):
        for argv in (
            ["nmap", "-b", "user@8.8.8.8", "192.168.56.10"],
            ["nmap", "-b", "user:pass@8.8.8.8:21", "192.168.56.10"],
            ["nmap", "--dns-servers", "8.8.8.8,1.1.1.1", "192.168.56.10"],
            ["nmap", "-S", "8.8.8.8", "192.168.56.10"],
            # An in-scope value does not license the flag either.
            ["nmap", "-S", "192.168.56.10", "192.168.56.11"],
            ["nmap", "--ip-options", "L 8.8.8.8", "192.168.56.10"],
            ["nmap", "--ip-options=L 8.8.8.8", "192.168.56.10"],
            ["dig", "-b", "8.8.8.8", "lab.local"],
            ["dig", "-b", "192.168.56.10", "lab.local"],
            ["curl", "--proto-default", "ftp", "http://app.lab.local/"],
            ["curl", "--proto", "=http", "http://app.lab.local/"],
            ["curl", "--proto-redir", "=ftp", "http://app.lab.local/"],
            ["curl", "--max-redirs", "5", "http://app.lab.local/"],
            ["curl", "-Q", "SITE EXEC", "http://app.lab.local/"],
            ["curl", "--quote", "SITE EXEC", "http://app.lab.local/"],
            ["curl", "-P", "-", "http://app.lab.local/"],
            ["curl", "--ftp-port", "-", "http://app.lab.local/"],
            ["curl", "-t", "TTYPE=vt100", "http://app.lab.local/"],
            ["curl", "--telnet-option", "TTYPE=vt100", "http://app.lab.local/"],
            ["curl", "-U", "user:pass", "http://app.lab.local/"],
            ["curl", "--proxy-user", "user:pass", "http://app.lab.local/"],
            ["whatweb", "--max-redirects", "5", "http://app.lab.local/"],
            # The same denials behind '=' and inside a short cluster.
            ["whatweb", "--max-redirects=5", "http://app.lab.local/"],
            ["curl", "-sQ", "SITE EXEC", "http://app.lab.local/"],
            ["nmap", "-vb", "user@8.8.8.8", "192.168.56.10"],
            ["curl", "--proto-default=ftp", "http://app.lab.local/"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)

    def test_r8_host_shaped_values_must_parse_or_be_refused(self):
        for argv in (
            ["curl", "--url", "ftp:evil.com", "http://app.lab.local/"],
            ["curl", "--url", "ftp:app.lab.local", "http://app.lab.local/"],
            ["nmap", "--exclude", "user@8.8.8.8", "192.168.56.10"],
            ["nmap", "--exclude", "user:pass@8.8.8.8:21", "192.168.56.10"],
            ["nmap", "--exclude", "a.b:c:d", "192.168.56.10"],
            ["curl", "--unknown", "user@8.8.8.8", "http://app.lab.local/"],
            ["nmap", "-p", "8.8.8.8", "192.168.56.10"],
            # A port spec licence cannot hide a destination either.
            ["nmap", "-p", "T:8.8.8.8", "192.168.56.10"],
            ["nmap", "-p", "http:8.8.8.8", "192.168.56.10"],
            ["nmap", "-p", "80,8.8.8.8", "192.168.56.10"],
            ["curl", "-m", "8.8.8.8", "http://app.lab.local/"],
            ["curl", "-A", "8.8.8.8", "http://app.lab.local/"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)

    def test_r8_comma_lists_are_validated_part_by_part(self):
        risk, targets = self.validate(
            ["nmap", "--exclude", "192.168.56.11,192.168.56.12",
             "192.168.56.10"])
        self.assertEqual(
            targets, ["192.168.56.10", "192.168.56.11", "192.168.56.12"])
        for argv in (
            ["nmap", "--dns-servers", "8.8.8.8,1.1.1.1", "192.168.56.10"],
            ["nmap", "--exclude", "192.168.56.11,8.8.8.8", "192.168.56.10"],
            ["nmap", "--exclude", "8.8.8.8,192.168.56.11", "192.168.56.10"],
            ["nmap", "--exclude", "192.168.56.11,", "192.168.56.10"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)

    def test_r8_userinfo_is_stripped_to_the_host(self):
        for value in ("user@192.168.56.11", "user:pass@192.168.56.11:21"):
            with self.subTest(value=value):
                _, targets = self.validate(
                    ["nmap", "--exclude", value, "192.168.56.10"])
                self.assertIn("192.168.56.11", targets)

    def test_r8_scheme_only_urls_get_the_http_rule(self):
        self.validate(["curl", "--url", "http:app.lab.local"])
        for scheme in ("ftp", "gopher", "file", "dict", "ldap"):
            with self.subTest(scheme=scheme):
                self.denied(["curl", "--url", scheme + ":app.lab.local",
                             "http://app.lab.local/"])

    def test_r8_licensed_non_destinations_still_work(self):
        words = "/usr/share/wordlists/dirb/common.txt"
        for argv in (
            ["nmap", "-sV", "-T4", "-p", "80,443", "192.168.56.10"],
            ["nmap", "-p", "T:80,U:53", "192.168.56.10"],
            ["nmap", "-p-", "192.168.56.10"],
            ["nmap", "-p", "1-65535", "192.168.56.10"],
            ["nmap", "-p", "80,http,https-alt", "192.168.56.10"],
            ["nmap", "--max-rtt-timeout", "100ms", "192.168.56.10"],
            ["nmap", "--host-timeout", "30s", "192.168.56.10"],
            ["nmap", "-e", "eth0", "192.168.56.10"],
            ["curl", "--limit-rate", "100K", "http://app.lab.local/"],
            ["openssl", "s_client", "-connect", "192.168.56.10:443",
             "-alpn", "h2,http/1.1"],
            ["curl", "-s", "-A", "Mozilla/5.0", "-H", "X-Test: 1",
             "http://app.lab.local/"],
            ["curl", "-X", "POST", "-d", "a=1", "http://app.lab.local/"],
            ["curl", "-m", "10", "--retry", "2", "http://app.lab.local/"],
            ["whatweb", "-a", "3", "http://app.lab.local/"],
            ["dig", "-t", "MX", "-p", "53", "@192.168.56.10", "lab.local"],
            ["nuclei", "-u", "http://app.lab.local/", "-severity",
             "critical,high", "-rl", "50"],
            ["searchsploit", "-t", "openssh"],
            ["ffuf", "-w", words, "-u", "http://app.lab.local/FUZZ",
             "-mc", "200,301"],
            ["masscan", "-p80", "--rate", "100", "192.168.56.10"],
            ["nc", "-zv", "-w", "3", "192.168.56.10", "80"],
        ):
            with self.subTest(argv=argv):
                self.validate(argv)

    def test_r8_unlicensed_values_are_refused_not_ignored(self):
        for argv in (
            ["curl", "--unknown", "plainword", "http://app.lab.local/"],
            ["nmap", "--exclude", "10.0.0.*", "192.168.56.10"],
            ["curl", "-m", "soon", "http://app.lab.local/"],
            ["nmap", "--exclude", "", "192.168.56.10"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)

    def test_r8_script_arg_values_are_never_ignored(self):
        self.validate(["nmap", "--script", "http-title", "--script-args",
                       "http.useragent=kestrel", "192.168.56.10"])
        for argv in (
            ["nmap", "--script", "http-title", "--script-args",
             "http-title.url=ftp:evil.com", "192.168.56.10"],
            ["nmap", "--script", "http-title", "--script-args",
             "userdb=user@8.8.8.8", "192.168.56.10"],
            ["nmap", "--script", "http-title", "--script-args",
             "http-title.url=http://8.8.8.8/", "192.168.56.10"],
            ["nmap", "--script", "http-title", "--script-args",
             "user db=x", "192.168.56.10"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)

    def test_r10_destination_flags_always_parse_as_targets(self):
        words = "/usr/share/wordlists/dirb/common.txt"
        for argv in (
            ["curl", "--url", "http://app.lab.local/"],
            ["whatweb", "-u", "http://app.lab.local/"],
            ["whatweb", "--url", "http://app.lab.local/"],
            ["nuclei", "-u", "http://app.lab.local/"],
            ["ffuf", "-w", words, "-u", "http://app.lab.local/FUZZ"],
            ["dig", "-q", "lab.local", "@192.168.56.10"],
        ):
            with self.subTest(argv=argv):
                self.validate(argv)
        for argv in (
            ["curl", "--url", "ftp:evil.com", "http://app.lab.local/"],
            ["curl", "--url", "http://8.8.8.8/", "http://app.lab.local/"],
            ["curl", "--url", "out.json", "http://app.lab.local/"],
            ["dig", "-q", "MX", "lab.local", "@192.168.56.10"],
            ["whatweb", "-u", "MX", "http://app.lab.local/"],
            ["nuclei", "-u", "critical", "http://app.lab.local/"],
            ["ffuf", "-w", words, "-u", "http://app.lab.local/FUZZ",
             "-u", "ftp:evil.com"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)

    def test_r10_masscan_range_requires_both_ends_in_scope(self):
        _, targets = self.validate(
            ["masscan", "--range", "192.168.56.10-192.168.56.20", "-p80"])
        self.assertEqual(targets, ["192.168.56.10", "192.168.56.20"])
        for value in ("192.168.56.10-8.8.8.8", "8.8.8.8-192.168.56.10",
                      "192.168.56.10-192.168.57.20",
                      "192.168.56.20-192.168.56.10", "192.168.56.10",
                      "192.168.56.10-", "lab.local-192.168.56.10",
                      "192.168.56.10-192.168.56.20-192.168.56.30"):
            with self.subTest(value=value):
                self.denied(["masscan", "--range", value, "-p80"])

    def test_r11_dig_and_host_positional_record_types(self):
        for kind in ("A", "AAAA", "MX", "TXT", "NS", "SOA", "CNAME", "PTR",
                     "SRV", "ANY", "CAA", "mx"):
            with self.subTest(kind=kind):
                self.validate(["dig", "@192.168.56.10", "lab.local", kind])
                self.validate(["host", "lab.local", kind])
        for kind in ("AXFR", "IXFR", "axfr"):
            with self.subTest(kind=kind):
                self.denied(["dig", "@192.168.56.10", "lab.local", kind])
                self.denied(["host", "lab.local", kind])

    def test_r11_dig_plus_options_are_not_destinations(self):
        for argv in (
            ["dig", "+short", "lab.local", "@192.168.56.10"],
            ["dig", "+tcp", "+bufsize=4096", "lab.local", "@192.168.56.10"],
            ["dig", "+subnet=192.168.56.0/24", "lab.local", "@192.168.56.10"],
        ):
            with self.subTest(argv=argv):
                self.validate(argv)
        for argv in (
            ["dig", "+subnet=8.8.8.8", "lab.local", "@192.168.56.10"],
            ["dig", "+short=8.8.8.8", "lab.local", "@192.168.56.10"],
            ["dig", "+../evil", "lab.local", "@192.168.56.10"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)

    def test_r11_nuclei_single_dash_long_flags_are_whole_flags(self):
        _, targets = self.validate(
            ["nuclei", "-output", "out.json", "-u", "http://app.lab.local/"])
        self.assertEqual(targets, ["app.lab.local"])
        self.validate(["nuclei", "-output", self.out("n.json"),
                       "-u", "http://app.lab.local/"])
        self.validate(["nuclei", "-severity", "critical",
                       "-u", "http://app.lab.local/"])
        self.denied(["nuclei", "-output", "/home/kestrel/.bashrc",
                     "-u", "http://app.lab.local/"])


class NsePolicyTests(TempOutputRoot):
    """C2/R4: a category word is not a licence to run any script in it."""

    def setUp(self):
        super().setUp()
        self.scope = kr.load_scope(write_scope(self.temp.name))

    def validate(self, argv):
        return kr.validate_steps([{"argv": argv, "timeout": 5}], self.scope)

    def denied(self, argv):
        with self.assertRaises(kr.PolicyError):
            self.validate(argv)

    def test_c2_category_words_do_not_authorize_real_scripts(self):
        for argv in (
            ["nmap", "--script", "*", "192.168.56.10"],
            ["nmap", "--script", "ssh-brute", "192.168.56.10"],
            ["nmap", "--script", "http-slowloris", "192.168.56.10"],
            ["nmap", "--script=*", "192.168.56.10"],
            ["nmap", "--script", "safe,ssh-brute", "192.168.56.10"],
            ["nmap", "--script", "http-enum", "192.168.56.10"],
            ["nmap", "--script", "smb-brute", "192.168.56.10"],
            ["nmap", "--script", "vuln", "192.168.56.10"],
            ["nmap", "--script", "http-*", "192.168.56.10"],
            ["nmap", "--script", "broadcast-ssh-hostkey", "192.168.56.10"],
            # Not on the ruling's fixed allowlist, however harmless it is.
            ["nmap", "--script", "http-methods", "192.168.56.10"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)

    def test_r4_categories_and_named_scripts_are_allowed(self):
        names = ("default", "safe", "version", "discovery",
                 "banner", "http-title", "http-headers", "http-server-header",
                 "http-robots.txt", "http-favicon", "ssl-cert",
                 "ssl-enum-ciphers", "ssh-hostkey", "ssh2-enum-algos",
                 "ftp-anon", "smb-os-discovery", "smb-protocols",
                 "dns-service-discovery", "mysql-info")
        for name in names:
            with self.subTest(name=name):
                risk, targets = self.validate(
                    ["nmap", "--script", name, "192.168.56.10"])
                self.assertEqual(risk, "intrusive")
                self.assertEqual(targets, ["192.168.56.10"])
        for argv in (
            ["nmap", "--script", "banner,http-title,ssl-cert",
             "192.168.56.10"],
            ["nmap", "--script=banner", "192.168.56.10"],
            ["nmap", "-sV", "--script", "default,safe", "192.168.56.10"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.validate(argv)[0], "intrusive")

    def test_r4_script_names_cannot_carry_paths_or_operators(self):
        for value in (self.out("evil"), "../evil.nse",
                      "/usr/share/nmap/scripts/ssh-brute.nse",
                      "banner and http-title", "+banner", "banner,",
                      "", "http-title.nse"):
            with self.subTest(value=value):
                self.denied(["nmap", "--script", value, "192.168.56.10"])

    def test_r4_script_args_values_are_confined_and_scope_checked(self):
        for argv in (
            ["nmap", "--script", "http-title", "--script-args",
             "http.useragent=kestrel", "192.168.56.10"],
            ["nmap", "--script", "ssh-hostkey", "--script-args",
             "userdb=" + self.out("users.txt") + ",passdb="
             + self.out("pass.txt"), "192.168.56.10"],
            ["nmap", "--script", "http-title",
             "--script-args=http.useragent=kestrel", "192.168.56.10"],
        ):
            with self.subTest(argv=argv):
                self.validate(argv)
        for argv in (
            ["nmap", "--script", "http-title", "--script-args",
             "userdb=/etc/shadow", "192.168.56.10"],
            ["nmap", "--script", "http-title",
             "--script-args=userdb=/etc/shadow", "192.168.56.10"],
            ["nmap", "--script", "http-title", "--script-args",
             "http-title.url=http://8.8.8.8/", "192.168.56.10"],
            ["nmap", "--script", "http-title", "--script-args",
             "userdb=/usr/share/wordlists/x", "192.168.56.10"],
            # newtargets grows the scan set past what the walk validated.
            ["nmap", "--script", "banner", "--script-args", "newtargets",
             "192.168.56.10"],
            ["nmap", "--script", "banner", "--script-args",
             "http.useragent=kestrel,newtargets", "192.168.56.10"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)


class PositiveControlTests(TempOutputRoot):
    """Legitimate invocations must keep working after the walk is rewritten."""

    def setUp(self):
        super().setUp()
        self.scope = kr.load_scope(write_scope(self.temp.name))

    def validate(self, argv):
        return kr.validate_steps([{"argv": argv, "timeout": 5}], self.scope)

    def test_the_briefs_positive_controls(self):
        cases = [
            (["nmap", "-sV", "192.168.56.10"], "recon", ["192.168.56.10"]),
            (["nmap", "--script", "default", "192.168.56.10"],
             "intrusive", ["192.168.56.10"]),
            (["nmap", "--script", "banner,http-title", "192.168.56.10"],
             "intrusive", ["192.168.56.10"]),
            (["curl", "http://app.lab.local/"], "recon", ["app.lab.local"]),
            (["curl", "-so", self.out("f.txt"), "http://app.lab.local/f.txt"],
             "recon", ["app.lab.local"]),
            (["ffuf", "-w", "/usr/share/wordlists/dirb/common.txt",
              "-u", "http://app.lab.local/FUZZ"], "intrusive",
             ["app.lab.local"]),
            (["openssl", "s_client", "-connect", "192.168.56.10:443"],
             "recon", ["192.168.56.10"]),
            (["dig", "@192.168.56.10", "lab.local"], "recon",
             ["192.168.56.10", "lab.local"]),
            (["searchsploit", "openssh"], "recon", []),
        ]
        for argv, risk, targets in cases:
            with self.subTest(argv=argv):
                found_risk, found_targets = self.validate(argv)
                self.assertEqual(found_risk, risk)
                self.assertEqual(found_targets, targets)

    def test_every_allowlisted_binary_has_a_working_invocation(self):
        words = "/usr/share/wordlists/dirb/common.txt"
        for argv in (
            ["nmap", "-sS", "-T4", "-p", "80,443", "-oN", self.out("scan.txt"),
             "192.168.56.10"],
            ["whatweb", "-a", "3", "--log-brief=" + self.out("ww.log"),
             "http://app.lab.local/"],
            ["dig", "-t", "A", "-p", "53", "@192.168.56.10", "lab.local"],
            ["host", "-t", "A", "lab.local"],
            ["whois", "-h", "whois.lab.local", "192.168.56.10"],
            ["curl", "-H", "X-Lab: 1", "-A", "kestrel", "-m", "10",
             "-o", self.out("body"), "http://app.lab.local/"],
            ["openssl", "s_client", "-connect", "192.168.56.10:443",
             "-servername", "app.lab.local", "-brief"],
            ["openssl", "x509", "-in", self.out("cert.pem"), "-noout",
             "-text"],
            ["searchsploit", "-t", "openssh"],
            ["ffuf", "-w", words, "-u", "http://app.lab.local/FUZZ",
             "-mc", "200,301", "-t", "40", "-o", self.out("ffuf.json")],
            ["gobuster", "dir", "-u", "http://app.lab.local/", "-w", words,
             "-t", "10", "-x", "php,html"],
            ["feroxbuster", "-u", "http://app.lab.local/", "-w", words,
             "-t", "10", "--output", self.out("ferox.txt")],
            ["nuclei", "-u", "http://app.lab.local/", "-severity", "critical",
             "-tags", "xss", "-rl", "50", "-o", self.out("nuclei.json")],
            ["nikto", "-h", "192.168.56.10", "-p", "80",
             "-o", self.out("nikto.html")],
            ["masscan", "-p80", "--rate", "100", "-oJ",
             self.out("masscan.json"), "192.168.56.10"],
            ["nc", "-zv", "-w", "3", "192.168.56.10", "80"],
        ):
            with self.subTest(argv=argv):
                self.validate(argv)


class ExecutionTests(TempOutputRoot):
    def test_timeout_kills_the_process(self):
        # Part C: elapsed time alone does not prove the child died. The child
        # beats into a file every 50 ms; after the kill the beat must stop.
        beat = self.root / "beat.txt"
        code = ("import pathlib, time\n"
                "p = pathlib.Path(%r)\n"
                "for i in range(400):\n"
                "    p.write_text(str(i), encoding='utf-8')\n"
                "    time.sleep(0.05)\n" % str(beat))
        started = time.monotonic()
        result = kr.run_step([sys.executable, "-c", code], 1.0)
        self.assertTrue(result["timed_out"])
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertTrue(beat.exists())
        first = beat.read_text(encoding="utf-8")
        time.sleep(0.4)
        self.assertEqual(beat.read_text(encoding="utf-8"), first,
                         "the child is still running after the timeout kill")

    def test_output_is_capped(self):
        result = kr.run_step(
            [sys.executable, "-c", "print('x' * 400000)"], 10)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["stdout"]), kr.MAX_STREAM_BYTES)
        # I6: the cap is a byte cap, so the delivered bytes must fit too.
        self.assertLessEqual(len(result["stdout"].encode("utf-8")),
                             kr.MAX_STREAM_BYTES)

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


class OutputRootTests(unittest.TestCase):
    """R7: the confinement root itself must not be squattable."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        saved = kr.OUTPUT_ROOT
        self.addCleanup(setattr, kr, "OUTPUT_ROOT", saved)

    def use_root(self, path):
        kr.OUTPUT_ROOT = path

    def test_a_private_output_root_is_accepted(self):
        root = self.base / "ok"
        root.mkdir(mode=0o700)
        self.use_root(root)
        result = kr.run_step([sys.executable, "-c", "print('ok')"], 10)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"].strip(), "ok")

    def test_a_symlinked_output_root_is_refused(self):
        real = self.base / "real"
        real.mkdir(mode=0o700)
        for name, target in (("link", real), ("dangling", self.base / "no")):
            link = self.base / name
            try:
                link.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not available on this platform")
            with self.subTest(link=name):
                self.use_root(link)
                with self.assertRaises(kr.PolicyError):
                    kr.run_step([sys.executable, "-c", "print('ok')"], 10)

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits")
    def test_an_output_root_that_is_not_0700_is_refused(self):
        root = self.base / "loose"
        root.mkdir(mode=0o700)
        root.chmod(0o755)
        self.use_root(root)
        with self.assertRaises(kr.PolicyError):
            kr.run_step([sys.executable, "-c", "print('ok')"], 10)

    @unittest.skipUnless(os.name == "posix", "POSIX ownership")
    def test_an_output_root_owned_by_another_uid_is_refused(self):
        root = self.base / "foreign"
        root.mkdir(mode=0o700)
        self.use_root(root)
        foreign = root.stat().st_uid + 1
        with mock.patch.object(kr.os, "getuid", return_value=foreign):
            with self.assertRaises(kr.PolicyError):
                kr.run_step([sys.executable, "-c", "print('ok')"], 10)

    def test_posix_mode_check_is_enforced(self):
        # Exercise the POSIX branch on Windows too: a root that is not 0700
        # must be refused even when the owner matches.
        root = self.base / "groupread"
        root.mkdir(mode=0o700)
        root.chmod(0o755)
        self.use_root(root)
        with mock.patch.object(kr.os, "name", "posix"):
            with mock.patch.object(kr.os, "getuid", create=True,
                                   return_value=root.stat().st_uid):
                with self.assertRaises(kr.PolicyError):
                    kr.run_step([sys.executable, "-c", "print('ok')"], 10)


class CliTests(TempOutputRoot):
    def setUp(self):
        super().setUp()
        self.scope = write_scope(self.temp.name)
        self.audit = Path(self.temp.name) / "audit.jsonl"

    def run_cli(self, payload):
        # R5: main() takes explicit paths; the environment configures nothing.
        captured = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            with contextlib.redirect_stdout(captured):
                status = kr.main(scope_path=self.scope, audit_path=self.audit)
        return subprocess.CompletedProcess(
            args=["kestrel_run.py"], returncode=status,
            stdout=captured.getvalue(), stderr="")

    def test_environment_cannot_redirect_policy_audit_or_output(self):
        # R12: the child runs the real CLI entry point (main with no arguments,
        # so the DEFAULT_SCOPE/DEFAULT_AUDIT code path is exercised) but with
        # those defaults pointed at this test's temp dir. On an installed Kali
        # host it must never append to /var/log/kestrel/audit.jsonl.
        marker_dir = self.root / "env"
        marker_dir.mkdir()
        marker = write_scope(marker_dir, networks=["192.0.2.0/24"])
        default_dir = self.root / "default"
        default_dir.mkdir()
        default_scope = write_scope(default_dir)
        default_audit = default_dir / "audit.jsonl"
        driver = self.root / "cli_driver.py"
        driver.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, %r)\n"
            "import kestrel_run as kr\n"
            "kr.DEFAULT_SCOPE = Path(%r)\n"
            "kr.DEFAULT_AUDIT = Path(%r)\n"
            "raise SystemExit(kr.main())\n"
            % (str(ROOT), str(default_scope), str(default_audit)),
            encoding="utf-8")
        env = dict(
            os.environ,
            KESTREL_SCOPE=str(marker),
            KESTREL_AUDIT=str(self.audit),
            KESTREL_OUTPUT=str(self.root / "envroot"),
        )
        completed = subprocess.run(
            [sys.executable, str(driver)],
            input=json.dumps({"version": 1, "mode": "scope"}),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        # The client-supplied audit destination must never be written.
        self.assertFalse(self.audit.exists())
        self.assertFalse((self.root / "envroot").exists())
        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        # The env-supplied scope must not be the one the policy loaded.
        self.assertNotIn("192.0.2.0/24", payload["scope"]["networks"])
        self.assertIn("192.168.56.0/24", payload["scope"]["networks"])
        # The audit went to the temp default, not to the env-supplied path.
        record = json.loads(default_audit.read_text().splitlines()[0])
        self.assertEqual(record["event"], "scope")

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


def run_cli(scope, audit, payload=None, raw=None):
    """Drive main() with explicit paths and capture BOTH streams.

    stderr is captured so a test can prove that no traceback (including a
    reader-thread traceback) reached it.
    """
    out, err = io.StringIO(), io.StringIO()
    text = raw if raw is not None else json.dumps(payload)
    with mock.patch.object(sys, "stdin", io.StringIO(text)):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = kr.main(scope_path=scope, audit_path=audit)
    return subprocess.CompletedProcess(
        args=["kestrel_run.py"], returncode=status,
        stdout=out.getvalue(), stderr=err.getvalue())


class FileReferencePolicyTests(TempOutputRoot):
    """R13: `@file` is curl's file-reference syntax, not a relative path."""

    def setUp(self):
        super().setUp()
        self.scope = kr.load_scope(write_scope(self.temp.name))

    def validate(self, argv):
        return kr.validate_steps([{"argv": argv, "timeout": 5}], self.scope)

    def denied(self, argv):
        with self.assertRaises(kr.PolicyError):
            self.validate(argv)

    def test_r13_the_probe_argv_is_denied(self):
        for argv in (
            ["curl", "-d", "@/etc/passwd", "http://app.lab.local/"],
            ["curl", "-F", "file=@/etc/shadow", "http://app.lab.local/"],
            ["curl", "--data-binary", "@/home/kestrel/.ssh/id_ed25519",
             "http://app.lab.local/"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)

    def test_r13_every_data_and_form_flag_confines_the_reference(self):
        for flag in ("-d", "--data", "--data-raw", "--data-binary",
                     "--data-ascii", "--data-urlencode", "-F", "--form",
                     "--form-string"):
            for value in ("@/etc/passwd", "file=@/etc/shadow",
                          "file=</etc/shadow", "@/etc/passwd",
                          "@../../etc/passwd"):
                with self.subTest(flag=flag, value=value):
                    self.denied(["curl", flag, value, "http://app.lab.local/"])
                    self.denied(["curl", flag + "=" + value,
                                 "http://app.lab.local/"])

    def test_r13_urlencode_name_at_file_and_attached_short_forms(self):
        for argv in (
            ["curl", "--data-urlencode", "name@/etc/passwd",
             "http://app.lab.local/"],
            ["curl", "-d@/etc/passwd", "http://app.lab.local/"],
            ["curl", "-Ffile=@/etc/shadow", "http://app.lab.local/"],
            ["curl", "--unknown", "@/etc/passwd", "http://app.lab.local/"],
            ["curl", "-o", "@/home/kestrel/.ssh/authorized_keys",
             "http://app.lab.local/"],
            ["curl", "-d", "@http://app.lab.local/x",
             "http://app.lab.local/"],
            ["nmap", "--script", "http-title", "--script-args",
             "userdb=@/etc/shadow", "192.168.56.10"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)

    def test_r13_a_sigil_on_a_non_file_flag_keeps_the_r8_rule(self):
        # R13 licenses file reads only where the sigil is real curl syntax.
        # For every other flag a leading "@" is still R8's business: the value
        # is target-parsed and scope-checked, so no denial is weakened.
        self.denied(["nmap", "--exclude", "@8.8.8.8", "192.168.56.10"])
        self.denied(["openssl", "s_client", "-connect", "@8.8.8.8:443"])
        _, targets = self.validate(
            ["nmap", "--exclude", "@192.168.56.11", "192.168.56.10"])
        self.assertIn("192.168.56.11", targets)

    def test_r13_confined_references_and_plain_data_still_work(self):
        for argv in (
            ["curl", "-d", "@" + self.out("payload.json"),
             "http://app.lab.local/"],
            ["curl", "-d", "@payload.json", "http://app.lab.local/"],
            ["curl", "-F", "file=@" + self.out("upload.bin"),
             "http://app.lab.local/"],
            ["curl", "--data-urlencode", "name@" + self.out("p.txt"),
             "http://app.lab.local/"],
            # curl's urlencode grammar: with a "=" this is name=content and
            # is encoded literally, so it is not a file reference.
            ["curl", "--data-urlencode", "x=y@/etc/passwd",
             "http://app.lab.local/"],
            ["curl", "-d", "name=value", "http://app.lab.local/"],
            ["curl", "-F", "field=plain", "http://app.lab.local/"],
            ["curl", "-d", "name=value", "-X", "POST",
             "http://app.lab.local/"],
        ):
            with self.subTest(argv=argv):
                risk, targets = self.validate(argv)
                self.assertEqual(targets, ["app.lab.local"])
        self.assertEqual(
            self.validate(["curl", "-d", "name=value",
                           "http://app.lab.local/"])[0], "intrusive")


class CredentialNumberPolicyTests(TempOutputRoot):
    """R14: credential pairs and decimal numbers are licensed, IPs are not."""

    def setUp(self):
        super().setUp()
        self.scope = kr.load_scope(write_scope(self.temp.name))
        self.words = "/usr/share/wordlists/dirb/common.txt"

    def validate(self, argv):
        return kr.validate_steps([{"argv": argv, "timeout": 5}], self.scope)

    def denied(self, argv):
        with self.assertRaises(kr.PolicyError):
            self.validate(argv)

    def test_r14_curl_credentials_are_licensed(self):
        for argv in (
            ["curl", "-u", "user:pass", "http://app.lab.local/"],
            ["curl", "--user", "user:pass", "http://app.lab.local/"],
            ["curl", "--user=user:pass", "http://app.lab.local/"],
            ["curl", "-u", "user", "http://app.lab.local/"],
        ):
            with self.subTest(argv=argv):
                _, targets = self.validate(argv)
                self.assertEqual(targets, ["app.lab.local"])

    def test_r14_a_credential_may_not_carry_a_destination(self):
        for value in ("user:pass@8.8.8.8", "user@192.168.56.11",
                      "user:pass@192.168.56.11:21", "http://app.lab.local/",
                      "https://app.lab.local/", "ftp:evil.com",
                      "file:///etc/passwd", "@/etc/passwd", "/etc/passwd",
                      "user:pass/../../etc"):
            with self.subTest(value=value):
                self.denied(["curl", "-u", value, "http://app.lab.local/"])

    def test_r14_numeric_flags_accept_decimals_and_decimal_lists(self):
        for argv in (
            ["curl", "-m", "0.5", "http://app.lab.local/"],
            ["curl", "--max-time", "0.5", "http://app.lab.local/"],
            ["curl", "--connect-timeout", "2.5", "http://app.lab.local/"],
            ["ffuf", "-p", "0.5", "-u", "http://app.lab.local/FUZZ",
             "-w", self.words],
            ["ffuf", "--delay", "0.5", "-u", "http://app.lab.local/FUZZ",
             "-w", self.words],
            ["nmap", "--scan-delay", "0.5", "192.168.56.10"],
            ["nmap", "--max-rtt-timeout", "0.5,1.5", "192.168.56.10"],
            ["whatweb", "--wait", "0.5", "http://app.lab.local/"],
            ["nuclei", "-rl", "0.5", "-u", "http://app.lab.local/"],
            ["masscan", "--rate", "100.5", "192.168.56.10"],
        ):
            with self.subTest(argv=argv):
                self.validate(argv)

    def test_r14_a_number_licence_cannot_hide_a_destination(self):
        for argv in (
            ["curl", "-m", "8.8.8.8", "http://app.lab.local/"],
            ["curl", "-m", "0.5.6", "http://app.lab.local/"],
            ["curl", "-m", "1e3", "http://app.lab.local/"],
            ["curl", "-m", "soon", "http://app.lab.local/"],
            ["ffuf", "-p", "0.5", "-u", "http://8.8.8.8/FUZZ",
             "-w", self.words],
            ["nmap", "--exclude", "0.5", "192.168.56.10"],
            ["curl", "--unknown", "0.5", "http://app.lab.local/"],
            # --spoof-mac has no licence: 0 stays denied, and so does 0.5.
            ["nmap", "--spoof-mac", "0", "192.168.56.10"],
            ["nmap", "--spoof-mac", "0.5", "192.168.56.10"],
            ["nmap", "--spoof-mac", "00:11:22:33:44:55", "192.168.56.10"],
        ):
            with self.subTest(argv=argv):
                self.denied(argv)


class _BrokenPipe:
    """A pipe that fails mid-read, so the reader's error path is exercised."""

    def __init__(self):
        self.calls = 0

    def read(self, size):
        self.calls += 1
        if self.calls == 1:
            return b"partial"
        raise OSError("pipe broke")


class ExecutionRobustnessTests(TempOutputRoot):
    """I5/I6: byte pipes, replacement decoding, a held cap, honest hashes."""

    def cap_total(self, limit):
        saved = kr.MAX_TOTAL_BYTES
        kr.MAX_TOTAL_BYTES = limit
        self.addCleanup(setattr, kr, "MAX_TOTAL_BYTES", saved)

    def assert_hashes(self, result):
        for key in ("stdout", "stderr"):
            self.assertEqual(
                result[key + "_sha256"],
                hashlib.sha256(result[key].encode("utf-8")).hexdigest(),
                "%s hash does not match the delivered text" % key)

    def test_non_utf8_output_is_preserved_and_no_thread_traceback(self):
        child = [sys.executable, "-c",
                 "import os; os.write(1, b'\\xff\\xfe\\x80binary'); "
                 "os.write(2, b'err')"]
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            result = kr.run_step(child, 30)
        self.assertEqual(captured.getvalue(), "")
        self.assertEqual(result["stdout"], "\ufffd\ufffd\ufffdbinary")
        self.assertEqual(result["stderr"], "err")
        self.assertFalse(result["truncated"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["read_errors"], [])
        self.assert_hashes(result)

    def test_reader_records_io_failures_instead_of_raising(self):
        holder = {}
        kr._read_stream(_BrokenPipe(), 1024, holder)
        self.assertEqual(holder["bytes"], b"partial")
        self.assertIn("OSError", holder["error"])
        self.assertFalse(holder["truncated"])

    def test_reader_errors_reach_the_result_dict(self):
        # A real child and a real read; only the I/O failure is injected, so
        # the assertion is on run_step's result, not on a mock's call count.
        real = kr._read_stream

        def failing(stream, limit, holder):
            real(stream, limit, holder)
            holder["error"] = "OSError: injected pipe failure"

        with mock.patch.object(kr, "_read_stream", failing):
            result = kr.run_step([sys.executable, "-c", "print('ok')"], 30)
        self.assertEqual(result["stdout"].strip(), "ok")
        self.assertEqual(result["read_errors"],
                         ["stdout: OSError: injected pipe failure",
                          "stderr: OSError: injected pipe failure"])

    def test_total_cap_holds_across_both_streams_and_hashes_match(self):
        self.cap_total(20)
        child = [sys.executable, "-c",
                 "import os; os.write(1, b'A' * 100); os.write(2, b'B' * 100)"]
        result = kr.run_steps([{"argv": child, "timeout": 30}], 60)[0]
        out = result["stdout"].encode("utf-8")
        err = result["stderr"].encode("utf-8")
        self.assertLessEqual(len(out) + len(err), 20)
        self.assertTrue(result["truncated"])
        self.assert_hashes(result)

    def test_each_stream_is_capped(self):
        saved = kr.MAX_STREAM_BYTES
        kr.MAX_STREAM_BYTES = 64
        self.addCleanup(setattr, kr, "MAX_STREAM_BYTES", saved)
        child = [sys.executable, "-c",
                 "import os; os.write(1, b'A' * 500); os.write(2, b'B' * 500)"]
        result = kr.run_step(child, 30)
        self.assertLessEqual(len(result["stdout"].encode("utf-8")), 64)
        self.assertLessEqual(len(result["stderr"].encode("utf-8")), 64)
        self.assertTrue(result["truncated"])
        self.assert_hashes(result)

    def test_the_budget_is_shared_across_steps(self):
        self.cap_total(30)
        steps = [{"argv": [sys.executable, "-c",
                           "import os; os.write(1, b'%s' * 20)" % letter],
                  "timeout": 30} for letter in ("A", "B")]
        results = kr.run_steps(steps, 60)
        self.assertEqual(len(results), 2)
        total = sum(len(r["stdout"].encode("utf-8"))
                    + len(r["stderr"].encode("utf-8")) for r in results)
        self.assertLessEqual(total, 30)
        self.assertEqual(len(results[0]["stdout"]), 20)
        self.assertEqual(len(results[1]["stdout"]), 10)
        self.assertTrue(results[1]["truncated"])
        for result in results:
            self.assert_hashes(result)

    def test_byte_accounting_counts_bytes_not_characters(self):
        self.cap_total(10)
        child = [sys.executable, "-c",
                 "import os; os.write(1, b'\\xc3\\xa9' * 20)"]
        result = kr.run_steps([{"argv": child, "timeout": 30}], 60)[0]
        data = result["stdout"].encode("utf-8")
        self.assertLessEqual(len(data), 10)
        self.assertTrue(result["truncated"])
        # The cut lands on a character boundary: no replacement character.
        self.assertNotIn("\ufffd", result["stdout"])
        self.assertEqual(result["stdout"], data.decode("utf-8"))
        self.assert_hashes(result)

    def test_a_normal_run_hashes_the_delivered_text(self):
        result = kr.run_step(
            [sys.executable, "-c", "import os; os.write(1, b'hi'); "
             "os.write(2, b'warn')"], 30)
        self.assertEqual(result["stdout"], "hi")
        self.assertEqual(result["stderr"], "warn")
        self.assertFalse(result["truncated"])
        self.assert_hashes(result)


class DeadlineTests(TempOutputRoot):
    """I7: the total deadline is a deadline, not a per-step 1s grant."""

    def test_the_total_deadline_stops_the_sequence(self):
        # The first child outlives the 0.5s budget but finishes inside the 1s
        # grant the old code handed out, so the second step used to run too.
        marker = self.root / "second.txt"
        first = [sys.executable, "-c", "import time; time.sleep(0.6)"]
        second = [sys.executable, "-c",
                  "import pathlib; pathlib.Path(%r).write_text('ran')"
                  % str(marker)]
        started = time.monotonic()
        results = kr.run_steps([{"argv": first, "timeout": 30},
                                {"argv": second, "timeout": 30}], 0.5)
        elapsed = time.monotonic() - started
        self.assertEqual(len(results), 1,
                         "a step was started after the total deadline")
        self.assertTrue(results[0]["timed_out"])
        self.assertFalse(marker.exists(), "the second step ran")
        self.assertLess(elapsed, 3.0)

    def test_a_step_keeps_its_own_shorter_timeout(self):
        started = time.monotonic()
        results = kr.run_steps([
            {"argv": [sys.executable, "-c", "import time; time.sleep(10)"],
             "timeout": 1},
        ], 60)
        self.assertTrue(results[0]["timed_out"])
        self.assertLess(time.monotonic() - started, 3.0)


class ScopeOptionTests(TempOutputRoot):
    """Part C: allow_public and IPv6. Policy-only — nothing is executed and
    no address here is ever contacted (the suite's existing 8.8.8.8 denial
    cases use the same addresses the same way)."""

    def validate(self, argv, scope):
        return kr.validate_steps([{"argv": argv, "timeout": 5}], scope)

    def test_allow_public_true_licenses_only_listed_public_targets(self):
        private = kr.load_scope(write_scope(self.temp.name))
        public = kr.load_scope(write_scope(
            self.temp.name, allow_public=True, networks=["8.8.8.0/24"],
            domains=["example.com"]))
        for argv, target in ((["nmap", "8.8.8.8"], "8.8.8.8"),
                             (["curl", "http://example.com/"], "example.com")):
            with self.subTest(argv=argv):
                with self.assertRaises(kr.PolicyError):
                    self.validate(argv, private)
                risk, targets = self.validate(argv, public)
                self.assertEqual(risk, "recon")
                self.assertEqual(targets, [target])
        # allow_public is not a bypass of the network/domain lists.
        for argv in (["nmap", "1.1.1.1"], ["curl", "http://other.com/"]):
            with self.subTest(argv=argv):
                with self.assertRaises(kr.PolicyError):
                    self.validate(argv, public)

    def test_ipv6_targets_are_validated(self):
        scope = kr.load_scope(write_scope(
            self.temp.name, networks=["192.168.56.0/24", "2001:db8::/64"]))
        for argv, target in (
            (["nmap", "-6", "2001:db8::10"], "2001:db8::10"),
            (["nmap", "2001:db8::/64"], "2001:db8::/64"),
            (["curl", "http://[2001:db8::10]/"], "2001:db8::10"),
            (["openssl", "s_client", "-connect", "[2001:db8::10]:443"],
             "2001:db8::10"),
        ):
            with self.subTest(argv=argv):
                _, targets = self.validate(argv, scope)
                self.assertIn(target, targets)
        for argv in (
            ["nmap", "2001:db8:1::10"],
            ["nmap", "2606:4700::1111"],
            ["curl", "http://[2606:4700::1111]/"],
            ["nmap", "2001:db8::/48"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(kr.PolicyError):
                    self.validate(argv, scope)

    def test_an_ipv6_network_against_an_ipv4_scope_is_denied_not_a_crash(self):
        scope = kr.load_scope(write_scope(self.temp.name))
        for argv in (["nmap", "2001:db8::/64"], ["nmap", "2001:db8::10"]):
            with self.subTest(argv=argv):
                with self.assertRaises(kr.PolicyError):
                    self.validate(argv, scope)


class ProtocolCliTests(TempOutputRoot):
    """Part C: every protocol refusal is JSON with exit 3 and no audit."""

    def setUp(self):
        super().setUp()
        self.scope = write_scope(self.temp.name)
        self.audit = self.root / "audit.jsonl"
        self.step = {"argv": ["nmap", "-sV", "192.168.56.10"], "timeout": 5}
        self.calls = 0

    def cli(self, payload=None, raw=None):
        # A fresh audit path per request, so "nothing was audited" is asserted
        # for this request and not polluted by an earlier subTest.
        self.calls += 1
        self.audit = self.root / ("audit-%d.jsonl" % self.calls)
        return run_cli(self.scope, self.audit, payload, raw)

    def request(self, **overrides):
        payload = {"version": 1, "mode": "check", "objective": "scan",
                   "steps": [self.step], "total_timeout": 10}
        payload.update(overrides)
        return payload

    def refused(self, completed):
        self.assertEqual(completed.returncode, 3)
        self.assertEqual(completed.stderr, "")
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["denied"])
        self.assertFalse(self.audit.exists(), "a refused request was audited")
        return payload

    def test_bad_json_is_refused(self):
        for raw in ("{not json", "", "[]", "null", '"text"'):
            with self.subTest(raw=raw):
                self.refused(self.cli(raw=raw))
        with self.assertRaises(kr.ProtocolError):
            kr.parse_request("{not json")

    def test_version_must_be_the_integer_one(self):
        for version in (2, 0, "1", True, 1.0, None, [1], {"a": 1}):
            with self.subTest(version=version):
                self.refused(self.cli(self.request(version=version)))
                with self.assertRaises(kr.ProtocolError):
                    kr.parse_request(json.dumps(self.request(version=version)))
        missing = self.request()
        del missing["version"]
        self.refused(self.cli(missing))
        with self.assertRaises(kr.ProtocolError):
            kr.parse_request(json.dumps(missing))

    def test_objective_is_required(self):
        for objective in (None, "", "   ", 42, ["scan"]):
            with self.subTest(objective=objective):
                payload = self.request()
                if objective is None:
                    del payload["objective"]
                else:
                    payload["objective"] = objective
                self.refused(self.cli(payload))

    def test_step_count_and_shape_are_limited(self):
        many = [self.step] * (kr.MAX_STEPS + 1)
        for steps in ([], many, "nmap", [None],
                      [{"argv": [], "timeout": 5}],
                      [{"argv": ["nmap", 1], "timeout": 5}],
                      [{"argv": "nmap", "timeout": 5}],
                      [{"timeout": 5}]):
            with self.subTest(steps=steps):
                self.refused(self.cli(self.request(steps=steps)))
        # The boundary itself is accepted, so the refusal is not vacuous.
        completed = self.cli(self.request(steps=[self.step] * kr.MAX_STEPS))
        self.assertEqual(completed.returncode, 0)

    def test_step_timeout_must_be_in_range(self):
        for timeout in (0, -1, kr.MAX_STEP_TIMEOUT + 1, True, "5", 1.5):
            with self.subTest(timeout=timeout):
                self.refused(self.cli(self.request(
                    steps=[{"argv": self.step["argv"], "timeout": timeout}])))

    def test_total_timeout_must_be_in_range(self):
        for total in (0, -1, kr.MAX_TOTAL_TIMEOUT + 1, True, "300", 1.5):
            with self.subTest(total=total):
                self.refused(self.cli(self.request(total_timeout=total)))

    def test_unknown_mode_is_refused(self):
        for mode in ("run", "EXECUTE", None, 1):
            with self.subTest(mode=mode):
                self.refused(self.cli(self.request(mode=mode)))

    def test_a_valid_request_is_accepted(self):
        completed = self.cli(self.request())
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, "")
        self.assertTrue(json.loads(completed.stdout)["ok"])


class CliExecuteTests(TempOutputRoot):
    """I7 + Part C: real check/execute runs through main() with a real child.

    The policy only permits fixed binary names, so the fixture copies the
    running interpreter to <temp>/bin/searchsploit(.exe) and points the child
    PATH at it (plus the interpreter's own directory, so Windows finds its
    DLL). `searchsploit` is local-safe and takes free positionals, so
    ["searchsploit", <script>, <marker>, ...] passes the walk and really runs:
    the child itself writes the marker, and a witness file records what the
    audit log already contained when the child started. Nothing is mocked.
    """

    def setUp(self):
        super().setUp()
        self.scope = write_scope(self.temp.name)
        self.audit = self.root / "audit.jsonl"
        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        name = "searchsploit.exe" if os.name == "nt" else "searchsploit"
        fake = self.bindir / name
        shutil.copyfile(sys.executable, fake)
        os.chmod(fake, 0o755)
        pydir = str(Path(sys.executable).resolve().parent)
        saved = kr.SAFE_PATH
        kr.SAFE_PATH = str(self.bindir) + os.pathsep + pydir
        self.addCleanup(setattr, kr, "SAFE_PATH", saved)
        if os.name == "nt":
            # CreateProcess resolves a bare binary name through the PARENT's
            # PATH, not the child env, so the fixture must be visible there.
            patcher = mock.patch.dict(
                os.environ,
                {"PATH": str(self.bindir) + os.pathsep
                 + os.environ.get("PATH", "")})
            patcher.start()
            self.addCleanup(patcher.stop)
        self.script = self.root / "fake_step.py"
        self.script.write_text(
            "import pathlib, sys\n"
            "pathlib.Path(sys.argv[1]).write_text('ran', encoding='utf-8')\n"
            "if len(sys.argv) > 3:\n"
            "    audit = pathlib.Path(sys.argv[2])\n"
            "    seen = audit.read_text(encoding='utf-8') if audit.exists() else ''\n"
            "    pathlib.Path(sys.argv[3]).write_text(seen, encoding='utf-8')\n"
            "print('ok')\n",
            encoding="utf-8")

    def step(self, witness_name=None):
        marker = self.root / "marker.txt"
        argv = ["searchsploit", str(self.script), str(marker)]
        if witness_name:
            argv += [str(self.audit), str(self.root / witness_name)]
        return {"argv": argv, "timeout": 30}, marker

    def request(self, step, mode="execute", **extra):
        payload = {"version": 1, "objective": "lab check", "steps": [step],
                   "total_timeout": 60}
        if mode is not None:
            payload["mode"] = mode
        payload.update(extra)
        return payload

    def events(self):
        return [json.loads(line)["event"]
                for line in self.audit.read_text(encoding="utf-8").splitlines()]

    def test_execute_returns_results_and_writes_intent_then_result(self):
        step, marker = self.step(witness_name="witness.txt")
        completed = run_cli(self.scope, self.audit,
                            self.request(step, approval="yes"))
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, "")
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["risk"], "recon")
        self.assertEqual(payload["targets"], [])
        result = payload["results"][0]
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"].strip(), "ok")
        self.assertFalse(result["timed_out"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["read_errors"], [])
        for key in ("stdout", "stderr"):
            self.assertEqual(
                result[key + "_sha256"],
                hashlib.sha256(result[key].encode("utf-8")).hexdigest())
        self.assertTrue(marker.exists(), "the child did not really run")
        self.assertEqual(self.events(), ["execute_intent", "execute"])
        records = [json.loads(line) for line in
                   self.audit.read_text(encoding="utf-8").splitlines()]
        objective_hash = hashlib.sha256(b"lab check").hexdigest()
        for record in records:
            self.assertIn("ts", record)
            self.assertEqual(record["objective_sha256"], objective_hash)
            self.assertEqual(record["approval"], "yes")
            self.assertEqual(record["risk"], "recon")
        self.assertEqual(records[1]["results"][0]["stdout_sha256"],
                         result["stdout_sha256"])
        self.assertEqual(records[1]["results"][0]["stderr_sha256"],
                         result["stderr_sha256"])
        self.assertNotIn("stdout", records[1]["results"][0])
        # The child saw the intent record on disk before it ran.
        witness = (self.root / "witness.txt").read_text(encoding="utf-8")
        self.assertEqual(json.loads(witness.splitlines()[0])["event"],
                         "execute_intent")

    def test_check_mode_is_side_effect_free(self):
        step, marker = self.step()
        completed = run_cli(self.scope, self.audit,
                            self.request(step, mode="check"))
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, "")
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["risk"], "recon")
        self.assertEqual(payload["steps"], [step["argv"]])
        self.assertNotIn("results", payload)
        self.assertFalse(marker.exists(), "check mode executed the step")
        self.assertEqual(self.events(), ["check"])

    def test_mode_defaults_to_check(self):
        step, marker = self.step()
        completed = run_cli(self.scope, self.audit,
                            self.request(step, mode=None))
        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertNotIn("results", payload)
        self.assertEqual(payload["risk"], "recon")
        self.assertFalse(marker.exists(), "a modeless request executed")
        self.assertEqual(self.events(), ["check"])

    def test_audit_write_failure_blocks_execution(self):
        # I7: the audit parent is a regular file, so the write raises OSError.
        blocker = self.root / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        audit = blocker / "audit.jsonl"
        step, marker = self.step()
        completed = run_cli(self.scope, audit, self.request(step))
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, "")
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertFalse(marker.exists(), "execution ran without an audit")
        self.assertFalse(audit.exists())

    def test_an_unexpected_failure_is_json_not_a_traceback(self):
        step, marker = self.step()
        with mock.patch.object(kr, "run_steps",
                               side_effect=RuntimeError("boom")):
            completed = run_cli(self.scope, self.audit, self.request(step))
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, "")
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("boom", payload["error"])
        self.assertFalse(marker.exists())
        # The intent was recorded; the result record could not be.
        self.assertEqual(self.events(), ["execute_intent"])


class InventoryModeTests(TempOutputRoot):
    """The wrapper can say what it is able to run, without running anything.

    Without this, Kestrel cannot tell "hydra is refused by policy" from "hydra is
    not installed", so it invents either a scan or a refusal. Presence is read
    from SAFE_PATH only, so a request cannot describe its own PATH, and no child
    is ever spawned on this path.
    """

    def setUp(self):
        super().setUp()
        self.scope = write_scope(self.temp.name)
        self.audit = self.root / "audit.jsonl"
        self.bindir = self.root / "bin"
        self.bindir.mkdir()

    def install(self, *names):
        for name in names:
            path = self.bindir / name
            path.write_text("#!/bin/sh\n", encoding="utf-8")
            path.chmod(0o755)

    def cli(self, **overrides):
        payload = {"version": 1, "mode": "inventory"}
        payload.update(overrides)
        with mock.patch.object(kr, "SAFE_PATH",
                               str(self.bindir) + ":/nonexistent-dir"), \
                mock.patch.object(kr, "EGRESS_RULE",
                                  str(self.root / "egress.conf")):
            return run_cli(self.scope, self.audit, payload)

    def inventory(self, **overrides):
        completed = self.cli(**overrides)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"], payload)
        return payload["inventory"]

    def test_reports_present_binaries_under_their_own_tier(self):
        self.install("nmap", "masscan")
        inventory = self.inventory()
        self.assertIn("nmap", inventory["recon"])
        self.assertIn("masscan", inventory["intrusive"])
        self.assertNotIn("whatweb", inventory["recon"])

    def test_local_safe_and_denied_names_are_reported_as_presence(self):
        self.install("searchsploit", "bash")
        inventory = self.inventory()
        self.assertIn("searchsploit", inventory["local_safe"])
        self.assertIn("bash", inventory["denied_present"])
        self.assertNotIn("python3", inventory["denied_present"])

    def test_needs_no_objective_no_steps_and_no_target(self):
        self.assertTrue(self.inventory()["egress_enforced"] in (True, False))

    def test_never_spawns_a_child(self):
        self.install("nmap")
        with mock.patch.object(kr.subprocess, "Popen",
                               side_effect=AssertionError("spawned")), \
                mock.patch.object(kr, "run_steps",
                                  side_effect=AssertionError("ran steps")):
            self.assertIn("nmap", self.inventory()["recon"])

    def test_egress_flag_tracks_the_rule_file(self):
        self.assertFalse(self.inventory()["egress_enforced"])
        (self.root / "egress.conf").write_text("table inet kestrel\n",
                                               encoding="utf-8")
        self.assertTrue(self.inventory()["egress_enforced"])

    def test_writes_only_an_inventory_event(self):
        self.inventory()
        events = [json.loads(line)["event"]
                  for line in self.audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events, ["inventory"])

    def test_an_expired_scope_still_refuses_inventory(self):
        # 2, not 3: an expired scope is a policy denial, the same status every
        # other mode gets for it. Inventory is not a side-effect-free hole in the
        # fail-closed rule, because presence information is still information.
        self.scope = write_scope(self.temp.name,
                                 expires="2000-01-01T00:00:00Z")
        completed = self.cli()
        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["denied"])
        self.assertFalse(self.audit.exists())


class StrictPermitTests(TempOutputRoot):
    """Credential-tier binaries declare every flag they accept; all else is refused.

    The rest of the wrapper is a deny-list: an unknown flag is permitted and only
    its value is constrained. That is affordable for twelve audited binaries and
    not for forty, so a strict binary must name every flag it will accept and
    refuse the rest by name -- including the flags nobody has thought to refuse
    yet, on versions this machine has never seen.
    """

    def setUp(self):
        super().setUp()
        self.scope = kr.load_scope(write_scope(self.temp.name))
        self.wordlist = "/usr/share/wordlists/rockyou.txt"

    def validate(self, argv):
        return kr.validate_steps([{"argv": argv, "timeout": 5}], self.scope)

    def denied(self, argv):
        with self.assertRaises(kr.PolicyError) as caught:
            self.validate(argv)
        return str(caught.exception)

    def test_credential_binaries_are_tiered_credential(self):
        for argv in (
            ["hydra", "-l", "admin", "-P", self.wordlist,
             "192.168.56.10", "ssh"],
            ["sqlmap", "-u", "http://app.lab.local/item.php?id=1", "--dump"],
        ):
            with self.subTest(binary=argv[0]):
                risk, _ = self.validate(argv)
                self.assertEqual(risk, "credential")

    def test_an_undeclared_flag_is_refused_and_named(self):
        for argv, flag in (
            (["hydra", "--restore", "192.168.56.10", "ssh"], "--restore"),
            (["hydra", "-R", self.wordlist, "192.168.56.10", "ssh"], "-R"),
            (["sqlmap", "--wizard",
              "-u", "http://app.lab.local/item.php?id=1"], "--wizard"),
        ):
            with self.subTest(argv=argv[0] + " " + flag):
                reason = self.denied(argv)
                self.assertIn(flag, reason)
                self.assertIn(argv[0], reason)

    def test_declared_flags_still_work(self):
        risk, targets = self.validate(
            ["hydra", "-l", "admin", "-P", self.wordlist, "-t", "16", "-f",
             "192.168.56.10", "ssh"])
        self.assertEqual(risk, "credential")
        self.assertEqual(targets, ["192.168.56.10"])

    def test_target_is_still_scope_checked(self):
        self.denied(["hydra", "-l", "admin", "-P", self.wordlist,
                     "8.8.8.8", "ssh"])
        self.denied(["sqlmap", "-u", "http://example.com/item.php?id=1"])

    def test_wordlist_reads_stay_confined_to_the_packaged_dirs(self):
        self.denied(["hydra", "-l", "admin", "-P", "/etc/shadow",
                     "192.168.56.10", "ssh"])
        self.denied(["sqlmap", "-u", "http://app.lab.local/i.php?id=1",
                     "--password-file", "/etc/shadow"])

    def test_output_writes_stay_confined_to_the_output_root(self):
        risk, _ = self.validate(["hydra", "-l", "admin", "-p", "guess",
                                 "-o", "found.txt", "192.168.56.10", "ssh"])
        self.assertEqual(risk, "credential")
        self.denied(["hydra", "-l", "admin", "-p", "guess",
                     "-o", "/etc/cron.d/x", "192.168.56.10", "ssh"])

    def test_a_target_list_file_is_refused(self):
        self.denied(["hydra", "-l", "admin", "-P", self.wordlist,
                     "-M", "hosts.txt", "ssh"])
        self.denied(["sqlmap", "-m", "192.168.56.0/24"])

    def test_form_service_modules_are_refused_by_name(self):
        # hydra's http-*-form payload needs `&` between fields, and `&` is refused
        # in every argv token by a control-character rule that has survived three
        # review rounds. Relaxing it for one value class is a decision of its own,
        # so the modules are refused and the refusal says why.
        for module in ("http-post-form", "http-get-form"):
            with self.subTest(module=module):
                reason = self.denied(
                    ["hydra", "-l", "admin", "-p", "guess",
                     "192.168.56.10", module, "/login:u=^USER:failed"])
                self.assertIn(module, reason)

    def test_module_loading_flags_are_refused_as_exec(self):
        # Both of these make the tool import Python code from disk: an exec
        # primitive wearing a scanning flag.
        self.denied(["sqlmap", "-u", "http://app.lab.local/i.php?id=1",
                     "--tamper", "space2comment"])
        self.denied(["sqlmap", "-u", "http://app.lab.local/i.php?id=1",
                     "--preprocessor", "x.py"])

    def test_a_search_engine_is_not_a_target_source(self):
        self.denied(["sqlmap", "-g", "inurl:items.php?id="])

    def test_help_and_version_report_needs_no_target(self):
        # Strict tables are built from the installed version's own help text, so
        # reading it has to be reachable. It runs no attack and reveals nothing
        # about the lab, but it stays at the binary's own tier.
        risk, targets = self.validate(["hydra", "-h"])
        self.assertEqual(risk, "credential")
        self.assertEqual(targets, [])
        self.validate(["sqlmap", "--version"])
        self.validate(["sqlmap", "--help"])

    def test_inventory_names_the_credential_tier_and_what_is_strict(self):
        bindir = self.root / "bin"
        bindir.mkdir()
        (bindir / "hydra").write_text("", encoding="utf-8")
        with mock.patch.object(kr, "SAFE_PATH",
                               str(bindir) + ":/nonexistent-dir"),                 mock.patch.object(kr, "EGRESS_RULE", str(self.root / "none")):
            inventory = kr.build_inventory()
        self.assertEqual(inventory["credential"], ["hydra"])
        self.assertEqual(inventory["strict"], sorted(kr.STRICT_BINARIES))

    def test_the_report_licence_does_not_extend_to_deny_list_binaries(self):
        # Staging means a non-strict binary behaves exactly as it did before.
        # `nmap -h` is harmless in itself, but the licence to run without a
        # scoped target is the strict path's own trade, and letting it leak
        # would silently widen every binary in the table below it.
        self.denied(["nmap", "-h"])
        self.denied(["curl", "--help"])
        self.assertEqual(kr.classify(["nmap", "-sV", "192.168.56.10"]), "recon")

    def test_a_report_flag_beside_a_real_scan_is_still_a_scan(self):
        # The report licence is for the whole argv being a report, not for
        # excusing a missing target when one is present.
        self.denied(["hydra", "-h", "-P", self.wordlist, "8.8.8.8", "ssh"])

    def test_non_strict_binaries_keep_the_deny_list_posture(self):
        risk, _ = self.validate(["nmap", "--future-nmap-flag-not-known",
                                 "192.168.56.10"])
        self.assertEqual(risk, "recon")
        self.assertEqual(kr.classify(["curl", "--whatever",
                                      "http://app.lab.local/"]), "recon")


class MasscanStrictMigrationTests(TempOutputRoot):
    """The pilot migration: a binary that already works, on the permit list.

    This is the change that has to be provably inert. masscan is used in the
    live lab today, so a flag missing from its declaration would be a
    regression the operator meets mid-scan rather than a test failure here --
    which is why the flags asserted below are the ones the README and the
    existing suite actually use.
    """

    def setUp(self):
        super().setUp()
        self.scope = kr.load_scope(write_scope(self.temp.name))

    def validate(self, argv):
        return kr.validate_steps([{"argv": argv, "timeout": 5}], self.scope)

    def denied(self, argv):
        with self.assertRaises(kr.PolicyError) as caught:
            self.validate(argv)
        return str(caught.exception)

    def test_masscan_is_declared_strict(self):
        self.assertIn("masscan", kr.STRICT_BINARIES)

    def test_the_flags_the_live_lab_uses_still_pass(self):
        for argv in (
            ["masscan", "-p80", "--rate", "100", "192.168.56.10"],
            ["masscan", "-p", "1-1024", "192.168.56.0/24"],
            ["masscan", "-pT:443", "-oX", "scan.xml", "192.168.56.10"],
            ["masscan", "-p22", "--wait", "2", "--retries", "1",
             "192.168.56.10"],
            ["masscan", "-p80", "--exclude", "192.168.56.1", "192.168.56.0/24"],
            ["masscan", "-p80", "--adapter-ip", "192.168.56.5",
             "192.168.56.10"],
            ["masscan", "-p80", "--banners", "--stats", "192.168.56.10"],
        ):
            with self.subTest(argv=" ".join(argv)):
                self.assertEqual(kr.classify(argv), "intrusive")
                self.validate(argv)

    def test_the_loopback_form_the_readme_documents_still_passes(self):
        # Model B: the VM is reached through a port forward, so the target the
        # operator is told to scan is the VM's own loopback -- which needs
        # 127.0.0.0/8 in scope, exactly as README section 4 says.
        scope = kr.load_scope(write_scope(self.temp.name,
                                          networks=["127.0.0.0/8"]))
        kr.validate_steps([{"argv": ["masscan", "-p80", "--rate", "100",
                                     "127.0.0.1"], "timeout": 5}], scope)

    def test_an_undeclared_masscan_flag_is_refused_by_name(self):
        reason = self.denied(["masscan", "-p80", "--new-release-flag",
                              "192.168.56.10"])
        self.assertIn("--new-release-flag", reason)
        self.assertIn("masscan", reason)

    def test_the_resume_and_target_list_refusals_keep_their_own_messages(self):
        # These were known escapes, so the sharper refusal must survive the
        # permit check running over the same argv.
        for flag in ("--resume", "--excludefile", "-iL"):
            with self.subTest(flag=flag):
                argv = ["masscan", "-p80", flag, "x", "192.168.56.10"]
                reason = self.denied(argv)
                self.assertIn(flag, reason)
                self.assertNotIn("not permitted", reason)

    def test_targets_are_still_the_thing_being_checked(self):
        self.denied(["masscan", "-p80", "8.8.8.8"])
        self.denied(["masscan", "-p80", "--range", "8.8.8.1-8.8.8.9"])
        self.denied(["masscan", "-p80", "192.168.56.0/16"])


class ReconAdditionsTests(TempOutputRoot):
    """ping, traceroute, arp-scan and httpx, declared strictly from birth.

    The tables are deliberately smaller than each tool's real option set. Under
    a permit list an omission is a refusal the operator sees and can ask to have
    opened, whereas a wrong declaration -- a boolean that is really a value, a
    path that is really a target list -- is the hole. The report licence from
    Task 2 is what closes the gap against the installed version's own help.
    """

    TARGETS = {
        "ping": "192.168.56.10",
        "traceroute": "192.168.56.10",
        "arp-scan": "192.168.56.0/24",
        "httpx": "http://app.lab.local/",
    }

    def setUp(self):
        super().setUp()
        self.scope = kr.load_scope(write_scope(self.temp.name))

    def validate(self, argv):
        return kr.validate_steps([{"argv": argv, "timeout": 5}], self.scope)

    def denied(self, argv):
        with self.assertRaises(kr.PolicyError) as caught:
            self.validate(argv)
        return str(caught.exception)

    def test_each_is_permitted_at_recon_tier(self):
        for binary, target in self.TARGETS.items():
            with self.subTest(binary=binary):
                argv = [binary] if binary == "arp-scan" else [binary, target]
                if binary == "arp-scan":
                    argv.append(target)
                self.assertEqual(kr.classify(argv), "recon")

    def test_each_is_strict_from_birth(self):
        for binary in self.TARGETS:
            with self.subTest(binary=binary):
                self.assertIn(binary, kr.STRICT_BINARIES)
                argv = [binary, "--flag-nobody-declared",
                        self.TARGETS[binary]]
                reason = self.denied(argv)
                self.assertIn("--flag-nobody-declared", reason)
                self.assertIn(binary, reason)

    def test_useful_option_sets_pass(self):
        for argv in (
            ["ping", "-c", "4", "192.168.56.10"],
            ["ping", "-4", "-n", "-i", "0.5", "192.168.56.10"],
            ["traceroute", "-m", "16", "-w", "2", "-n", "192.168.56.10"],
            ["traceroute", "-P", "tcp", "-p", "22", "192.168.56.10"],
            ["arp-scan", "192.168.56.0/24"],
            ["arp-scan", "-t", "3", "-r", "1", "192.168.56.10"],
            ["httpx", "-u", "http://app.lab.local/", "-title", "-json"],
        ):
            with self.subTest(argv=" ".join(argv)):
                self.validate(argv)

    def test_evasion_and_flood_primitives_are_refused_by_name(self):
        for argv, flag in (
            (["ping", "-f", "192.168.56.10"], "-f"),
            (["ping", "-l", "65500", "192.168.56.10"], "-l"),
            (["ping", "-s", "192.168.56.1", "192.168.56.10"], "-s"),
            (["traceroute", "-g", "192.168.56.1", "192.168.56.10"], "-g"),
            (["traceroute", "-s", "10.0.0.1", "192.168.56.10"], "-s"),
            (["arp-scan", "--localnet"], "--localnet"),
            (["httpx", "-l", "hosts.txt"], "-l"),
            (["httpx", "-proxy", "http://192.168.56.1:8080",
              "-u", "http://app.lab.local/"], "-proxy"),
            (["httpx", "-listener", "-u", "http://app.lab.local/"],
             "-listener"),
        ):
            with self.subTest(argv=" ".join(argv)):
                reason = self.denied(argv)
                self.assertIn(flag, reason)
                # A named escape is refused as a known escape, not merely as a
                # flag nobody declared: that is the difference between the deny
                # tables still carrying weight and them being dead config.
                self.assertNotIn("not permitted", reason)

    def test_the_segment_wide_probe_needs_an_in_scope_network(self):
        # `--localnet` asks the interface which segment to sweep, so the scope
        # file never sees the answer. An explicit network is refused instead.
        self.denied(["arp-scan", "192.168.1.0/24"])
        self.denied(["arp-scan", "8.8.8.8"])

    def test_every_new_binary_still_needs_an_in_scope_target(self):
        # The whole point of adding these is that they cannot be pointed
        # anywhere but the lab, including through a value flag.
        for argv in (["ping", "8.8.8.8"], ["traceroute", "example.com"],
                     ["arp-scan", "8.8.8.8"],
                     ["httpx", "-u", "http://example.com/"]):
            with self.subTest(argv=" ".join(argv)):
                self.denied(argv)

    def test_httpx_output_stays_confined(self):
        self.validate(["httpx", "-u", "http://app.lab.local/",
                       "-o", "found.txt"])
        self.denied(["httpx", "-u", "http://app.lab.local/",
                     "-o", "/etc/apt/sources.list"])
        self.denied(["httpx", "-u", "http://app.lab.local/",
                     "-timeout", "/etc/shadow"])


class IntrusiveAdditionsTests(TempOutputRoot):
    """dirsearch, wfuzz, wpscan and sslyze at intrusive tier, declared strictly.

    `dirb` is deliberately absent from this arc: its wordlist is a second
    positional, and a positional path has no confined representation in the
    model -- the only mechanism for a non-target positional (FREE_POSITIONALS)
    skips the checks entirely, so declaring dirb would either refuse every real
    command or let it read any path as a dictionary. gobuster and feroxbuster
    already do the same job with `-w` confined to the packaged wordlists.
    """

    def setUp(self):
        super().setUp()
        self.scope = kr.load_scope(write_scope(self.temp.name))
        self.wordlist = "/usr/share/wordlists/dirb/common.txt"

    def validate(self, argv):
        return kr.validate_steps([{"argv": argv, "timeout": 5}], self.scope)

    def denied(self, argv):
        with self.assertRaises(kr.PolicyError) as caught:
            self.validate(argv)
        return str(caught.exception)

    def test_each_is_permitted_at_intrusive_tier(self):
        for argv in (
            ["dirsearch", "-u", "http://app.lab.local/", "-e", "php,html"],
            ["wfuzz", "-c", "-u", "http://app.lab.local/FUZZ",
             "-w", self.wordlist],
            ["wpscan", "--url", "http://app.lab.local/", "--random-useragent"],
            ["sslyze", "--regular", "app.lab.local:443"],
        ):
            with self.subTest(argv=" ".join(argv)):
                self.assertEqual(kr.classify(argv), "intrusive")
                self.validate(argv)

    def test_each_is_strict_from_birth(self):
        for binary in ("dirsearch", "wfuzz", "wpscan", "sslyze"):
            with self.subTest(binary=binary):
                self.assertIn(binary, kr.STRICT_BINARIES)
                reason = self.denied([binary, "--flag-nobody-declared",
                                      "http://app.lab.local/"])
                self.assertIn("--flag-nobody-declared", reason)
                self.assertIn(binary, reason)

    def test_a_wordlist_scanner_is_refused_because_paths_hide_in_its_value(self):
        # `wfuzz -z file,/etc/shadow` reads a path through a value the walker
        # cannot separate from the scanner spec. `-w`, confined, does the job.
        reason = self.denied(["wfuzz", "-u", "http://app.lab.local/FUZZ",
                              "-z", "file,/etc/shadow"])
        # Pinned to the deny-table message on purpose. Refusing `-z` merely
        # because its value does not parse as a target would be enough to block
        # this one input, and would let any other spec-shaped value through.
        self.assertIn("-z is not allowed", reason)

    def test_target_lists_and_relays_are_refused(self):
        for argv, flag in (
            (["dirsearch", "-l", "hosts.txt"], "-l"),
            (["wfuzz", "--pickle", "state.pkl"], "--pickle"),
            (["wfuzz", "-p", "http://192.168.56.1:8080",
              "-u", "http://app.lab.local/FUZZ"], "-p"),
            (["wpscan", "--url", "http://app.lab.local/",
              "--proxy", "http://192.168.56.1:8080"], "--proxy"),
            (["sslyze", "--targets_in", "list.txt", "app.lab.local:443"],
             "--targets_in"),
        ):
            with self.subTest(flag=flag):
                self.assertIn(flag, self.denied(argv))

    def test_output_paths_stay_confined(self):
        self.validate(["wfuzz", "-u", "http://app.lab.local/FUZZ",
                       "-w", self.wordlist, "--output-file", "found.txt"])
        for argv in (
            ["wfuzz", "-u", "http://app.lab.local/FUZZ", "-w", self.wordlist,
             "--output-file", "/etc/cron.d/x"],
            ["wpscan", "--url", "http://app.lab.local/",
             "--output", "/etc/sudoers"],
            ["dirsearch", "-u", "http://app.lab.local/",
             "--wordlist", "/etc/shadow"],
        ):
            with self.subTest(argv=" ".join(argv[1:])):
                self.denied(argv)

    def test_a_word_list_may_not_name_an_absolute_or_escaped_path(self):
        # These flags do not open files, so the value class is documentation of
        # intent rather than the enforcement: what refuses these inputs is the
        # confinement rule that has always applied to a value that looks like a
        # path. An absolute or traversal value here is refused because it names
        # somewhere outside the root, and a relative one (`4/etc`) is permitted
        # because it resolves inside it -- proved by the case below.
        for argv in (
            ["dirsearch", "-u", "http://app.lab.local/", "-e", "/etc/shadow"],
            ["wpscan", "--url", "http://app.lab.local/", "-e", "/etc/passwd"],
            ["wfuzz", "-u", "http://app.lab.local/FUZZ", "--hc", "../etc"],
        ):
            with self.subTest(argv=" ".join(argv[1:])):
                self.denied(argv)
        self.validate(["wfuzz", "-u", "http://app.lab.local/FUZZ",
                       "--hc", "4/etc"])

    def test_destinations_are_still_scope_checked(self):
        for argv in (
            ["dirsearch", "-u", "http://example.com/"],
            ["wfuzz", "-u", "http://example.com/FUZZ", "-w", self.wordlist],
            ["wpscan", "--url", "http://example.com/"],
            ["sslyze", "--regular", "example.com:443"],
        ):
            with self.subTest(argv=" ".join(argv)):
                self.denied(argv)


class CrunchTests(TempOutputRoot):
    """Wordlist generation is bounded arithmetically before the child runs.

    The hazard is not escape, it is disk: `crunch 6 12 -t user%%` emits billions
    of lines, and the 600 s step cap kills the process only after the output has
    filled the VM. So the wrapper computes an upper bound and refuses first.
    Where the bound cannot be worked out at all, the request is refused too --
    guessing the arithmetic is not a licence to run it.
    """

    def setUp(self):
        super().setUp()
        self.scope = kr.load_scope(write_scope(self.temp.name))

    def validate(self, argv):
        return kr.validate_steps([{"argv": argv, "timeout": 5}], self.scope)

    def denied(self, argv):
        with self.assertRaises(kr.PolicyError) as caught:
            self.validate(argv)
        return str(caught.exception)

    def test_crunch_is_permitted_locally_at_recon_tier(self):
        self.assertEqual(kr.classify(["crunch", "2", "2", "digits"]), "recon")

    def test_it_needs_no_target(self):
        risk, targets = self.validate(["crunch", "4", "4", "digits",
                                       "-o", "w.txt"])
        self.assertEqual(risk, "recon")
        self.assertEqual(targets, [])

    def test_a_small_generation_passes(self):
        for argv in (
            ["crunch", "4", "4", "digits"],
            ["crunch", "6", "6", "digits"],
            ["crunch", "1", "3", "lowercase"],
            ["crunch", "8", "8", "-t", "passwo%%"],
        ):
            with self.subTest(argv=" ".join(argv)):
                self.validate(argv)

    def test_a_generation_that_would_fill_the_disk_is_refused_with_numbers(self):
        for argv in (
            ["crunch", "6", "12"],
            ["crunch", "8", "8"],
            ["crunch", "5", "5", "lowercase"],
        ):
            with self.subTest(argv=" ".join(argv)):
                reason = self.denied(argv)
                self.assertIn("crunch", reason)
                self.assertIn(str(kr.MAX_GENERATED_LINES), reason)

    def test_the_template_counted_is_placeholders_not_length(self):
        # A six-character template with two placeholder positions is 95^2, not
        # 95^6 -- refusing this would refuse every realistic targeted list.
        self.validate(["crunch", "6", "6", "-t", "user%%"])
        self.assertIn("crunch", self.denied(
            ["crunch", "8", "8", "-t", "%%%%%%%%"]))

    def test_a_template_length_outside_the_requested_range_is_refused(self):
        # crunch only accepts a template whose length lies within min..max; an
        # argument set it would reject cannot be measured, so it is refused.
        self.assertIn("crunch", self.denied(
            ["crunch", "6", "6", "-t", "user%%secret%%word"]))

    def test_an_alpha_placeholder_is_refused_by_the_host_shape_rule(self):
        # crunch's `@` is its alphanumeric placeholder, and a template that
        # fullmatches the host-shaped pattern with an `@` in it is refused
        # because `text` is deliberately outside STRUCTURED_CLASSES: the loosest
        # licence cannot vouch for something that reads as a hostname. The
        # other three placeholders work, so this costs a spelling, not a tool.
        self.assertIn("looks like a host", self.denied(
            ["crunch", "6", "6", "-t", "user@@"]))
        # Each template is six characters, because crunch requires the
        # template's own length to fall inside the requested range.
        for template in ("user%%", "user__", "user^^", "pa%%_d"):
            with self.subTest(template=template):
                self.validate(["crunch", "6", "6", "-t", template])

    def test_an_unbounded_or_unreadable_request_is_refused(self):
        for argv in (
            ["crunch"],
            ["crunch", "abc", "def"],
            ["crunch", "12", "6"],
        ):
            with self.subTest(argv=" ".join(argv) or "(bare)"):
                self.assertIn("crunch", self.denied(argv))

    def test_output_is_confined_and_file_input_is_refused(self):
        base = ["crunch", "4", "4", "digits"]
        self.validate(base + ["-o", "words.txt"])
        # The confinement message names the path, not the flag: what an
        # operator needs is the location that was refused.
        self.assertIn("/etc/cron.d/x",
                      self.denied(base + ["-o", "/etc/cron.d/x"]))
        self.assertIn("-f is not allowed",
                      self.denied(base + ["-f", "/etc/passwd"]))
        # `-p` emits size! lines and has no arithmetic the wrapper would trust,
        # so it stays undeclared and the refusal names it.
        self.assertIn("-p is not permitted", self.denied(base + ["-p"]))
        self.assertIn("control characters",
                      self.denied(["crunch", "4", "4", "abc;id"]))


class ChildFileLimitTests(unittest.TestCase):
    """The disk guard that does not have to understand any tool's flags."""

    @unittest.skipUnless(__import__("os").name == "posix",
                         "RLIMIT_FSIZE does not exist on the Windows host; the "
                         "child that gets it is only ever started in the VM")
    def test_the_applied_limit_is_the_configured_one(self):
        import resource
        apply = kr._child_limits()
        self.assertIsNotNone(apply)
        apply()
        self.assertEqual(
            resource.getrlimit(resource.RLIMIT_FSIZE),
            (kr.MAX_CHILD_FILE_BYTES, kr.MAX_CHILD_FILE_BYTES))

    def test_the_spawn_passes_the_limit(self):
        # Asserted from the source, the way the installer and README assets are
        # asserted: the child this line governs only ever exists in the VM, and
        # stubbing Popen just to read one kwarg back would test the stub.
        source = Path(__file__).resolve().parents[1] / "kestrel_run.py"
        body = source.read_text(encoding="utf-8").split("def run_step(")[1]
        run_step = body.split("\ndef ")[0]
        self.assertIn("preexec_fn=_child_limits(),", run_step)
