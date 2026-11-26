"""Real Windows integration tests for the execute_python sandbox.

Run: python -m unittest tests.test_python_sandbox -v
"""
import ctypes
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import python_sandbox

PROJECT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(os.name == "nt", "Windows known-folder test")
class DefaultRootsTests(unittest.TestCase):
    def test_default_roots_resolve_redirected_known_folders(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            home = base / "home"
            downloads = home / "Downloads"
            documents = home / "Documents"
            desktop = home / "Desktop"
            redirected = base / "OneDrive"
            known = {
                "Downloads": str(redirected / "Downloads"),
                "Documents": str(redirected / "Documents"),
                "Desktop": str(redirected / "Desktop"),
            }
            for path in (project, downloads, documents, desktop, *(
                    Path(value) for value in known.values())):
                path.mkdir(parents=True)
            with mock.patch.object(python_sandbox, "__file__",
                                   str(project / "python_sandbox.py")), \
                    mock.patch.dict(os.environ, {"USERPROFILE": str(home)}), \
                    mock.patch.object(python_sandbox, "_known_folder", create=True,
                                      side_effect=known.__getitem__):
                roots = python_sandbox._default_roots()
            self.assertEqual(
                {os.path.normcase(path) for path in roots},
                {os.path.normcase(str(path)) for path in
                 (project, *(Path(value) for value in known.values()))})


@unittest.skipUnless(os.name == "nt", "Windows AppContainer integration tests")
class PythonSandboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared_temp = tempfile.TemporaryDirectory()
        cls.production_profile = python_sandbox._profile_identity()[0]
        cls.state_path = str(Path(cls.shared_temp.name) / "sandbox-state.json")
        state_digest = hashlib.sha256(cls.state_path.encode("utf-8")).hexdigest()[:12]
        profile_name = f"Kestrel.PythonSandbox.Tests.{state_digest}"
        cls.profile_name = profile_name
        cls.identity_patch = mock.patch.object(
            python_sandbox, "_profile_identity",
            return_value=(profile_name, "S-1-test", cls.state_path))
        cls.identity_patch.start()

    @classmethod
    def tearDownClass(cls):
        try:
            profile_name, state_path = cls.profile_name, cls.state_path
            _, advapi, userenv = python_sandbox._apis()
            sid_pointer = ctypes.c_void_p()
            code = userenv.DeriveAppContainerSidFromAppContainerName(
                profile_name, ctypes.byref(sid_pointer))
            if code:
                raise AssertionError(f"cannot derive test SID: {code & 0xffffffff:#x}")
            try:
                sid = python_sandbox._sid_string(sid_pointer)
            finally:
                advapi.FreeSid(sid_pointer)
            python_sandbox._configure_access(
                sid, [], [], state_path, cls.shared_temp.name)
        finally:
            cls.identity_patch.stop()
            cls.shared_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        secret = mock.patch.dict(
            os.environ, {"KESTREL_SANDBOX_SECRET": "must-not-leak"})
        secret.start()
        self.addCleanup(secret.stop)
        config_patch = mock.patch("config.CONFIG_PATH", str(self.root / "config.json"))
        memory_patch = mock.patch(
            "config.memory_dir", return_value=str(self.root / "memory"))
        config_patch.start()
        memory_patch.start()
        self.addCleanup(config_patch.stop)
        self.addCleanup(memory_patch.stop)

    def run_code(self, code, **kwargs):
        options = {
            "cwd": str(self.root),
            "writable_roots": [str(self.root)],
            "timeout": 10,
        }
        options.update(kwargs)
        return python_sandbox.run_python(code, **options)

    def assert_ok(self, result):
        self.assertIsNone(result.error)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unicode_stdout_and_stderr_stay_separate(self):
        result = self.run_code(
            "import sys; print('caf\u00e9 \u0639\u0631\u0628\u0649'); "
            "print('warning', file=sys.stderr)")
        self.assert_ok(result)
        self.assertEqual(result.stdout, "caf\u00e9 \u0639\u0631\u0628\u0649")
        self.assertEqual(result.stderr, "warning")

    def test_project_imports_cwd_and_dash_c_semantics_are_preserved(self):
        result = self.run_code(
            "import file_tools, os, sys; "
            "print(os.getcwd()); print(__name__); print(__file__); "
            "print(sys.argv)",
            cwd=str(PROJECT), writable_roots=[str(PROJECT), str(self.root)])
        self.assert_ok(result)
        lines = result.stdout.splitlines()
        self.assertEqual(Path(lines[0]), PROJECT)
        self.assertEqual(lines[1:3], ["__main__", "<string>"])
        self.assertEqual(lines[3], "['-c']")

    def test_installed_packages_are_importable(self):
        result = self.run_code("import docx; print(docx.__version__)")
        self.assert_ok(result)
        self.assertTrue(result.stdout.strip())

    def test_receipt_target_overflow_stops_the_run(self):
        first = self.root / "first.txt"
        second = self.root / "second.txt"
        code = (
            "from pathlib import Path\n"
            f"Path({str(first)!r}).write_text('one', encoding='utf-8')\n"
            f"Path({str(second)!r}).write_text('two', encoding='utf-8')"
        )
        with mock.patch.object(python_sandbox, "_RECEIPT_MAX_TARGETS", 1):
            result = self.run_code(code)
        self.assertIsNotNone(result.error)
        self.assertIn("receipt", result.error.lower())
        self.assertEqual(first.read_text(encoding="utf-8"), "one")
        self.assertFalse(second.exists())

    def test_allowed_root_supports_create_edit_and_delete(self):
        edited = self.root / "edited.txt"
        deleted = self.root / "deleted.txt"
        result = self.run_code(
            "from pathlib import Path\n"
            f"edited = Path({str(edited)!r})\n"
            f"deleted = Path({str(deleted)!r})\n"
            "edited.write_text('one', encoding='utf-8')\n"
            "edited.write_text('two', encoding='utf-8')\n"
            "deleted.write_text('gone', encoding='utf-8')\n"
            "deleted.unlink()\n")
        self.assert_ok(result)
        self.assertEqual(edited.read_text(encoding="utf-8"), "two")
        self.assertFalse(deleted.exists())

    def test_test_profile_is_isolated_from_production_acl_state(self):
        test_profile = python_sandbox._profile_identity()[0]
        self.assertNotEqual(test_profile, self.production_profile)
        _, advapi, userenv = python_sandbox._apis()
        sids = []
        for name in (test_profile, self.production_profile):
            sid = ctypes.c_void_p()
            code = userenv.DeriveAppContainerSidFromAppContainerName(
                name, ctypes.byref(sid))
            self.assertEqual(code, 0)
            try:
                sids.append(python_sandbox._sid_string(sid))
            finally:
                advapi.FreeSid(sid)
        self.assertNotEqual(sids[0], sids[1])

    def test_default_run_does_not_grant_read_access_to_project_root(self):
        _, advapi, userenv = python_sandbox._apis()
        sid_pointer = ctypes.c_void_p()
        code = userenv.DeriveAppContainerSidFromAppContainerName(
            self.profile_name, ctypes.byref(sid_pointer))
        self.assertEqual(code, 0)
        try:
            sid = python_sandbox._sid_string(sid_pointer)
        finally:
            advapi.FreeSid(sid_pointer)
        self.assert_ok(self.run_code("print('ok')"))
        self.assertEqual(
            AccessControlTests.access_aces(os.path.normcase(str(PROJECT)), sid), [])

    def test_allowed_root_supports_directory_tree_deletion(self):
        tree = self.root / "tree"
        (tree / "nested").mkdir(parents=True)
        (tree / "nested" / "file.txt").write_text("data", encoding="utf-8")
        result = self.run_code(
            "import shutil\n"
            f"shutil.rmtree({str(tree)!r})")
        self.assert_ok(result)
        self.assertFalse(tree.exists())

    def test_write_outside_allowed_roots_is_denied(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "blocked.txt"
            result = self.run_code(
                "from pathlib import Path\n"
                "try:\n"
                f"    Path({str(target)!r}).write_text('bad', encoding='utf-8')\n"
                "except OSError as exc:\n"
                "    print('blocked', exc.errno)\n")
            self.assert_ok(result)
            self.assertTrue(result.stdout.startswith("blocked"))
            self.assertFalse(target.exists())

    def test_read_root_label_blocks_descendant_writes_despite_existing_allows(self):
        library = self.root / "library"
        workspace = self.root / "workspace"
        library.mkdir()
        workspace.mkdir()
        existing = library / "existing.txt"
        created = library / "created.txt"
        existing.write_text("before", encoding="utf-8")
        _, advapi, userenv = python_sandbox._apis()
        sid_pointer = ctypes.c_void_p()
        code = userenv.DeriveAppContainerSidFromAppContainerName(
            python_sandbox._profile_identity()[0], ctypes.byref(sid_pointer))
        self.assertEqual(code, 0)
        try:
            sid = python_sandbox._sid_string(sid_pointer)
        finally:
            advapi.FreeSid(sid_pointer)
        python_sandbox._set_root_access(
            os.path.normcase(str(existing)), sid, python_sandbox._MODIFY_ACCESS)
        result = self.run_code(
            "from pathlib import Path\n"
            f"existing = Path({str(existing)!r})\n"
            f"created = Path({str(created)!r})\n"
            "try:\n"
            "    existing.write_text('changed', encoding='utf-8')\n"
            "except OSError as exc:\n"
            "    print('existing-blocked', exc.errno)\n"
            "else:\n"
            "    print('existing-allowed')\n"
            "try:\n"
            "    created.write_text('created', encoding='utf-8')\n"
            "except OSError as exc:\n"
            "    print('created-blocked', exc.errno)\n"
            "else:\n"
            "    print('created-allowed')\n"
            "print('read', existing.read_text(encoding='utf-8'))",
            cwd=str(library), writable_roots=[str(workspace)])
        self.assert_ok(result)
        self.assertEqual(
            result.stdout.splitlines(),
            ["existing-blocked 13", "created-blocked 13", "read before"])
        self.assertEqual(existing.read_text(encoding="utf-8"), "before")
        self.assertFalse(created.exists())

    def test_secret_file_read_is_denied(self):
        secret = self.root / ".env"
        secret.write_text("dummy-secret", encoding="utf-8")
        result = self.run_code(
            "from pathlib import Path\n"
            "try:\n"
            f"    Path({str(secret)!r}).read_text(encoding='utf-8')\n"
            "except OSError as exc:\n"
            "    print('blocked', exc.errno)\n")
        self.assert_ok(result)
        self.assertTrue(result.stdout.startswith("blocked"))
        self.assertEqual(secret.read_text(encoding="utf-8"), "dummy-secret")

    def test_sandbox_cannot_grant_itself_dacl_control(self):
        target = self.root / "acl-target.txt"
        target.write_text("original", encoding="utf-8")
        result = self.run_code(
            "import ctypes\n"
            "from ctypes import wintypes\n"
            "advapi = ctypes.WinDLL('advapi32', use_last_error=True)\n"
            "advapi.SetNamedSecurityInfoW.argtypes = [\n"
            "    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,\n"
            "    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,\n"
            "    ctypes.c_void_p,\n"
            "]\n"
            "advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD\n"
            f"code = advapi.SetNamedSecurityInfoW({str(target)!r}, 1, 4, None, None, None, None)\n"
            "print('blocked' if code else 'changed')")
        self.assert_ok(result)
        self.assertEqual(result.stdout, "blocked")
        self.assertEqual(target.read_text(encoding="utf-8"), "original")

    def test_secret_in_a_readable_only_project_is_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            secret = project / ".env"
            secret.write_text("dummy-secret", encoding="utf-8")
            result = self.run_code(
                "from pathlib import Path\n"
                "try:\n"
                f"    print(Path({str(secret)!r}).read_text(encoding='utf-8'))\n"
                "except OSError as exc:\n"
                "    print('blocked', exc.errno)",
                cwd=str(project))
            self.assert_ok(result)
            self.assertTrue(result.stdout.startswith("blocked"))
            self.assertEqual(secret.read_text(encoding="utf-8"), "dummy-secret")

    def test_all_application_packages_cannot_bypass_secret_protection(self):
        secret = self.root / ".env"
        secret.write_text("dummy-secret", encoding="utf-8")
        self.assertTrue(python_sandbox._run_icacls(
            str(secret), ["/grant", "*S-1-15-2-1:(F)"]))
        moved = self.root / "moved.env"
        result = self.run_code(
            "from pathlib import Path\n"
            f"secret = Path({str(secret)!r})\n"
            f"moved = Path({str(moved)!r})\n"
            "blocked = 0\n"
            "for operation in (\n"
            "    lambda: secret.write_text('changed', encoding='utf-8'),\n"
            "    secret.unlink,\n"
            "    lambda: secret.rename(moved),\n"
            "):\n"
            "    try:\n"
            "        operation()\n"
            "    except OSError:\n"
            "        blocked += 1\n"
            "print(blocked)")
        self.assert_ok(result)
        self.assertEqual(result.stdout, "3")
        self.assertEqual(secret.read_text(encoding="utf-8"), "dummy-secret")
        self.assertFalse(moved.exists())

    def test_secret_cannot_be_written_deleted_or_renamed(self):
        secret = self.root / ".env"
        secret.write_text("dummy-secret", encoding="utf-8")
        moved = self.root / "moved.env"
        result = self.run_code(
            "from pathlib import Path\n"
            f"secret = Path({str(secret)!r})\n"
            f"moved = Path({str(moved)!r})\n"
            "blocked = 0\n"
            "for operation in (\n"
            "    lambda: secret.write_text('changed', encoding='utf-8'),\n"
            "    secret.unlink,\n"
            "    lambda: secret.rename(moved),\n"
            "):\n"
            "    try:\n"
            "        operation()\n"
            "    except OSError:\n"
            "        blocked += 1\n"
            "print(blocked)")
        self.assert_ok(result)
        self.assertEqual(result.stdout, "3")
        self.assertEqual(secret.read_text(encoding="utf-8"), "dummy-secret")
        self.assertFalse(moved.exists())

    def test_git_and_venv_are_read_only_even_inside_an_allowed_root(self):
        protected = []
        for name in (".git", ".venv"):
            folder = self.root / name
            folder.mkdir()
            existing = folder / "config.txt"
            existing.write_text("original", encoding="utf-8")
            self.assertTrue(python_sandbox._run_icacls(
                str(folder), ["/grant", "*S-1-15-2-1:(F)"]))
            protected.append((folder, existing))
        code = ["from pathlib import Path", "reads = 0", "blocked = 0"]
        for folder, existing in protected:
            code.extend([
                "try:",
                f"    if Path({str(existing)!r}).read_text(encoding='utf-8') == 'original':",
                "        reads += 1",
                "except OSError:",
                "    pass",
                "try:",
                f"    Path({str(existing)!r}).write_text('changed', encoding='utf-8')",
                "except OSError:",
                "    blocked += 1",
                "try:",
                f"    Path({str(folder / 'new.txt')!r}).write_text('bad', encoding='utf-8')",
                "except OSError:",
                "    blocked += 1",
                "try:",
                f"    Path({str(folder)!r}).rename(Path({str(folder.with_name(folder.name + '-moved'))!r}))",
                "except OSError:",
                "    blocked += 1",
            ])
        code.append("print(reads, blocked)")
        result = self.run_code("\n".join(code))
        self.assert_ok(result)
        self.assertEqual(result.stdout, "2 6")
        for folder, existing in protected:
            self.assertEqual(existing.read_text(encoding="utf-8"), "original")
            self.assertFalse((folder / "new.txt").exists())
            self.assertFalse(folder.with_name(folder.name + "-moved").exists())
            host_file = folder / "host-check.txt"
            host_file.write_text("host", encoding="utf-8")
            self.assertEqual(host_file.read_text(encoding="utf-8"), "host")

    def test_protected_named_junctions_are_read_only(self):
        protected = []
        for name in (".git", ".venv"):
            target = self.root / f"{name[1:]}_target"
            target.mkdir()
            existing = target / "config.txt"
            existing.write_text("original", encoding="utf-8")
            junction = self.root / name
            created = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction),
                 str(target)], capture_output=True, text=True, check=False)
            self.assertEqual(created.returncode, 0, created.stderr)
            protected.append((junction, target, existing))
        code = ["from pathlib import Path", "blocked = 0"]
        for junction, _target, existing in protected:
            code.extend([
                f"print(Path({str(existing)!r}).read_text(encoding='utf-8'))",
                "try:",
                f"    Path({str(junction / 'config.txt')!r}).write_text('changed', encoding='utf-8')",
                "except OSError:",
                "    blocked += 1",
                "try:",
                f"    Path({str(junction / 'new.txt')!r}).write_text('bad', encoding='utf-8')",
                "except OSError:",
                "    blocked += 1",
            ])
        code.append("print(blocked)")
        result = self.run_code("\n".join(code))
        self.assert_ok(result)
        self.assertEqual(result.stdout.splitlines(), ["original", "original", "4"])
        for _junction, _target, existing in protected:
            self.assertEqual(existing.read_text(encoding="utf-8"), "original")
            self.assertFalse((existing.parent / "new.txt").exists())

    def test_out_of_root_protected_junction_fails_closed_without_grant(self):
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory)
            secret = outside / "id_rsa"
            secret.write_text("dummy external secret", encoding="utf-8")
            self.assert_ok(self.run_code("print('ready')"))
            junction = self.root / ".git"
            create = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction),
                 str(outside)], capture_output=True, text=True, check=False)
            self.assertEqual(create.returncode, 0, create.stderr)

            profile_name, _, _ = python_sandbox._profile_identity()
            python_sandbox._ensure_profile(profile_name)
            icacls = Path(os.environ["SystemRoot"]) / "System32" / "icacls.exe"
            before_acl = subprocess.run(
                [str(icacls), str(outside)], check=True,
                capture_output=True, text=True).stdout
            result = self.run_code(
                "from pathlib import Path\n"
                f"print(Path({str(secret)!r}).read_text(encoding='utf-8'))")
            after_acl = subprocess.run(
                [str(icacls), str(outside)], check=True,
                capture_output=True, text=True).stdout

            self.assertIsNotNone(result.error)
            self.assertNotIn("dummy external secret", result.stdout)
            self.assertEqual(after_acl, before_acl)
            self.assertEqual(secret.read_text(encoding="utf-8"),
                             "dummy external secret")

    def test_protected_junction_entry_cannot_be_removed(self):
        target = self.root / "git_target"
        target.mkdir()
        junction = self.root / ".git"
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction),
             str(target)], capture_output=True, text=True, check=False)
        self.assertEqual(created.returncode, 0, created.stderr)
        result = self.run_code(
            "import os\n"
            "try:\n"
            f"    os.rmdir({str(junction)!r})\n"
            "except OSError:\n"
            "    pass\n"
            "print('present' if os.path.lexists("
            f"{str(junction)!r}) else 'missing')")
        self.assert_ok(result)
        self.assertEqual(result.stdout, "present")
        self.assertTrue(junction.is_dir())

    def test_protected_acl_remains_valid_across_consecutive_runs(self):
        folder = self.root / ".git"
        folder.mkdir()
        existing = folder / "config.txt"
        existing.write_text("original", encoding="utf-8")
        code = (
            "from pathlib import Path\n"
            f"print(Path({str(existing)!r}).read_text(encoding='utf-8'))")
        self.assert_ok(self.run_code(code))
        self.assert_ok(self.run_code(code))

    def test_nested_git_and_venv_are_also_read_only(self):
        protected = []
        for name in (".git", ".venv"):
            folder = self.root / "nested" / name
            folder.mkdir(parents=True)
            existing = folder / "config.txt"
            existing.write_text("original", encoding="utf-8")
            protected.append((folder, existing))
        code = ["from pathlib import Path", "blocked = 0"]
        for folder, existing in protected:
            code.extend([
                "try:",
                f"    Path({str(existing)!r}).write_text('changed', encoding='utf-8')",
                "except OSError:",
                "    blocked += 1",
                "try:",
                f"    Path({str(folder / 'new.txt')!r}).write_text('bad', encoding='utf-8')",
                "except OSError:",
                "    blocked += 1",
            ])
        code.append("print(blocked)")
        result = self.run_code("\n".join(code))
        self.assert_ok(result)
        self.assertEqual(result.stdout, "4")
        for folder, existing in protected:
            self.assertEqual(existing.read_text(encoding="utf-8"), "original")
            self.assertFalse((folder / "new.txt").exists())

    def test_host_environment_is_not_inherited(self):
        result = self.run_code(
            "import os; print(repr(os.environ.get('KESTREL_SANDBOX_SECRET')))")
        self.assert_ok(result)
        self.assertEqual(result.stdout, "None")

    def test_temp_and_home_are_private_and_cleaned_up(self):
        result = self.run_code(
            "import os, tempfile; "
            "print(tempfile.gettempdir()); print(os.path.expanduser('~'))")
        self.assert_ok(result)
        temp_path, home_path = map(Path, result.stdout.splitlines())
        self.assertEqual(temp_path, home_path)
        self.assertNotEqual(temp_path, Path(tempfile.gettempdir()))
        self.assertFalse(temp_path.exists())

    def test_cleanup_does_not_follow_a_junction_outside_the_run_tree(self):
        profile_name, _, _ = python_sandbox._profile_identity()
        sid = python_sandbox._ensure_profile(profile_name)
        icacls = Path(os.environ["SystemRoot"]) / "System32" / "icacls.exe"
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside)
            sentinel = external / "sentinel.txt"
            sentinel.write_text("keep", encoding="utf-8")
            self.assertTrue(python_sandbox._run_icacls(
                str(external), ["/grant", f"*{sid}:(OI)(CI)(M)"]))
            before_acl = subprocess.run(
                [str(icacls), str(external)], check=True,
                capture_output=True, text=True).stdout
            code = (
                "import os, subprocess\n"
                "from pathlib import Path\n"
                "link = Path(os.environ['TEMP']).parent / 'nested' / 'escape'\n"
                "link.parent.mkdir()\n"
                f"subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', "
                f"str(link), {str(external)!r}], check=True, capture_output=True)\n"
                "print('linked')")
            result = self.run_code(code)
            self.assert_ok(result)
            self.assertEqual(result.stdout, "linked")
            after_acl = subprocess.run(
                [str(icacls), str(external)], check=True,
                capture_output=True, text=True).stdout
            self.assertTrue(sentinel.is_file())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertEqual(after_acl, before_acl)

    def test_child_is_an_appcontainer_in_the_controlled_job(self):
        result = self.run_code("print('ok')")
        self.assert_ok(result)
        self.assertTrue(result.app_container)
        self.assertTrue(result.job_controlled)

    def test_live_loopback_listener_receives_no_connection(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(2)
        port = listener.getsockname()[1]
        result = self.run_code(
            "import socket\n"
            f"s = socket.socket()\n"
            f"s.settimeout(1)\n"
            f"try:\n"
            f"    s.connect(('127.0.0.1', {port}))\n"
            f"    print('connected')\n"
            f"except OSError:\n"
            f"    print('blocked')\n"
            f"finally:\n"
            f"    s.close()\n")
        self.assert_ok(result)
        self.assertEqual(result.stdout, "blocked")
        with self.assertRaises(socket.timeout):
            listener.accept()

    def test_timeout_kills_a_spawned_descendant(self):
        pidfile = self.root / "child.pid"
        code = (
            "import subprocess, sys, time\n"
            "from pathlib import Path\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(30)'])\n"
            f"Path({str(pidfile)!r}).write_text(str(child.pid), encoding='ascii')\n"
            "time.sleep(30)\n")
        process_started_at = []
        original_start = python_sandbox._start_process

        def timed_start(*args, **kwargs):
            started = original_start(*args, **kwargs)
            process_started_at.append(time.monotonic())
            return started

        with mock.patch.object(python_sandbox, "_start_process",
                               side_effect=timed_start):
            result = self.run_code(code, timeout=3)
        self.assertIsNone(result.error)
        self.assertTrue(result.timed_out)
        self.assertEqual(len(process_started_at), 1)
        self.assertLess(time.monotonic() - process_started_at[0], 8)
        self.assertFalse(self.process_is_running(self.wait_for_pid(pidfile)))

    def test_cancellation_kills_a_spawned_descendant(self):
        pidfile = self.root / "child.pid"
        code = (
            "import subprocess, sys, time\n"
            "from pathlib import Path\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(30)'])\n"
            f"Path({str(pidfile)!r}).write_text(str(child.pid), encoding='ascii')\n"
            "time.sleep(30)\n")
        event = threading.Event()
        process_started = threading.Event()
        cancel_requested_at = []
        errors = []
        original_start = python_sandbox._start_process

        def timed_start(*args, **kwargs):
            try:
                return original_start(*args, **kwargs)
            finally:
                process_started.set()

        def cancel_after_spawn():
            try:
                if not process_started.wait(python_sandbox._SETUP_TIMEOUT + 5):
                    raise AssertionError("sandbox process did not start")
                self.wait_for_pid(pidfile)
                cancel_requested_at.append(time.monotonic())
                event.set()
            except Exception as exc:
                errors.append(exc)

        canceller = threading.Thread(target=cancel_after_spawn, daemon=True)
        canceller.start()
        with mock.patch.object(python_sandbox, "_start_process",
                               side_effect=timed_start):
            result = self.run_code(code, stop_event=event)
        canceller.join(timeout=10)
        self.assertFalse(canceller.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNone(result.error)
        self.assertTrue(result.cancelled)
        self.assertEqual(len(cancel_requested_at), 1)
        self.assertLess(time.monotonic() - cancel_requested_at[0], 8)
        self.assertFalse(self.process_is_running(self.wait_for_pid(pidfile)))

    def test_large_stdout_and_stderr_are_drained_and_bounded(self):
        result = self.run_code(
            "import sys\n"
            "sys.stdout.write('x' * 200000)\n"
            "sys.stdout.flush()\n"
            "sys.stderr.write('y' * 200000)\n"
            "sys.stderr.flush()\n",
            max_output=128)
        self.assert_ok(result)
        self.assertEqual(result.stdout, "x" * 128)
        self.assertEqual(result.stderr, "y" * 128)
        self.assertTrue(result.stdout_truncated)
        self.assertTrue(result.stderr_truncated)

    def test_removed_writable_root_loses_its_acl_grant(self):
        old_root = self.root / "old"
        new_root = self.root / "new"
        cwd = self.root / "cwd"
        old_root.mkdir()
        new_root.mkdir()
        cwd.mkdir()
        self.assert_ok(self.run_code(
            "print('old')", cwd=str(cwd), writable_roots=[str(old_root)]))
        self.assert_ok(self.run_code(
            "print('new')", cwd=str(cwd), writable_roots=[str(new_root)]))
        target = old_root / "late.txt"
        result = self.run_code(
            "from pathlib import Path\n"
            "try:\n"
            f"    Path({str(target)!r}).write_text('bad', encoding='utf-8')\n"
            "except OSError as exc:\n"
            "    print('blocked', exc.errno)\n",
            cwd=str(cwd), writable_roots=[str(new_root)])
        self.assert_ok(result)
        self.assertTrue(result.stdout.startswith("blocked"))
        self.assertFalse(target.exists())

    def test_missing_writable_root_fails_closed(self):
        target = self.root / "must-not-run.txt"
        result = self.run_code(
            f"from pathlib import Path; Path({str(target)!r}).write_text('bad')",
            writable_roots=[str(self.root / "missing")])
        self.assertIsNotNone(result.error)
        self.assertFalse(target.exists())

    def test_project_local_venv_runtime_is_allowed_in_a_writable_project(self):
        runtime = self.root / ".venv" / "Scripts"
        runtime.mkdir(parents=True)
        interpreter = runtime / "python.exe"
        interpreter.write_bytes(b"")
        with mock.patch.object(python_sandbox, "_resolve_interpreter",
                               return_value=str(interpreter)), \
                mock.patch.object(python_sandbox, "_configure_access",
                                  side_effect=AssertionError("ACL setup reached")):
            result = self.run_code("print('not reached')")
        self.assertIn("ACL setup reached", result.error)

    def test_writable_root_cannot_contain_runtime_or_acl_state(self):
        target = self.root / "must-not-run.txt"
        code = f"from pathlib import Path; Path({str(target)!r}).write_text('bad')"
        runtime = self.root / "runtime"
        runtime.mkdir()
        interpreter = runtime / "python.exe"
        interpreter.write_bytes(b"")
        with mock.patch.object(python_sandbox, "_resolve_interpreter",
                               return_value=str(interpreter)), \
                mock.patch.object(python_sandbox, "_configure_access",
                                  side_effect=AssertionError("ACL setup ran too early")):
            runtime_result = self.run_code(code)
        self.assertIn("cannot contain", runtime_result.error)
        self.assertFalse(target.exists())

        state_path = self.root / "sandbox-state.json"
        with mock.patch.object(python_sandbox, "_profile_identity",
                               return_value=("Kestrel.Test", "S-1-test", str(state_path))), \
                mock.patch.object(python_sandbox, "_configure_access",
                                  side_effect=AssertionError("ACL setup ran too early")):
            state_result = self.run_code(code)
        self.assertIn("cannot contain", state_result.error)
        self.assertFalse(target.exists())

    def test_cancellation_during_setup_prevents_process_creation(self):
        event = threading.Event()
        run_dir = self.root / "run"
        run_dir.mkdir()

        def prepare(*args, **kwargs):
            event.set()
            return str(run_dir)

        with mock.patch.object(python_sandbox, "_ensure_profile",
                               return_value="S-1-test"), \
                mock.patch.object(python_sandbox, "_configure_access"), \
                mock.patch.object(python_sandbox, "_prepare_run_dir",
                                  side_effect=prepare), \
                mock.patch.object(python_sandbox, "_create_job",
                                  side_effect=AssertionError("job created")), \
                mock.patch.object(python_sandbox, "_remove_run_tree"):
            result = python_sandbox.run_python(
                "print('must not run')", cwd=str(PROJECT),
                writable_roots=[str(self.root)], stop_event=event)
        self.assertIsNone(result.error)
        self.assertTrue(result.cancelled)

    def test_cancellation_during_acl_setup_never_creates_a_job(self):
        with mock.patch.object(python_sandbox, "_ensure_profile",
                               return_value="S-1-test"), \
                mock.patch.object(python_sandbox, "_configure_access",
                                  side_effect=InterruptedError("cancelled")), \
                mock.patch.object(python_sandbox, "_create_job",
                                  side_effect=AssertionError("job created")):
            result = self.run_code("print('must not run')")
        self.assertIsNone(result.error)
        self.assertTrue(result.cancelled)

    def test_suspended_process_is_resumed_exactly_once(self):
        kernel = python_sandbox._apis()[0]
        original_resume = kernel.ResumeThread
        calls = []

        def resume_once(thread):
            if calls:
                raise AssertionError("ResumeThread was called more than once")
            calls.append(thread)
            return original_resume(thread)

        with mock.patch.object(kernel, "ResumeThread", side_effect=resume_once):
            result = self.run_code("print('ok')")

        self.assert_ok(result)
        self.assertEqual(result.stdout, "ok")
        self.assertEqual(len(calls), 1)

    def test_process_verification_rejects_medium_integrity_token(self):
        marker = self.root / "must-not-run.txt"
        code = (
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')")
        with mock.patch.object(
                python_sandbox, "_token_integrity_below_medium", return_value=False):
            result = self.run_code(code)
        self.assertIsNotNone(result.error)
        self.assertIn("integrity", result.error.lower())
        self.assertFalse(marker.exists())

    def test_cancellation_during_process_verification_prevents_user_code(self):
        marker = self.root / "must-not-run.txt"
        event = threading.Event()
        original_verify = python_sandbox._verify_process

        def verify_and_cancel(process, job):
            verified = original_verify(process, job)
            event.set()
            deadline = time.monotonic() + 2
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            return verified

        code = (
            "from pathlib import Path\n"
            "import time\n"
            f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n"
            "time.sleep(5)"
        )
        with mock.patch.object(python_sandbox, "_verify_process",
                               side_effect=verify_and_cancel):
            result = self.run_code(code, stop_event=event)
        self.assertIsNone(result.error)
        self.assertTrue(result.cancelled)
        self.assertFalse(marker.exists())

    def test_setup_deadline_expiring_during_verification_prevents_resume(self):
        marker = self.root / "must-not-run.txt"
        original_verify = python_sandbox._verify_process
        original_monotonic = time.monotonic
        expired = False

        def clock():
            if expired:
                return original_monotonic() + python_sandbox._SETUP_TIMEOUT + 1
            return original_monotonic()

        def verify_and_expire(process, job):
            nonlocal expired
            verified = original_verify(process, job)
            expired = True
            return verified

        kernel = python_sandbox._apis()[0]
        code = (
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')"
        )
        with mock.patch.object(python_sandbox.time, "monotonic", side_effect=clock), \
                mock.patch.object(
                    python_sandbox, "_verify_process",
                    side_effect=verify_and_expire), \
                mock.patch.object(kernel, "ResumeThread", wraps=kernel.ResumeThread) as resume:
            result = self.run_code(code)
        self.assertIn("time limit", result.error or "")
        resume.assert_not_called()
        self.assertFalse(marker.exists())

    def test_cancellation_after_lock_acquisition_releases_it(self):
        event = threading.Event()

        class CancellingLock:
            released = False

            def acquire(self, timeout):
                event.set()
                return True

            def release(self):
                self.released = True

        lock = CancellingLock()
        with mock.patch.object(python_sandbox, "_RUN_LOCK", lock):
            self.assertEqual(
                python_sandbox._acquire_run_lock(event), "cancelled")
        self.assertTrue(lock.released)

    def test_acl_setup_failure_fails_closed(self):
        target = self.root / "must-not-run.txt"
        digest = hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:12]
        with tempfile.TemporaryDirectory() as state_dir, \
                mock.patch.object(
                    python_sandbox, "_profile_identity",
                    return_value=(f"Kestrel.PythonSandbox.Tests.Failure.{digest}",
                                  "S-1-test", str(Path(state_dir) / "state.json"))), \
                mock.patch.object(python_sandbox, "_set_root_access",
                                  side_effect=OSError("grant denied")), \
                mock.patch.object(python_sandbox, "_save_state", return_value=None):
            result = self.run_code(
                f"from pathlib import Path; Path({str(target)!r}).write_text('bad')")
        self.assertIn("grant denied", result.error)
        self.assertFalse(target.exists())

    def test_frozen_launcher_is_rejected_instead_of_used_as_python(self):
        target = self.root / "must-not-run.txt"
        with mock.patch.object(python_sandbox.sys, "executable",
                               str(PROJECT / "Kestrel.exe")):
            result = self.run_code(
                f"from pathlib import Path; Path({str(target)!r}).write_text('bad')")
        self.assertIsNotNone(result.error)
        self.assertIn("real Python interpreter", result.error)
        self.assertFalse(target.exists())

    @staticmethod
    def wait_for_pid(pidfile):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if pidfile.exists():
                return int(pidfile.read_text(encoding="ascii"))
            time.sleep(0.02)
        raise AssertionError("child did not write its PID file")

    @staticmethod
    def process_is_running(pid):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259
        finally:
            kernel32.CloseHandle(handle)


class MutationBrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.kernel = mock.Mock()
        self.kernel.WriteFile.return_value = 1

    def broker(self):
        broker = python_sandbox._MutationBroker(
            1, 2, str(self.root), [str(self.root)])
        broker.snapshot_file = mock.Mock(return_value=None)
        return broker

    @staticmethod
    def request(path):
        return json.dumps({"path": str(path)}, separators=(",", ":")).encode("ascii")

    def test_target_limit_overflow_invalidates_tracking(self):
        broker = self.broker()
        with mock.patch.object(python_sandbox, "_RECEIPT_MAX_TARGETS", 1):
            broker._handle(self.kernel, self.request(self.root / "first.txt"))
            broker._handle(self.kernel, self.request(self.root / "second.txt"))
        self.assertIsNotNone(broker.error)
        self.assertEqual(self.kernel.WriteFile.call_args.args[1], b"0")

    def test_request_limit_overflow_invalidates_tracking(self):
        broker = self.broker()
        path = self.root / "report.txt"
        with mock.patch.object(python_sandbox, "_RECEIPT_MAX_REQUESTS", 1):
            broker._handle(self.kernel, self.request(path))
            broker._handle(self.kernel, self.request(path))
        self.assertIsNotNone(broker.error)
        self.assertEqual(self.kernel.WriteFile.call_args.args[1], b"0")

    def test_snapshot_failure_invalidates_tracking(self):
        broker = self.broker()
        broker.snapshot_file.side_effect = OSError("cannot snapshot")
        broker._handle(self.kernel, self.request(self.root / "report.txt"))
        self.assertIsNotNone(broker.error)
        self.assertEqual(self.kernel.WriteFile.call_args.args[1], b"0")


class HostWorkBoundTests(unittest.TestCase):
    def test_protected_scan_honors_deadline(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("config.CONFIG_PATH", str(Path(directory) / "config.json")), \
                mock.patch("config.memory_dir", return_value=str(Path(directory) / "memory")), \
                self.assertRaisesRegex(TimeoutError, "time limit"):
            python_sandbox._protected_paths(
                [directory], str(Path(directory) / "state.json"),
                deadline=time.monotonic() - 1)

    def test_protected_scan_honors_cancellation_during_walk(self):
        with tempfile.TemporaryDirectory() as directory:
            event = threading.Event()

            def cancelled_walk(*args, **kwargs):
                event.set()
                yield directory, [], []

            with mock.patch("config.CONFIG_PATH", str(Path(directory) / "config.json")), \
                    mock.patch("config.memory_dir", return_value=str(Path(directory) / "memory")), \
                    mock.patch.object(python_sandbox.os, "walk", side_effect=cancelled_walk), \
                    self.assertRaises(InterruptedError):
                python_sandbox._protected_paths(
                    [directory], str(Path(directory) / "state.json"),
                    stop_event=event)

    def test_acl_command_deadline_prevents_launch(self):
        with mock.patch.object(python_sandbox.subprocess, "Popen") as popen, \
                self.assertRaisesRegex(TimeoutError, "time limit"):
            python_sandbox._run_icacls("unused", [], deadline=time.monotonic() - 1)
        popen.assert_not_called()

    def test_acl_configuration_honors_deadline_before_state_write(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(python_sandbox, "_save_state") as save, \
                self.assertRaisesRegex(TimeoutError, "time limit"):
            python_sandbox._configure_access(
                "S-1-test", [os.path.normcase(directory)], [],
                str(Path(directory) / "state.json"), os.path.normcase(directory),
                deadline=time.monotonic() - 1)
        save.assert_not_called()


class AclLifecycleTests(unittest.TestCase):
    def test_private_run_directory_cleans_up_after_grant_failure(self):
        for failure in (OSError("grant failed"), InterruptedError()):
            with self.subTest(failure=type(failure).__name__), \
                    tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "PythonSandboxRuns" / "Test"
                calls = []
                grant_count = 0

                def fake_icacls(path, arguments, **kwargs):
                    nonlocal grant_count
                    calls.append((path, arguments))
                    if arguments[0] == "/grant":
                        grant_count += 1
                        if grant_count == 2:
                            raise failure
                    return True

                with mock.patch.object(python_sandbox, "_run_icacls",
                                       side_effect=fake_icacls), \
                        self.assertRaises(type(failure)):
                    python_sandbox._prepare_run_dir(
                        str(Path(directory) / "state.json"),
                        "Kestrel.PythonSandbox.Test", "S-1-test")
                self.assertEqual(list(root.iterdir()), [])
                self.assertTrue(any(
                    "/remove" in arguments for _, arguments in calls))

    def test_run_lock_wait_honors_setup_deadline(self):
        clock = [0.0]
        timeouts = []

        class BusyLock:
            def acquire(self, timeout):
                timeouts.append(timeout)
                clock[0] = 121.0
                return False

        with mock.patch.object(python_sandbox, "_RUN_LOCK", BusyLock()), \
                mock.patch.object(python_sandbox.time, "monotonic",
                                  side_effect=lambda: clock[0]):
            self.assertEqual(
                python_sandbox._acquire_run_lock(None, deadline=120.0),
                "timed_out")
        self.assertEqual(timeouts, [0.1])

    def test_mutex_wait_honors_setup_deadline(self):
        clock = [0.0]
        kernel = mock.Mock()

        def wait(handle, timeout):
            clock[0] = 121.0
            return python_sandbox._WAIT_TIMEOUT

        kernel.WaitForSingleObject.side_effect = wait
        with mock.patch.object(python_sandbox, "_apis",
                               return_value=(kernel, None, None)), \
                mock.patch.object(python_sandbox.time, "monotonic",
                                  side_effect=lambda: clock[0]):
            self.assertEqual(
                python_sandbox._acquire_mutex(object(), None, deadline=120.0),
                "timed_out")
        kernel.WaitForSingleObject.assert_called_once()

    def test_profile_identity_is_bound_to_state_location(self):
        with tempfile.TemporaryDirectory() as first, \
                tempfile.TemporaryDirectory() as second, \
                mock.patch.object(python_sandbox, "_current_user_sid",
                                  return_value="S-1-test"):
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": first}):
                original = python_sandbox._profile_identity()
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": second}):
                moved = python_sandbox._profile_identity()
        self.assertEqual(original[1], moved[1])
        self.assertNotEqual(original[0], moved[0])
        self.assertNotEqual(original[2], moved[2])
        self.assertEqual(Path(original[2]).parent, Path(first) / "Kestrel")
        self.assertEqual(Path(moved[2]).parent, Path(second) / "Kestrel")

    def test_existing_profile_without_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            userenv = mock.Mock()
            userenv.CreateAppContainerProfile.return_value = (
                0x80070000 | python_sandbox._ERROR_ALREADY_EXISTS)
            with mock.patch.object(python_sandbox, "_apis",
                                   return_value=(mock.Mock(), mock.Mock(), userenv)), \
                    self.assertRaisesRegex(OSError, "state"):
                python_sandbox._ensure_profile(
                    "Kestrel.PythonSandbox.Test",
                    str(Path(directory) / "missing.json"))
        userenv.DeriveAppContainerSidFromAppContainerName.assert_not_called()

    def test_retired_protected_grants_are_revoked_without_following_junctions(self):
        for reparse in (False, True):
            with self.subTest(reparse=reparse), \
                    tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root = base / "root"
                retired = root / "retired"
                retired.mkdir(parents=True)
                state_path = base / "state.json"
                sid = "S-1-test"
                state = python_sandbox._empty_state(sid)
                state["writable_roots"] = [os.path.normcase(str(root))]
                state["protected_paths"] = {os.path.normcase(str(retired)): True}
                python_sandbox._save_state(str(state_path), state)
                with mock.patch.object(python_sandbox, "_protected_paths",
                                       return_value=[]), \
                        mock.patch.object(python_sandbox, "_root_access_matches",
                                          return_value=True), \
                        mock.patch.object(python_sandbox, "_is_reparse_point",
                                          return_value=reparse), \
                        mock.patch.object(python_sandbox, "_revoke_access",
                                          return_value=True) as revoke, \
                        mock.patch.object(python_sandbox, "_protect_path") as protect, \
                        mock.patch.object(python_sandbox, "_save_state"), \
                        mock.patch.object(python_sandbox, "_run_icacls",
                                          return_value=True):
                    python_sandbox._configure_access(
                        sid, [os.path.normcase(str(root))], [], str(state_path),
                        os.path.normcase(str(root)))
                expected = os.path.normcase(str(retired))
                if reparse:
                    protect.assert_called_once_with(expected, sid, False)
                    revoke.assert_not_called()
                else:
                    revoke.assert_called_once_with(
                        expected, sid, stop_event=None, deadline=None,
                        clear_label=False)
                    protect.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Windows ACL migration test")
class AccessControlTests(unittest.TestCase):
    @staticmethod
    def derive_sid(name):
        _, advapi, userenv = python_sandbox._apis()
        sid = ctypes.c_void_p()
        code = userenv.DeriveAppContainerSidFromAppContainerName(
            name, ctypes.byref(sid))
        if code:
            raise AssertionError(f"cannot derive test SID: {code & 0xffffffff:#x}")
        try:
            return python_sandbox._sid_string(sid)
        finally:
            advapi.FreeSid(sid)

    @staticmethod
    def acl_lines(path):
        icacls = Path(os.environ["SystemRoot"]) / "System32" / "icacls.exe"
        result = subprocess.run(
            [str(icacls), str(path)], check=True, capture_output=True, text=True)
        return sorted(line.strip() for line in result.stdout.splitlines() if line.strip())

    @staticmethod
    def access_aces(path, sid):
        import struct

        kernel, advapi, _ = python_sandbox._apis()
        descriptor = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        code = advapi.GetNamedSecurityInfoW(
            str(path), python_sandbox._SE_FILE_OBJECT,
            python_sandbox._DACL_SECURITY_INFORMATION,
            None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
        if code:
            raise AssertionError(f"cannot read ACL for {path}: {code}")
        try:
            if not dacl.value:
                return []
            acl_size = struct.unpack_from("<H", ctypes.string_at(dacl, 8), 2)[0]
            raw = ctypes.string_at(dacl, acl_size)
            ace_count = struct.unpack_from("<H", raw, 4)[0]
            aces = []
            offset = 8
            for _ in range(ace_count):
                ace_size = struct.unpack_from("<H", raw, offset + 2)[0]
                if raw[offset] in (
                        python_sandbox._ACCESS_ALLOWED_ACE,
                        python_sandbox._ACCESS_DENIED_ACE):
                    ace_sid = python_sandbox._sid_string(dacl.value + offset + 8)
                    if ace_sid == sid:
                        aces.append((
                            raw[offset], raw[offset + 1],
                            struct.unpack_from("<I", raw, offset + 4)[0]))
                offset += ace_size
            return aces
        finally:
            kernel.LocalFree(descriptor)

    @staticmethod
    def label_aces(path):
        import struct

        kernel, advapi, _ = python_sandbox._apis()
        descriptor = ctypes.c_void_p()
        sacl = ctypes.c_void_p()
        code = advapi.GetNamedSecurityInfoW(
            str(path), python_sandbox._SE_FILE_OBJECT, 0x10,
            None, None, None, ctypes.byref(sacl), ctypes.byref(descriptor))
        if code:
            raise AssertionError(f"cannot read label for {path}: {code}")
        try:
            if not sacl.value:
                return []
            header = ctypes.string_at(sacl, 8)
            acl_size = struct.unpack_from("<H", header, 2)[0]
            ace_count = struct.unpack_from("<H", header, 4)[0]
            raw = ctypes.string_at(sacl, acl_size)
            labels = []
            offset = 8
            for _ in range(ace_count):
                ace_size = struct.unpack_from("<H", raw, offset + 2)[0]
                if raw[offset] == 0x11:
                    labels.append((
                        python_sandbox._sid_string(sacl.value + offset + 8),
                        raw[offset + 1],
                        struct.unpack_from("<I", raw, offset + 4)[0]))
                offset += ace_size
            return labels
        finally:
            kernel.LocalFree(descriptor)

    @staticmethod
    def set_label(path, sid="S-1-16-8192", mask=1, flags=3):
        kernel, advapi, _ = python_sandbox._apis()
        sid_pointer = ctypes.c_void_p()
        label_acl = ctypes.c_void_p()
        try:
            if not advapi.ConvertStringSidToSidW(sid, ctypes.byref(sid_pointer)):
                raise AssertionError("cannot convert label SID")
            label_acl = kernel.LocalAlloc(python_sandbox._LMEM_FIXED, 256)
            if not label_acl:
                raise AssertionError("cannot allocate label ACL")
            if not advapi.InitializeAcl(label_acl, 256, python_sandbox._ACL_REVISION):
                raise AssertionError("cannot initialize label ACL")
            advapi.AddMandatoryAce.argtypes = [
                ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD,
                ctypes.wintypes.DWORD, ctypes.c_void_p]
            advapi.AddMandatoryAce.restype = ctypes.wintypes.BOOL
            if not advapi.AddMandatoryAce(label_acl, 2, flags, mask, sid_pointer):
                raise AssertionError("cannot add mandatory label")
            code = advapi.SetNamedSecurityInfoW(
                str(path), python_sandbox._SE_FILE_OBJECT, 0x10,
                None, None, None, label_acl)
            if code:
                raise AssertionError(f"cannot set label for {path}: {code}")
        finally:
            if label_acl:
                kernel.LocalFree(label_acl)
            if sid_pointer:
                kernel.LocalFree(sid_pointer)

    @staticmethod
    def non_target_acl_aces(path, sid):
        import struct

        kernel, advapi, _ = python_sandbox._apis()
        descriptor = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        code = advapi.GetNamedSecurityInfoW(
            str(path), python_sandbox._SE_FILE_OBJECT,
            python_sandbox._DACL_SECURITY_INFORMATION,
            None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
        if code:
            raise AssertionError(f"cannot read ACL for {path}: {code}")
        try:
            if not dacl.value:
                return []
            acl_size = struct.unpack_from("<H", ctypes.string_at(dacl, 8), 2)[0]
            raw = ctypes.string_at(dacl, acl_size)
            ace_count = struct.unpack_from("<H", raw, 4)[0]
            aces = []
            offset = 8
            for _ in range(ace_count):
                ace_size = struct.unpack_from("<H", raw, offset + 2)[0]
                if raw[offset] in (
                        python_sandbox._ACCESS_ALLOWED_ACE,
                        python_sandbox._ACCESS_DENIED_ACE):
                    ace_sid = python_sandbox._sid_string(dacl.value + offset + 8)
                    if ace_sid != sid:
                        aces.append(raw[offset:offset + ace_size])
                else:
                    aces.append(raw[offset:offset + ace_size])
                offset += ace_size
            return sorted(aces)
        finally:
            kernel.LocalFree(descriptor)

    def test_retired_parent_cannot_leave_pending_child_with_only_inherited_access(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            nested = root / "nested"
            nested.mkdir(parents=True)
            state_path = base / "state.json"
            root_key = os.path.normcase(str(root))
            nested_key = os.path.normcase(str(nested))
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.INH.{uuid.uuid4().hex[:12]}")
            state = python_sandbox._empty_state(sid)
            state["pending_read_roots"] = [root_key, nested_key]
            python_sandbox._set_root_access(
                root_key, sid, python_sandbox._GENERIC_READ | python_sandbox._GENERIC_EXECUTE)
            python_sandbox._save_state(str(state_path), state)
            with mock.patch.object(
                    python_sandbox, "_set_root_access",
                    wraps=python_sandbox._set_root_access) as set_access:
                python_sandbox._configure_access(
                    sid, [], [nested_key], str(state_path), nested_key)
            aces = self.access_aces(nested, sid)
            self.assertTrue(any(
                flags & 0x0B == 0x0B and not flags & 0x10
                for _, flags, _ in aces))
            self.assertIn(nested_key, [
                call.args[0] for call in set_access.call_args_list])
            state = python_sandbox._load_state(str(state_path), sid)
            self.assertEqual(state["read_roots"], [nested_key])
            self.assertEqual(state["pending_read_roots"], [])

    def test_intermediate_state_preserves_retired_protected_path_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            retired = root / "retired"
            readable = base / "readable"
            retired.mkdir(parents=True)
            readable.mkdir()
            state_path = base / "state.json"
            root_key = os.path.normcase(str(root))
            retired_key = os.path.normcase(str(retired))
            readable_key = os.path.normcase(str(readable))
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.CRASH.{uuid.uuid4().hex[:12]}")
            state = python_sandbox._empty_state(sid)
            state["writable_roots"] = [root_key]
            state["protected_paths"] = {retired_key: True}
            python_sandbox._protect_path(retired_key, sid, True)
            python_sandbox._save_state(str(state_path), state)
            original_apply = python_sandbox._apply_access_changes

            def interrupt_grants(changes, *args, **kwargs):
                if changes:
                    raise InterruptedError("simulated setup interruption")
                return original_apply(changes, *args, **kwargs)

            with mock.patch.object(python_sandbox, "_protected_paths",
                                   return_value=[]), \
                    mock.patch.object(
                        python_sandbox, "_apply_access_changes",
                        side_effect=interrupt_grants), \
                    self.assertRaisesRegex(
                        InterruptedError, "simulated setup interruption"):
                python_sandbox._configure_access(
                    sid, [root_key], [readable_key],
                    str(state_path), root_key)
            interrupted = python_sandbox._load_state(str(state_path), sid)
            self.assertEqual(
                interrupted["protected_paths"], {retired_key: True})
            with mock.patch.object(python_sandbox, "_protected_paths",
                                   return_value=[]):
                python_sandbox._configure_access(
                    sid, [root_key], [readable_key],
                    str(state_path), root_key)
            self.assertEqual(self.access_aces(retired, sid), [])
            self.assertTrue(self.access_aces(readable, sid))

    def test_interruption_after_new_protection_is_recovered_when_root_retires(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            retired = root / "retired"
            retired.mkdir(parents=True)
            state_path = base / "state.json"
            root_key = os.path.normcase(str(root))
            retired_key = os.path.normcase(str(retired))
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.NEWPROT.{uuid.uuid4().hex[:12]}")
            original_protect = python_sandbox._protect_path

            def protect_then_interrupt(path, sid_value, allow_read):
                original_protect(path, sid_value, allow_read)
                if os.path.normcase(path) == retired_key:
                    raise InterruptedError("simulated interruption after protection")

            with mock.patch.object(python_sandbox, "_protected_paths",
                                   return_value=[(str(retired), True)]), \
                    mock.patch.object(python_sandbox, "_protect_path",
                                      side_effect=protect_then_interrupt), \
                    self.assertRaisesRegex(
                        InterruptedError, "simulated interruption after protection"):
                python_sandbox._configure_access(
                    sid, [root_key], [], str(state_path), root_key)
            with mock.patch.object(python_sandbox, "_protected_paths",
                                   return_value=[]):
                python_sandbox._configure_access(
                    sid, [], [], str(state_path), root_key)
            self.assertEqual(self.access_aces(retired, sid), [])
            self.assertEqual(self.access_aces(root, sid), [])

    def test_active_root_with_excessive_package_group_access_fails_closed(self):
        for access_mask in (
                python_sandbox._GENERIC_READ | python_sandbox._GENERIC_EXECUTE,
                python_sandbox._MODIFY_ACCESS):
            with self.subTest(access_mask=hex(access_mask)), \
                    tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "root"
                root.mkdir()
                root_key = os.path.normcase(str(root))
                state_path = Path(directory) / "state.json"
                sid = self.derive_sid(
                    f"Kestrel.PythonSandbox.Tests.GROUP.{uuid.uuid4().hex[:12]}")
                python_sandbox._set_root_access(root_key, sid, access_mask)
                self.assertTrue(python_sandbox._root_access_matches(
                    root_key, sid, access_mask))
                self.assertTrue(python_sandbox._run_icacls(
                    root_key, ["/grant", "*S-1-15-2-1:(OI)(CI)(F)"]))
                host_aces = self.non_target_acl_aces(root_key, sid)
                state = python_sandbox._empty_state(sid)
                if access_mask == python_sandbox._MODIFY_ACCESS:
                    state["writable_roots"] = [root_key]
                    writable, readable = [root_key], []
                else:
                    state["read_roots"] = [root_key]
                    writable, readable = [], [root_key]
                python_sandbox._save_state(str(state_path), state)
                with mock.patch.object(python_sandbox, "_protected_paths",
                                       return_value=[]), \
                        self.assertRaisesRegex(OSError, "ALL_APPLICATION_PACKAGES"):
                    python_sandbox._configure_access(
                        sid, writable, readable, str(state_path), root_key)
                self.assertFalse(python_sandbox._root_access_matches(
                    root_key, sid, access_mask))
                self.assertEqual(
                    self.non_target_acl_aces(root_key, sid), host_aces)

    def test_token_integrity_gate_accepts_only_below_medium(self):
        _, advapi, _ = python_sandbox._apis()
        original = advapi.GetTokenInformation
        for integrity_sid, expected in (
                ("S-1-16-4096", True),
                ("S-1-16-8192", False),
                ("S-1-16-12288", False)):
            with self.subTest(integrity_sid=integrity_sid):
                sid_pointer = ctypes.c_void_p()
                self.assertTrue(advapi.ConvertStringSidToSidW(
                    integrity_sid, ctypes.byref(sid_pointer)))
                try:
                    label = python_sandbox._TOKEN_MANDATORY_LABEL()
                    label.Label.Sid = sid_pointer.value
                    size = ctypes.sizeof(label)

                    def get_token_information(
                            token, info_class, buffer, length, return_length,
                            label=label, size=size):
                        if info_class != python_sandbox._TOKEN_INTEGRITY_LEVEL:
                            return original(
                                token, info_class, buffer, length, return_length)
                        if buffer is None:
                            ctypes.set_last_error(
                                python_sandbox._ERROR_INSUFFICIENT_BUFFER)
                            return False
                        ctypes.memmove(buffer, ctypes.byref(label), size)
                        ctypes.cast(
                            return_length, ctypes.POINTER(ctypes.c_ulong)
                        ).contents.value = size
                        return True

                    with mock.patch.object(
                            advapi, "GetTokenInformation",
                            side_effect=get_token_information):
                        self.assertEqual(
                            python_sandbox._token_integrity_below_medium(None),
                            expected)
                finally:
                    advapi.FreeSid(sid_pointer)

    def test_inherited_file_label_is_safe_without_inheritance_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            child = root / "child.txt"
            root.mkdir()
            child.write_text("data", encoding="utf-8")
            self.set_label(root)
            self.assertEqual(
                self.label_aces(child), [("S-1-16-8192", 0x10, 1)])
            self.assertEqual(
                python_sandbox._root_label_state(os.path.normcase(str(child))),
                "inherited")

    def test_read_root_applies_inheritable_medium_no_write_up_label(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            child = root / "child"
            child.mkdir(parents=True)
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.LABEL.{uuid.uuid4().hex[:12]}")
            python_sandbox._set_root_access(
                os.path.normcase(str(root)), sid,
                python_sandbox._GENERIC_READ | python_sandbox._GENERIC_EXECUTE)
            self.assertEqual(self.label_aces(root), [("S-1-16-8192", 3, 1)])
            self.assertIn(("S-1-16-8192", 0x13, 1), self.label_aces(child))
            self.assertTrue(python_sandbox._root_access_matches(
                os.path.normcase(str(root)), sid,
                python_sandbox._GENERIC_READ | python_sandbox._GENERIC_EXECUTE))

    def test_read_root_has_explicit_inheritable_write_deny(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            root_key = os.path.normcase(str(root))
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.DENY.{uuid.uuid4().hex[:12]}")
            python_sandbox._set_root_access(
                root_key, sid,
                python_sandbox._GENERIC_READ | python_sandbox._GENERIC_EXECUTE)
            aces = self.access_aces(root, sid)
            self.assertIn((
                python_sandbox._ACCESS_ALLOWED_ACE, 0x0B,
                python_sandbox._GENERIC_READ | python_sandbox._GENERIC_EXECUTE,
            ), aces)
            self.assertIn((
                python_sandbox._ACCESS_DENIED_ACE, 0x0B,
                python_sandbox._WRITE_DENY_ACCESS,
            ), aces)
            self.assertTrue(python_sandbox._root_access_matches(
                root_key, sid,
                python_sandbox._GENERIC_READ | python_sandbox._GENERIC_EXECUTE))

    def test_read_root_file_accepts_normalized_direct_write_deny(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "read-only.txt"
            target.write_text("data", encoding="utf-8")
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.FILEDENY.{uuid.uuid4().hex[:12]}")
            python_sandbox._set_root_access(
                os.path.normcase(str(target)), sid,
                python_sandbox._GENERIC_READ | python_sandbox._GENERIC_EXECUTE)
            file_write_rights = (
                0x00000002 | 0x00000004 | 0x00000010 | 0x00000040
                | 0x00000100 | 0x00010000 | 0x00040000 | 0x00080000)
            self.assertTrue(any(
                ace_type == python_sandbox._ACCESS_DENIED_ACE
                and mask & file_write_rights == file_write_rights
                for ace_type, _, mask in self.access_aces(target, sid)))
            self.assertTrue(python_sandbox._root_access_matches(
                os.path.normcase(str(target)), sid,
                python_sandbox._GENERIC_READ | python_sandbox._GENERIC_EXECUTE))

    def test_read_root_scan_reports_direct_write_and_unsafe_group_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            direct = root / "direct.txt"
            grouped = root / "grouped"
            root.mkdir()
            direct.write_text("data", encoding="utf-8")
            grouped.mkdir()
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.SCAN.{uuid.uuid4().hex[:12]}")
            python_sandbox._set_root_access(
                os.path.normcase(str(direct)), sid, python_sandbox._MODIFY_ACCESS)
            self.assertTrue(python_sandbox._run_icacls(
                os.path.normcase(str(grouped)),
                ["/grant", "*S-1-15-2-1:(OI)(CI)(F)"]))
            findings = python_sandbox._scan_read_root(root, sid)
            self.assertEqual(
                {(item["path"], item["kind"]) for item in findings},
                {(os.path.normcase(str(direct)), "write"),
                 (os.path.normcase(str(grouped)), "group")})

    def test_read_root_configuration_remediates_direct_descendant_write_allow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            child = root / "child"
            child.mkdir(parents=True)
            root_key = os.path.normcase(str(root))
            child_key = os.path.normcase(str(child))
            state_path = Path(directory) / "state.json"
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.REMEDIATE.{uuid.uuid4().hex[:12]}")
            python_sandbox._set_root_access(child_key, sid, python_sandbox._MODIFY_ACCESS)
            host_aces = self.non_target_acl_aces(child, sid)
            with mock.patch.object(python_sandbox, "_protected_paths", return_value=[]):
                python_sandbox._configure_access(
                    sid, [], [root_key], str(state_path), root_key)
            aces = self.access_aces(child, sid)
            self.assertFalse(any(
                ace_type == python_sandbox._ACCESS_ALLOWED_ACE
                and mask & python_sandbox._WRITE_ACCESS
                for ace_type, _, mask in aces))
            self.assertTrue(any(
                ace_type == python_sandbox._ACCESS_DENIED_ACE
                and mask == python_sandbox._WRITE_DENY_ACCESS
                for ace_type, _, mask in aces))
            self.assertEqual(self.non_target_acl_aces(child, sid), host_aces)

    def test_read_root_configuration_fails_closed_on_unsafe_descendant_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            child = root / "child"
            child.mkdir(parents=True)
            root_key = os.path.normcase(str(root))
            child_key = os.path.normcase(str(child))
            state_path = Path(directory) / "state.json"
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.GROUPDESC.{uuid.uuid4().hex[:12]}")
            self.assertTrue(python_sandbox._run_icacls(
                child_key, ["/grant", "*S-1-15-2-1:(OI)(CI)(F)"]))
            host_aces = self.non_target_acl_aces(child, sid)
            with mock.patch.object(python_sandbox, "_protected_paths", return_value=[]), \
                    self.assertRaisesRegex(OSError, "ALL_APPLICATION_PACKAGES"):
                python_sandbox._configure_access(
                    sid, [], [root_key], str(state_path), root_key)
            self.assertEqual(self.non_target_acl_aces(child, sid), host_aces)

    def test_read_root_security_update_combines_dacl_and_label(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.ONEPASS.{uuid.uuid4().hex[:12]}")
            kernel, advapi, userenv = python_sandbox._apis()
            with mock.patch.object(
                    python_sandbox, "_apis", return_value=(kernel, advapi, userenv)), \
                    mock.patch.object(
                        advapi, "SetNamedSecurityInfoW",
                        wraps=advapi.SetNamedSecurityInfoW) as set_security:
                python_sandbox._set_root_access(
                    os.path.normcase(str(root)), sid,
                    python_sandbox._GENERIC_READ | python_sandbox._GENERIC_EXECUTE)
            self.assertEqual(set_security.call_count, 1)
            call = set_security.call_args
            self.assertEqual(
                call.args[2],
                python_sandbox._DACL_SECURITY_INFORMATION
                | python_sandbox._LABEL_SECURITY_INFORMATION)
            self.assertTrue(call.args[5])
            self.assertTrue(call.args[6])

    def test_read_root_revocation_clears_safe_mandatory_label(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            child = root / "child"
            child.mkdir(parents=True)
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.CLEAR.{uuid.uuid4().hex[:12]}")
            self.set_label(root)
            python_sandbox._set_root_access(
                os.path.normcase(str(root)), sid,
                python_sandbox._GENERIC_READ | python_sandbox._GENERIC_EXECUTE)
            self.assertTrue(python_sandbox._revoke_access(
                os.path.normcase(str(root)), sid))
            self.assertEqual(self.label_aces(root), [])
            self.assertEqual(self.label_aces(child), [])

    def test_read_root_refuses_foreign_mandatory_label(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.FOREIGN.{uuid.uuid4().hex[:12]}")
            self.set_label(root, sid="S-1-16-4096")
            before = self.label_aces(root)
            with self.assertRaisesRegex(OSError, "mandatory label"):
                python_sandbox._set_root_access(
                    os.path.normcase(str(root)), sid,
                    python_sandbox._GENERIC_READ | python_sandbox._GENERIC_EXECUTE)
            self.assertEqual(self.label_aces(root), before)
            self.assertEqual(self.access_aces(root, sid), [])

    def test_writable_descendant_of_read_root_is_rejected_before_acl_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            child = root / "child"
            child.mkdir(parents=True)
            root_key = os.path.normcase(str(root))
            child_key = os.path.normcase(str(child))
            state_path = Path(directory) / "state.json"
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.NESTED.{uuid.uuid4().hex[:12]}")
            with mock.patch.object(python_sandbox, "_protected_paths",
                                   return_value=[]), \
                    self.assertRaisesRegex(ValueError, "nested"):
                python_sandbox._configure_access(
                    sid, [child_key], [root_key], str(state_path), root_key)
            self.assertEqual(self.access_aces(root, sid), [])
            self.assertEqual(self.access_aces(child, sid), [])
            self.assertEqual(self.label_aces(root), [])

    def test_preexisting_safe_label_is_not_cleared_when_root_retires(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            root_key = os.path.normcase(str(root))
            state_path = Path(directory) / "state.json"
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.HOSTLABEL.{uuid.uuid4().hex[:12]}")
            self.set_label(root)
            with mock.patch.object(python_sandbox, "_protected_paths",
                                   return_value=[]):
                python_sandbox._configure_access(
                    sid, [], [root_key], str(state_path), root_key)
                python_sandbox._configure_access(
                    sid, [], [], str(state_path), root_key)
            self.assertEqual(self.label_aces(root), [("S-1-16-8192", 3, 1)])
            self.assertEqual(self.access_aces(root, sid), [])

    def test_sandbox_created_label_is_cleared_when_root_retires(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            child = root / "child"
            child.mkdir(parents=True)
            root_key = os.path.normcase(str(root))
            state_path = Path(directory) / "state.json"
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.OWNLABEL.{uuid.uuid4().hex[:12]}")
            with mock.patch.object(python_sandbox, "_protected_paths",
                                   return_value=[]):
                python_sandbox._configure_access(
                    sid, [], [root_key], str(state_path), root_key)
                self.assertEqual(
                    self.label_aces(root), [("S-1-16-8192", 3, 1)])
                python_sandbox._configure_access(
                    sid, [], [], str(state_path), root_key)
            self.assertEqual(self.label_aces(root), [])
            self.assertEqual(self.label_aces(child), [])

    def test_read_root_label_reaches_package_descendant_with_existing_allow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            child = root / "ordinary"
            child.mkdir(parents=True)
            root_key = os.path.normcase(str(root))
            child_key = os.path.normcase(str(child))
            state_path = Path(directory) / "state.json"
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.DESC.{uuid.uuid4().hex[:12]}")
            python_sandbox._set_root_access(
                child_key, sid, python_sandbox._MODIFY_ACCESS)
            host_aces = self.non_target_acl_aces(child_key, sid)
            state = python_sandbox._empty_state(sid)
            state["read_roots"] = [root_key]
            python_sandbox._save_state(str(state_path), state)
            with mock.patch.object(python_sandbox, "_protected_paths",
                                   return_value=[]):
                python_sandbox._configure_access(
                    sid, [], [root_key], str(state_path), root_key)
            self.assertEqual(
                self.label_aces(root), [("S-1-16-8192", 3, 1)])
            self.assertIn(("S-1-16-8192", 0x13, 1), self.label_aces(child))
            self.assertEqual(self.non_target_acl_aces(child_key, sid), host_aces)

    def test_retired_root_with_unsafe_package_group_access_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            root_key = os.path.normcase(str(root))
            state_path = Path(directory) / "state.json"
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.RETIRE.{uuid.uuid4().hex[:12]}")
            python_sandbox._set_root_access(root_key, sid, python_sandbox._MODIFY_ACCESS)
            self.assertTrue(python_sandbox._run_icacls(
                root_key, ["/grant", "*S-1-15-2-1:(OI)(CI)(F)"]))
            host_aces = self.non_target_acl_aces(root_key, sid)
            state = python_sandbox._empty_state(sid)
            state["writable_roots"] = [root_key]
            python_sandbox._save_state(str(state_path), state)
            with mock.patch.object(python_sandbox, "_protected_paths",
                                   return_value=[]), \
                    self.assertRaisesRegex(OSError, "ALL_APPLICATION_PACKAGES"):
                python_sandbox._configure_access(
                    sid, [], [], str(state_path), root_key)
            self.assertEqual(self.non_target_acl_aces(root_key, sid), host_aces)

    def test_root_access_validation_rejects_generic_all_and_delete_child(self):
        import struct

        for target in ("package", "group"):
            for unsafe_mask in (
                    0x10000000,
                    python_sandbox._MODIFY_ACCESS | 0x00000040):
                with self.subTest(target=target, mask=hex(unsafe_mask)), \
                        tempfile.TemporaryDirectory() as directory:
                    root = Path(directory) / "root"
                    root.mkdir()
                    root_key = os.path.normcase(str(root))
                    sid = self.derive_sid(
                        f"Kestrel.PythonSandbox.Tests.MASK.{uuid.uuid4().hex[:12]}")
                    python_sandbox._set_root_access(root_key, sid, python_sandbox._MODIFY_ACCESS)
                    if target == "group":
                        self.assertTrue(python_sandbox._run_icacls(
                            root_key,
                            ["/grant", "*S-1-15-2-1:(OI)(CI)(RX)"]))
                        target_sid = python_sandbox._ALL_APPLICATION_PACKAGES
                    else:
                        target_sid = sid
                    _kernel, advapi, _ = python_sandbox._apis()
                    original = advapi.GetNamedSecurityInfoW

                    def read_with_unsafe_mask(
                            path, object_type, security_info, owner, group, dacl,
                            sacl, descriptor, target_sid=target_sid,
                            unsafe_mask=unsafe_mask):
                        code = original(
                            path, object_type, security_info, owner, group, dacl,
                            sacl, descriptor)
                        if code:
                            return code
                        pointer = dacl._obj.value
                        acl_size = struct.unpack_from("<H", ctypes.string_at(pointer, 8), 2)[0]
                        raw = bytearray(ctypes.string_at(pointer, acl_size))
                        ace_count = struct.unpack_from("<H", raw, 4)[0]
                        offset = 8
                        for _ in range(ace_count):
                            ace_size = struct.unpack_from("<H", raw, offset + 2)[0]
                            if raw[offset] == python_sandbox._ACCESS_ALLOWED_ACE:
                                ace_sid = python_sandbox._sid_string(pointer + offset + 8)
                                if ace_sid == target_sid:
                                    struct.pack_into("<I", raw, offset + 4, unsafe_mask)
                                    ctypes.memmove(pointer, bytes(raw), acl_size)
                                    return code
                            offset += ace_size
                        raise AssertionError("target access ACE was not found")

                    with mock.patch.object(
                            advapi, "GetNamedSecurityInfoW",
                            side_effect=read_with_unsafe_mask):
                        self.assertFalse(python_sandbox._root_access_matches(
                            root_key, sid, python_sandbox._MODIFY_ACCESS))
                        if target == "group":
                            with self.assertRaisesRegex(
                                    OSError, "ALL_APPLICATION_PACKAGES"):
                                python_sandbox._set_root_access(
                                    root_key, sid, python_sandbox._MODIFY_ACCESS)
                        else:
                            python_sandbox._set_root_access(
                                root_key, sid, python_sandbox._MODIFY_ACCESS)
                    if target == "package":
                        self.assertTrue(python_sandbox._root_access_matches(
                            root_key, sid, python_sandbox._MODIFY_ACCESS))

    def test_atomic_state_save_preserves_protected_dacl(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.SD.{uuid.uuid4().hex[:12]}")
            state = python_sandbox._empty_state(sid)
            python_sandbox._save_state(str(state_path), state)
            python_sandbox._protect_path(str(state_path), sid, False)
            self.assertTrue(python_sandbox._path_is_protected(
                str(state_path), sid, False))
            state["writable_roots"] = [os.path.normcase(directory)]
            python_sandbox._save_state(str(state_path), state)
            self.assertTrue(python_sandbox._path_is_protected(
                str(state_path), sid, False))
            self.assertEqual(
                python_sandbox._load_state(str(state_path), sid)["writable_roots"],
                [os.path.normcase(directory)])

    def test_private_run_directory_uses_modify_without_permission_control(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []

            def fake_icacls(path, arguments, **kwargs):
                calls.append((path, arguments))
                return True

            with mock.patch.object(python_sandbox, "_run_icacls",
                                   side_effect=fake_icacls):
                run_dir = python_sandbox._prepare_run_dir(
                    str(Path(directory) / "state.json"),
                    "Kestrel.PythonSandbox.Test", "S-1-test")
            grants = [arguments for path, arguments in calls if path == run_dir]
            self.assertEqual(grants, [["/grant", "*S-1-test:(OI)(CI)(M)"]])
    def test_protected_acl_validation_rejects_raw_generic_write_masks(self):
        import struct

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "protected.txt"
            target.write_text("original", encoding="utf-8")
            sid = "S-1-5-21-1-2-3"
            python_sandbox._protect_path(str(target), sid, allow_read=True)
            _kernel, advapi, _ = python_sandbox._apis()
            original = advapi.GetNamedSecurityInfoW

            for generic_mask in (0x10000000, 0x02000000):
                with self.subTest(mask=generic_mask):
                    def read_with_raw_mask(path, object_type, security_info,
                                           owner, group, dacl, sacl, descriptor,
                                           generic_mask=generic_mask):
                        code = original(path, object_type, security_info,
                                        owner, group, dacl, sacl, descriptor)
                        if code:
                            return code
                        pointer = dacl._obj.value
                        acl_size = struct.unpack_from(
                            "<H", ctypes.string_at(pointer, 8), 2)[0]
                        raw = bytearray(ctypes.string_at(pointer, acl_size))
                        ace_count = struct.unpack_from("<H", raw, 4)[0]
                        offset = 8
                        for _ in range(ace_count):
                            ace_size = struct.unpack_from("<H", raw, offset + 2)[0]
                            if raw[offset] == python_sandbox._ACCESS_ALLOWED_ACE:
                                ace_sid = python_sandbox._sid_string(
                                    pointer + offset + 8)
                                if ace_sid == sid:
                                    struct.pack_into("<I", raw, offset + 4, generic_mask)
                                    ctypes.memmove(pointer, bytes(raw), acl_size)
                                    return code
                            offset += ace_size
                        raise AssertionError("protected package ACE was not found")

                    with mock.patch.object(advapi, "GetNamedSecurityInfoW",
                                           side_effect=read_with_raw_mask):
                        self.assertFalse(python_sandbox._path_is_protected(
                            str(target), sid, allow_read=True))

    def test_cleanup_reports_a_remaining_private_tree(self):
        with tempfile.TemporaryDirectory() as directory, \
                self.assertRaises(OSError), \
                mock.patch.object(python_sandbox, "_run_icacls", return_value=True), \
                mock.patch.object(python_sandbox.shutil, "rmtree"):
            python_sandbox._remove_run_tree(directory, "S-1-test")

    def test_legacy_revocation_skips_absent_sid_without_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            normal = root / "normal"
            regular = normal / "regular"
            outside = base / "outside"
            regular.mkdir(parents=True)
            outside.mkdir()
            junction = normal / "linked"
            created = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction),
                 str(outside)],
                capture_output=True, text=True, check=False)
            self.assertEqual(created.returncode, 0, created.stderr)
            with mock.patch.object(
                    python_sandbox, "_run_icacls",
                    side_effect=AssertionError("absent SID must not invoke icacls")):
                revoked = python_sandbox._revoke_access(root, "S-1-test")
            self.assertTrue(revoked)

    def test_root_modify_grant_reaches_existing_children_and_revokes_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            existing = root / "existing.txt"
            existing.write_text("before", encoding="utf-8")
            state_path = base / "state.json"
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.DM.{uuid.uuid4().hex[:12]}")
            root_host_before = self.non_target_acl_aces(root, sid)
            child_host_before = self.non_target_acl_aces(existing, sid)
            with mock.patch.object(python_sandbox, "_protected_paths",
                                   return_value=[]), \
                    mock.patch.object(
                        python_sandbox, "_run_icacls",
                        side_effect=AssertionError("root grants must edit DACLs directly")):
                python_sandbox._configure_access(
                    sid, [os.path.normcase(str(root))], [], str(state_path),
                    os.path.normcase(str(root)))

            root_aces = self.access_aces(root, sid)
            child_aces = self.access_aces(existing, sid)
            self.assertTrue(root_aces)
            self.assertTrue(child_aces)
            for ace_type, flags, mask in root_aces:
                self.assertEqual(ace_type, python_sandbox._ACCESS_ALLOWED_ACE)
                self.assertTrue(mask & (0x00000002 | 0x40000000))
                self.assertTrue(mask & 0x00010000)
                self.assertFalse(mask & (0x00040000 | 0x00080000 | 0x00000040))
            self.assertTrue(any(
                flags & (python_sandbox._OBJECT_INHERIT_ACE
                         | python_sandbox._CONTAINER_INHERIT_ACE)
                == python_sandbox._OBJECT_INHERIT_ACE
                | python_sandbox._CONTAINER_INHERIT_ACE
                for _, flags, _ in root_aces))
            self.assertEqual(
                self.non_target_acl_aces(root, sid), root_host_before)
            self.assertEqual(
                self.non_target_acl_aces(existing, sid), child_host_before)

            self.assertTrue(python_sandbox._revoke_access(root, sid))
            self.assertEqual(self.access_aces(root, sid), [])
            self.assertEqual(self.access_aces(existing, sid), [])
            self.assertEqual(
                self.non_target_acl_aces(root, sid), root_host_before)
            self.assertEqual(
                self.non_target_acl_aces(existing, sid), child_host_before)

    def test_protected_nested_read_root_receives_a_direct_grant(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            nested = root / "nested"
            nested.mkdir(parents=True)
            existing = nested / "existing.txt"
            existing.write_text("before", encoding="utf-8")
            state_path = base / "state.json"
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.NR.{uuid.uuid4().hex[:12]}")
            root_key = os.path.normcase(str(root))
            nested_key = os.path.normcase(str(nested))
            python_sandbox._protect_path(nested_key, sid, False)
            with mock.patch.object(python_sandbox, "_protected_paths",
                                   return_value=[]), \
                    mock.patch.object(
                        python_sandbox, "_set_root_access",
                        wraps=python_sandbox._set_root_access) as set_access:
                python_sandbox._configure_access(
                    sid, [], [root_key, nested_key], str(state_path), root_key)
            self.assertCountEqual(
                [call.args[0] for call in set_access.call_args_list],
                [root_key, nested_key])
            state = python_sandbox._load_state(str(state_path), sid)
            self.assertEqual(state["read_roots"], sorted([root_key, nested_key]))
            self.assertTrue(self.access_aces(existing, sid))

    def test_disjoint_root_grants_parallelize_but_nested_grants_stay_sequential(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            nested = root / "nested"
            other = base / "other"
            nested.mkdir(parents=True)
            other.mkdir()
            state_path = base / "state.json"
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.PA.{uuid.uuid4().hex[:12]}")
            lock = threading.Lock()
            active = 0
            max_active = 0
            events = []

            def fake_set_access(path, sid_value, access_mask, **kwargs):
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                    started = time.monotonic()
                time.sleep(0.18 if path == os.path.normcase(str(other)) else 0.05)
                with lock:
                    events.append((path, started, time.monotonic()))
                    active -= 1
                return True

            with mock.patch.object(python_sandbox, "_protected_paths",
                                   return_value=[]), \
                    mock.patch.object(
                        python_sandbox, "_set_root_access",
                        side_effect=fake_set_access):
                python_sandbox._configure_access(
                    sid, [os.path.normcase(str(path)) for path in (root, nested, other)],
                    [], str(state_path), os.path.normcase(str(root)))
            self.assertGreaterEqual(max_active, 2)
            root_key = os.path.normcase(str(root))
            nested_key = os.path.normcase(str(nested))
            root_event = next(event for event in events if event[0] == root_key)
            nested_event = next(event for event in events if event[0] == nested_key)
            self.assertFalse(
                nested_event[1] < root_event[2] and root_event[1] < nested_event[2])
            other_event = next(
                event for event in events
                if event[0] == os.path.normcase(str(other)))
            self.assertLess(nested_event[1], other_event[2])

    def test_pending_grant_already_matching_desired_access_is_reconciled(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            existing = root / "existing.txt"
            existing.write_text("before", encoding="utf-8")
            state_path = base / "state.json"
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.PG.{uuid.uuid4().hex[:12]}")
            root_key = os.path.normcase(str(root))
            with mock.patch.object(python_sandbox, "_protected_paths",
                                   return_value=[]):
                python_sandbox._configure_access(
                    sid, [root_key], [], str(state_path), root_key)
                state = python_sandbox._load_state(str(state_path), sid)
                state["writable_roots"] = []
                state["pending_writable_roots"] = [root_key]
                python_sandbox._save_state(str(state_path), state)
                with mock.patch.object(
                        python_sandbox, "_set_root_access",
                        wraps=python_sandbox._set_root_access) as set_access:
                    python_sandbox._configure_access(
                        sid, [root_key], [], str(state_path), root_key)
            set_access.assert_not_called()
            state = python_sandbox._load_state(str(state_path), sid)
            self.assertEqual(state["writable_roots"], [root_key])
            self.assertEqual(state["pending_writable_roots"], [])
            self.assertTrue(any(
                mask & (0x00000002 | 0x40000000)
                for _, _, mask in self.access_aces(existing, sid)))

    def test_overlapping_write_and_read_roots_keep_modify_access(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            existing = root / "existing.txt"
            existing.write_text("before", encoding="utf-8")
            state_path = base / "state.json"
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.OV.{uuid.uuid4().hex[:12]}")
            with mock.patch.object(python_sandbox, "_protected_paths",
                                   return_value=[]), \
                    mock.patch.object(
                        python_sandbox, "_run_icacls",
                        side_effect=AssertionError("root grants must edit DACLs directly")):
                python_sandbox._configure_access(
                    sid, [os.path.normcase(str(root))],
                    [os.path.normcase(str(root))], str(state_path),
                    os.path.normcase(str(root)))
            for path in (root, existing):
                aces = self.access_aces(path, sid)
                self.assertTrue(aces)
                self.assertTrue(any(
                    mask & (0x00000002 | 0x40000000)
                    for _, _, mask in aces))
                self.assertTrue(all(
                    not mask & (0x00040000 | 0x00080000)
                    for _, _, mask in aces))

    def test_root_read_grant_reaches_existing_children_without_write_rights(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            existing = root / "existing.txt"
            existing.write_text("before", encoding="utf-8")
            state_path = base / "state.json"
            sid = self.derive_sid(
                f"Kestrel.PythonSandbox.Tests.DR.{uuid.uuid4().hex[:12]}")
            with mock.patch.object(python_sandbox, "_protected_paths",
                                   return_value=[]), \
                    mock.patch.object(
                        python_sandbox, "_run_icacls",
                        side_effect=AssertionError("root grants must edit DACLs directly")):
                python_sandbox._configure_access(
                    sid, [], [os.path.normcase(str(root))], str(state_path),
                    os.path.normcase(str(root)))
            root_aces = self.access_aces(root, sid)
            child_aces = self.access_aces(existing, sid)
            self.assertTrue(root_aces)
            self.assertTrue(child_aces)
            for aces in (root_aces, child_aces):
                self.assertTrue(aces)
                self.assertTrue(any(
                    ace_type == python_sandbox._ACCESS_ALLOWED_ACE
                    and not mask & python_sandbox._WRITE_ACCESS
                    for ace_type, _, mask in aces))
            child_allows = [
                mask for ace_type, _, mask in child_aces
                if ace_type == python_sandbox._ACCESS_ALLOWED_ACE]
            child_denies = [
                mask for ace_type, _, mask in child_aces
                if ace_type == python_sandbox._ACCESS_DENIED_ACE]
            self.assertTrue(any(
                mask & python_sandbox._FILE_READ_EXECUTE_ACCESS
                == python_sandbox._FILE_READ_EXECUTE_ACCESS
                for mask in child_allows))
            self.assertTrue(any(
                mask & python_sandbox._FILE_WRITE_DENY_ACCESS
                == python_sandbox._FILE_WRITE_DENY_ACCESS
                for mask in child_denies))
            self.assertEqual(self.label_aces(root), [("S-1-16-8192", 3, 1)])
            self.assertIn(("S-1-16-8192", 0x10, 1), self.label_aces(existing))


class ProtectedPathTests(unittest.TestCase):
    def test_discovers_nested_protected_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            nested = root / "nested"
            nested.mkdir(parents=True)
            expected = {}
            for name in (".git", ".venv"):
                path = nested / name
                path.mkdir()
                expected[os.path.normcase(path)] = True
            for name in (".env", "credentials.json",
                         "agent_user_session.session", "AgentBot_memory.md"):
                path = nested / name
                path.write_text("dummy", encoding="utf-8")
                expected[os.path.normcase(path)] = False
            with mock.patch("config.CONFIG_PATH", str(base / "config.json")), \
                    mock.patch("config.memory_dir", return_value=str(base / "memory")):
                protected = python_sandbox._protected_paths(
                    [str(root)], str(base / "state.json"))
            actual = {
                os.path.normcase(path): allow_read
                for path, allow_read in protected
                if Path(path).is_relative_to(root)
            }
            self.assertEqual(actual, expected)

    @unittest.skipUnless(os.name == "nt", "Windows junction integration test")
    def test_protected_named_junctions_resolve_to_their_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            expected = {}
            for name in (".git", ".venv"):
                target = root / f"{name[1:]}_target"
                target.mkdir()
                junction = root / name
                created = subprocess.run(
                    ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction),
                     str(target)], capture_output=True, text=True, check=False)
                self.assertEqual(created.returncode, 0, created.stderr)
                expected[os.path.normcase(str(target))] = True
                expected[os.path.normcase(str(junction))] = True
            with mock.patch("config.CONFIG_PATH", str(base / "config.json")), \
                    mock.patch("config.memory_dir", return_value=str(base / "memory")):
                protected = python_sandbox._protected_paths(
                    [str(root)], str(base / "state.json"))
            actual = {
                os.path.normcase(path): allow_read
                for path, allow_read in protected
                if Path(path).is_relative_to(root)
            }
            self.assertEqual(actual, expected)

    @unittest.skipUnless(os.name == "nt", "Windows junction integration test")
    def test_out_of_root_protected_junction_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            junction = root / ".git"
            created = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction),
                 str(outside)], capture_output=True, text=True, check=False)
            self.assertEqual(created.returncode, 0, created.stderr)
            with mock.patch("config.CONFIG_PATH", str(base / "config.json")), \
                    mock.patch("config.memory_dir", return_value=str(base / "memory")), \
                    self.assertRaisesRegex(ValueError, "outside"):
                python_sandbox._protected_paths(
                    [str(root)], str(base / "state.json"))

    def test_default_and_configured_telegram_sessions_are_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            for name in ("agent_docs_session.session", "custom_docs.session"):
                (base / name).write_text("dummy", encoding="utf-8")
            for configured in ("", "custom_docs"):
                with self.subTest(configured=configured), \
                        mock.patch("config.CONFIG_PATH", str(base / "config.json")), \
                        mock.patch("config.memory_dir", return_value=str(base / "memory")), \
                        mock.patch("config.load_config",
                                   return_value={"TG_DOC_SESSION": configured}), \
                        mock.patch.object(python_sandbox, "__file__",
                                          str(base / "python_sandbox.py")):
                    protected = {
                        os.path.normcase(path)
                        for path, _allow_read in python_sandbox._protected_paths(
                            [str(root)], str(base / "state.json"))
                    }
                self.assertIn(os.path.normcase(str(base / "agent_docs_session.session")),
                              protected)
                if configured:
                    self.assertIn(os.path.normcase(str(base / "custom_docs.session")),
                                  protected)
                else:
                    self.assertNotIn(os.path.normcase(str(base / "custom_docs.session")),
                                     protected)

    @unittest.skipUnless(os.name == "nt", "Windows junction integration test")
    def test_protected_path_scan_does_not_cross_a_directory_junction(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            external_secret = outside / ".env"
            external_secret.write_text("dummy", encoding="utf-8")
            junction = root / "linked"
            created = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction),
                 str(outside)],
                capture_output=True, text=True, check=False)
            self.assertEqual(created.returncode, 0, created.stderr)
            with mock.patch("config.CONFIG_PATH", str(base / "config.json")), \
                    mock.patch("config.memory_dir", return_value=str(base / "memory")):
                protected = python_sandbox._protected_paths(
                    [str(root)], str(base / "state.json"))
            discovered = {os.path.normcase(path) for path, _ in protected}
            self.assertNotIn(os.path.normcase(junction / ".env"), discovered)


if __name__ == "__main__":
    unittest.main()
