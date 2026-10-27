"""Tests for the AGENT_MEMORY_DIR setting and its path helpers.

Run: python -m unittest tests.test_memory_dir -v
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import memory


class MemoryDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="memory_dir_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"
        patcher = mock.patch.object(config, "CONFIG_PATH", str(self.config_path))
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AGENT_MEMORY_DIR", None)

    def test_default_when_unset(self):
        self.assertEqual(config.memory_dir(), config.DEFAULT_MEMORY_DIR)

    def test_environment_override(self):
        target = self.root / "brain"
        os.environ["AGENT_MEMORY_DIR"] = str(target)

        self.assertEqual(config.memory_dir(), os.path.abspath(str(target)))
        self.assertTrue(target.is_dir())
        self.assertEqual(
            config.memory_path("facts.json"),
            os.path.join(os.path.abspath(str(target)), "facts.json"))

    def test_config_file_override(self):
        target = self.root / "from_config"
        self.config_path.write_text(
            json.dumps({"AGENT_MEMORY_DIR": str(target)}), encoding="utf-8")

        self.assertEqual(config.memory_dir(), os.path.abspath(str(target)))

    def test_environment_beats_config_file(self):
        self.config_path.write_text(
            json.dumps({"AGENT_MEMORY_DIR": str(self.root / "file")}),
            encoding="utf-8")
        env_target = self.root / "env"
        os.environ["AGENT_MEMORY_DIR"] = str(env_target)

        self.assertEqual(config.memory_dir(), os.path.abspath(str(env_target)))

    def test_facts_path_follows_the_setting(self):
        target = self.root / "brain"
        os.environ["AGENT_MEMORY_DIR"] = str(target)

        self.assertEqual(
            memory._memory_file(),
            os.path.join(os.path.abspath(str(target)), "facts.json"))


if __name__ == "__main__":
    unittest.main()
