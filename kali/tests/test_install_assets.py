"""Asset tests for the Kali install (T2-R2).

These tests are the only regression guard on the installer<->wrapper contract,
so they assert content that matters -- exact owners, modes, restriction options
and the ordering of the symlink guards -- never mere file existence.

Pure text/JSON assertions: no subprocess, no POSIX-only call, no network and no
tool execution, so the suite runs unchanged on Windows.
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Task 1 shipped these paths as constants; the installer must create exactly
# them, with the owner and mode the wrapper's own checks demand.
WRAPPER_CONSTANTS = (
    'DEFAULT_SCOPE = Path("/etc/kestrel/scope.json")',
    'DEFAULT_AUDIT = Path("/var/log/kestrel/audit.jsonl")',
    'OUTPUT_ROOT = Path("/tmp/kestrel")',
)
WRAPPER_PATHS = ("/etc/kestrel/scope.json", "/var/log/kestrel/audit.jsonl",
                 "/tmp/kestrel")

INSTALLED_WRAPPER = ('install -o root -g root -m 0755 "$HERE/kestrel_run.py" '
                     "/usr/local/bin/kestrel-run")
# /tmp is world-writable, so the output root is created with `mkdir` (which
# refuses an existing entry) and attributed in separate steps, never with
# `install -d`, whose check-then-apply pair a local user can win with a link.
OUTPUT_ROOT_MK = "mkdir -m 0700 /tmp/kestrel"
OUTPUT_ROOT_STEPS = (OUTPUT_ROOT_MK, "chown kestrel:kestrel /tmp/kestrel",
                     "chmod 0700 /tmp/kestrel")
AUDIT_DIR = "install -d -o kestrel -g kestrel -m 0700 /var/log/kestrel"
AUDIT_TOUCH = "touch /var/log/kestrel/audit.jsonl"
AUDIT_CHOWN = "chown kestrel:kestrel /var/log/kestrel/audit.jsonl"
AUDIT_CHMOD = "chmod 0600 /var/log/kestrel/audit.jsonl"
MASSCAN_SETCAP = 'setcap cap_net_raw+ep "$(command -v masscan)"'

FORCED_COMMAND = 'command="/usr/local/bin/kestrel-run"'
KEY_RESTRICTIONS = ("no-port-forwarding", "no-agent-forwarding", "no-pty",
                    "no-X11-forwarding", "no-user-rc")
SHELL_STARTUP_FILES = ("/home/kestrel/.bashrc", "/home/kestrel/.profile",
                       "/home/kestrel/.bash_logout")
SSH_RC = "/home/kestrel/.ssh/rc"
FORBIDDEN_ENV = ("KESTREL_SCOPE", "KESTREL_AUDIT", "KESTREL_OUTPUT")


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def logical_lines(text):
    """Whole commands: comments dropped, backslash continuations joined.

    Matching a whole command instead of a substring is what turns a dropped
    `-m 0700`, `-o kestrel` or restriction option into a failure rather than a
    silent pass, while still allowing the installer to wrap long lines.
    """
    joined = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1].strip() + " "
            continue
        joined.append((pending + line).strip())
        pending = ""
    if pending:
        joined.append(pending.strip())
    return joined


def commands(name):
    return logical_lines(read(name))


def section(number):
    """Body of README section `number`, heading included, up to the next ##."""
    for part in read("README.md").split("\n## ")[1:]:
        if part.startswith("%d." % number):
            return part
    raise AssertionError("README section %d is missing" % number)


def bash_commands(body):
    """Logical commands from the ```bash fences of one README section."""
    found = []
    for block in re.findall(r"```bash\n(.*?)```", body, re.DOTALL):
        found.extend(logical_lines(block))
    return found


def prose(body):
    """One README section as a single line, so a wrapped sentence still matches.

    The security instructions are prose, not commands: matching them across
    line wraps is what makes deleting the sentence a failure instead of a
    reflow that hides inside a substring check.
    """
    return " ".join(body.split())


