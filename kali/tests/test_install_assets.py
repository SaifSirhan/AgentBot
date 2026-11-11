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
