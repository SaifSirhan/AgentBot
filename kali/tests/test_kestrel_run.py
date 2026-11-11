import contextlib
import io
import json
import os
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
        self.validate(["nmap", "-S", "192.168.56.10", "192.168.56.11"])
        for argv in (
            ["nmap", "-S", "8.8.8.8", "192.168.56.10"],
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
        marker_dir = self.root / "env"
        marker_dir.mkdir()
        marker = write_scope(marker_dir, networks=["192.0.2.0/24"])
        env = dict(
            os.environ,
            KESTREL_SCOPE=str(marker),
            KESTREL_AUDIT=str(self.audit),
            KESTREL_OUTPUT=str(self.root / "envroot"),
        )
        completed = subprocess.run(
            [sys.executable, str(ROOT / "kestrel_run.py")],
            input=json.dumps({"version": 1, "mode": "scope"}),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        # The client-supplied audit destination must never be written.
        self.assertFalse(self.audit.exists())
        self.assertFalse((self.root / "envroot").exists())
        if completed.returncode == 0:
            # A real default scope exists on this host: it must not be the
            # client-supplied one.
            payload = json.loads(completed.stdout)
            self.assertNotIn("192.0.2.0/24", payload["scope"]["networks"])
        else:
            self.assertEqual(completed.returncode, 2)
            self.assertTrue(json.loads(completed.stdout)["denied"])

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