def position(items, needle):
    if needle not in items:
        raise AssertionError("missing line: %s" % needle)
    return items.index(needle)


class InstallAssetTests(unittest.TestCase):
    def test_assets_exist(self):
        for name in ("kestrel_run.py", "install_kestrel_run.sh",
                     "scope.example.json", "logrotate.kestrel", "README.md"):
            self.assertTrue((ROOT / name).is_file(), name)

    # -- T2-R2: the installer must create the wrapper's boundary exactly -----

    def test_installer_creates_the_output_root_0700_kestrel_owned(self):
        # A 0755 or root-owned /tmp/kestrel makes every execute fail the
        # wrapper's output-root check, so the whole sequence is asserted.
        lines = commands("install_kestrel_run.sh")
        self.assertEqual([line for line in lines
                          if line.startswith("install -d")
                          and "/tmp/kestrel" in line], [])
        self.assertIn("if [ ! -e /tmp/kestrel ]; then", lines)
        order = [position(lines, line) for line in OUTPUT_ROOT_STEPS]
        self.assertEqual(order, sorted(order))

    def test_installer_creates_the_audit_dir_and_file_kestrel_owned(self):
        lines = commands("install_kestrel_run.sh")
        created = [line for line in lines
                   if line.startswith("install -d")
                   and "/var/log/kestrel" in line]
        self.assertEqual(created, [AUDIT_DIR])
        for line in (AUDIT_TOUCH, AUDIT_CHOWN, AUDIT_CHMOD):
            self.assertIn(line, lines)
        order = [position(lines, line) for line in
                 (AUDIT_DIR, AUDIT_TOUCH, AUDIT_CHOWN, AUDIT_CHMOD)]
        self.assertEqual(order, sorted(order))

    def test_installer_installs_wrapper_scope_and_logrotate_root_owned(self):
        lines = commands("install_kestrel_run.sh")
        for line in (INSTALLED_WRAPPER,
                     "install -d -o root -g root -m 0755 /etc/kestrel",
                     'install -o root -g root -m 0644 "$HERE/scope.example.json"'
                     " /etc/kestrel/scope.json",
                     'install -o root -g root -m 0644 "$HERE/logrotate.kestrel"'
                     " /etc/logrotate.d/kestrel"):
            self.assertIn(line, lines)

    def test_installer_paths_are_the_wrapper_constants(self):
        wrapper = read("kestrel_run.py")
        for constant in WRAPPER_CONSTANTS:
            self.assertIn(constant, wrapper)
        installer = read("install_kestrel_run.sh")
        for path in WRAPPER_PATHS:
            self.assertIn(path, installer)

    def test_installer_grants_masscan_only_cap_net_raw(self):
        lines = commands("install_kestrel_run.sh")
        self.assertEqual([line for line in lines if line.startswith("setcap")],
                         [MASSCAN_SETCAP])
        self.assertNotIn("cap_net_admin", read("install_kestrel_run.sh"))

    def test_installer_is_fail_fast_and_idempotent(self):
        text = read("install_kestrel_run.sh")
        self.assertIn("set -euo pipefail", text.splitlines())
        lines = commands("install_kestrel_run.sh")
        self.assertIn("if [ ! -e /etc/kestrel/scope.json ]; then", lines)
        self.assertIn(AUDIT_TOUCH, lines)          # touch, never truncate
        self.assertIn("mkdir -p /home/kestrel/.ssh", lines)
        self.assertNotIn("rm -rf /var/log/kestrel", text)
        self.assertNotIn("rm -rf /tmp/kestrel", text)

    # -- T2-R1: refuse a planted symlink BEFORE root applies attributes ------

    def assert_guard_before(self, path, guarded_line):
        lines = commands("install_kestrel_run.sh")
        start = position(lines, "if [ -L %s ]; then" % path)
        end = position(lines, guarded_line)
        self.assertLess(start, end,
                        "%s must be refused before %s" % (path, guarded_line))
        body = lines[start:end]
        self.assertIn('echo "refusing: %s is a symlink" >&2' % path, body)
        self.assertIn("exit 1", body)
        self.assertIn("fi", body)

    def test_installer_refuses_a_symlinked_output_root(self):
        self.assert_guard_before("/tmp/kestrel", OUTPUT_ROOT_MK)

    def test_installer_refuses_a_symlinked_audit_file(self):
        self.assert_guard_before("/var/log/kestrel/audit.jsonl", AUDIT_TOUCH)

    # -- T2-R3: no user-writable startup file, including ~/.ssh/rc -----------

    def test_installer_removes_every_user_writable_startup_file(self):
        lines = commands("install_kestrel_run.sh")
        removals = [line for line in lines if line.startswith("rm -f")]
        startup = [line for line in removals if SHELL_STARTUP_FILES[0] in line]
        self.assertEqual(len(startup), 1)
        for path in SHELL_STARTUP_FILES:
            self.assertIn(path, startup[0])
        # PermitUserRC defaults to yes and sshd runs this file as kestrel
        # BEFORE the forced command; root ownership does not remove it.
        self.assertIn("rm -f %s" % SSH_RC, lines)
        self.assertIn("chown -R root:root /home/kestrel", lines)
        self.assertIn("chmod 0755 /home/kestrel /home/kestrel/.ssh", lines)
        self.assertLess(position(lines, "rm -f %s" % SSH_RC),
                        position(lines, "chown -R root:root /home/kestrel"))

    def test_readme_removes_the_user_rc_before_root_owns_the_home(self):
        # The operator follows README 1, not the installer's source. A planted
        # rc survives there and sshd runs it as kestrel BEFORE the forced
        # command; chown -R would only make it root-owned, so the removal must
        # come first, exactly as the installer does it.
        lines = bash_commands(section(1))
        rc = "sudo rm -f %s" % SSH_RC
        self.assertIn(rc, lines)
        self.assertLess(position(lines, rc),
                        position(lines, "sudo chown -R root:root /home/kestrel"))

    # -- T2-R2/R3: the forced-command line is the whole SSH boundary ---------

    def test_readme_forced_command_carries_every_restriction(self):
        forced = [line.strip() for line in read("README.md").splitlines()
                  if line.strip().startswith('command="')]
        self.assertEqual(len(forced), 1, "exactly one forced-command line")
        options, _, key = forced[0].partition(" ")
        self.assertTrue(key.startswith("ssh-ed25519 "), key)
        parts = options.split(",")
        self.assertEqual(parts[0], FORCED_COMMAND)
        self.assertEqual(set(parts), {FORCED_COMMAND} | set(KEY_RESTRICTIONS))

    def test_readme_explains_the_root_owned_rc_free_ssh_state(self):
        body = section(3)
        self.assertIn("sudo chown root:root /home/kestrel/.ssh/authorized_keys",
                      body)
        self.assertIn("sudo chmod 0644 /home/kestrel/.ssh/authorized_keys", body)
        self.assertIn("PermitUserRC", body)
        self.assertIn(SSH_RC, body)

    # -- T2-R4: lock the password without depending on UsePAM ----------------

    def test_readme_locks_the_password_without_pam(self):
        self.assertIn("sudo usermod -p '*' kestrel",
                      bash_commands(section(1)))
        # Prose may explain why `passwd -l` is wrong; no command may run it.
        for line in bash_commands(read("README.md")):
            self.assertNotIn("passwd -l", line)
        text = read("README.md")
        self.assertIn("PubkeyAuthentication yes", text)
        self.assertIn("UsePAM", text)

    # -- T2-R6: the fuzzers need the allowlisted wordlist directories --------

    def test_readme_installs_the_wordlist_packages(self):
        apt = [line for line in bash_commands(section(1))
               if line.startswith("sudo apt install")]
        self.assertEqual(len(apt), 1)
        for package in ("openssh-server", "nmap", "masscan", "ffuf", "gobuster",
                        "feroxbuster", "libcap2-bin", "seclists", "wordlists"):
            self.assertIn(package, apt[0].split())
        text = read("README.md")
        self.assertIn("/usr/share/seclists", text)
        self.assertIn("/usr/share/wordlists", text)

    # -- T2-R8: a host-only adapter inside the seeded scope ------------------

    def test_readme_requires_a_host_only_adapter_inside_the_scope(self):
        body = section(1)
        self.assertIn("attach a host-only adapter", body.lower())
        self.assertIn("192.168.56.0/24", body)
        self.assertIn("ip -brief addr", body)
        self.assertLess(body.lower().index("host-only"),
                        body.index("sudo apt install"))

    # -- T2-R5: recording the host key is TOFU unless the console is used ----

    def test_readme_host_key_step_compares_the_console_fingerprint(self):
        body = section(5)
        self.assertIn("trust-on-first-use", body.lower())
        self.assertIn("console", body.lower())
        self.assertIn("ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub", body)
        self.assertGreaterEqual(body.count("ssh-keygen -lf"), 2)
        self.assertIn("known_hosts", body)
        # Windows PowerShell 5.1 redirects as UTF-16, which OpenSSH rejects
        # with "is not a public key file"; the step must write ASCII.
        self.assertIn("| Out-File -Encoding ascii", body)
        # Printing two fingerprints is not the step; comparing them is. Without
        # this instruction ssh-keyscan stays bare TOFU and a substituted host
        # key is accepted silently.
        step = prose(body).lower()
        self.assertIn("`sha256:` fingerprint", step)
        self.assertIn("must match the console's character for character", step)
        self.assertIn("if it does not, stop", step)
        self.assertIn("delete `known_hosts`", step)
        # The comparison guards the connection, so it must precede it.
        self.assertLess(step.index("character for character"),
                        step.index("ssh -i "),
                        "verify the fingerprint before connecting")
        self.assertNotIn("## 5. Pin the host key", read("README.md"))

    # -- T2-R7: honest capability wording and a masscan live check -----------

    def test_readme_capability_wording_is_accurate(self):
        text = read("README.md")
        self.assertIn("pass policy but fail without root", text)
        self.assertNotIn("are not enabled", text)
        self.assertIn("cap_net_raw", text)

    def test_readme_live_checks_exercise_masscan(self):
        body = section(7)
        # The exact command, not just the tool name: masscan is the one tool
        # whose capability grant is installer-specific, so it must be run.
        self.assertIn("masscan -p80 --rate 100 192.168.56.20", body)
        self.assertIn("cap_net_raw", body)
        self.assertIn("kali_scope", body)
        self.assertIn("kali_task", body)
        self.assertIn("nmap -sV 8.8.8.8", body)
        self.assertIn('bash -c "id"', body)

    def test_readme_documents_the_installer_symlink_refusal(self):
        body = section(2)
        self.assertIn("symlink", body)
        self.assertIn("/tmp/kestrel", body)
        self.assertIn("/var/log/kestrel/audit.jsonl", body)
        # The installer refuses on BOTH paths, so both need a recovery command.
        # Documenting only the output root leaves the operator stuck on the
        # audit refusal with no way forward.
        text = prose(body)
        self.assertIn("`sudo rm /tmp/kestrel`", text)
        self.assertIn("`sudo rm /var/log/kestrel/audit.jsonl`", text)

    def test_readme_documents_how_kali_reaches_the_vm(self):
        # The installer reads three files beside itself, so "copy kali/" is the
        # only correct instruction; and a copy made through Windows can arrive
        # with CRLF, which bash rejects only at run time.
        body = prose(section(2))
        self.assertIn("copy the whole `kali/` directory", body.lower())
        self.assertIn("kestrel_run.py", body)
        self.assertIn("bash -n kali/install_kestrel_run.sh", body)
        self.assertIn("CRLF", body)
        self.assertIn("sed -i 's/\\r$//' kali/*.sh", body)

    # -- NAT port-forward topology: the guide must not assume host-only ------

    def test_readme_distinguishes_the_two_transports(self):
        # The wrapper resolves targets inside the VM, so the guide has to say
        # which link the VM actually has: a host-only address, or a NAT address
        # reached through a forwarded loopback port on Windows.
        body = prose(section(1)).lower()
        self.assertIn("model a", body)
        self.assertIn("model b", body)
        self.assertIn("port forward", body)
        self.assertIn("10.0.2.15", body)
        self.assertIn("get-netipaddress", body)

    def test_readme_warns_before_claiming_a_subnet_the_host_already_owns(self):
        # 192.168.56.1 is VirtualBox's default host-only address AND the Windows
        # WSL/Hyper-V switch address. A guide that tells everyone to use it
        # silently sets up two adapters on one subnet, where Windows routing
        # picks which machine an in-scope target reaches.
        body = prose(section(1)).lower()
        self.assertIn("192.168.56.1", body)
        self.assertIn("vethernet (wsl", body)
        self.assertIn("already taken", body)
        self.assertIn("192.168.120", body)

    def test_readme_scope_step_says_targets_resolve_inside_the_vm(self):
        body = prose(section(4)).lower()
        self.assertIn("resolved inside the vm", body)
        self.assertIn("127.0.0.0/8", body)
        self.assertIn("10.0.2.0/24", body)
        self.assertIn("never leaves the vm", body)

    def test_readme_keeps_allow_public_as_the_lab_boundary(self):
        # The temptation this documents is the one that actually happened:
        # widening allow_public to stop an out-of-scope refusal.
        body = prose(section(4)).lower()
        self.assertIn("do not set `allow_public` to `true`", body)
        self.assertIn("add that target", body)

    def test_readme_host_key_step_matches_the_configured_port(self):
        # known_hosts keys on host[:port]. An entry recorded for a different
        # host or port than kali.json uses makes every call fail host-key
        # verification, which reads as a broken tool rather than a wrong file.
        body = prose(section(5))
        self.assertIn("ssh-keyscan -p 2222 127.0.0.1 | Out-File -Encoding ascii",
                      body)
        self.assertIn("[127.0.0.1]:2222", body)
        self.assertIn("Host key verification failed", body)

    def test_readme_config_and_live_checks_cover_the_forward(self):
        config = section(6)
        self.assertIn('"host": "127.0.0.1"', config)
        self.assertIn('"port": 2222', config)
        # Commands keep their flag case; only the prose is matched loosely.
        checks = prose(section(7))
        self.assertIn("nmap -sV 127.0.0.1", checks)
        self.assertIn("step 4", checks.lower())

    # -- Shipped Task 1 facts the assets must not contradict -----------------

    def test_assets_never_configure_the_wrapper_by_environment(self):
        for name in ("install_kestrel_run.sh", "README.md"):
            text = read(name)
            for variable in FORBIDDEN_ENV:
                self.assertNotIn(variable, text)

    def test_readme_keeps_the_seven_operator_sections_in_order(self):
        headings = re.findall(r"^## (\d)\.", read("README.md"), re.MULTILINE)
        self.assertEqual(headings, [str(number) for number in range(1, 8)])

    def test_logrotate_targets_the_audit_log(self):
        text = read("logrotate.kestrel")
        self.assertIn("/var/log/kestrel/audit.jsonl {", text)
        self.assertIn("rotate 12", text)
        # Rotation must leave the audit file writable by kestrel, or the
        # wrapper's audit-before-execute rule refuses every later execute.
        self.assertIn("create 0600 kestrel kestrel", text)

    def test_scope_example_is_private_and_versioned(self):
        data = json.loads(read("scope.example.json"))
        self.assertEqual(data["version"], 1)
        self.assertFalse(data["allow_public"])
        self.assertIn("192.168.56.0/24", data["networks"])
        self.assertIn("*.lab.local", data["domains"])
        self.assertIn("192.168.56.0/24", section(4))


if __name__ == "__main__":
    unittest.main()
