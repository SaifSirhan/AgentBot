"""Artifact store round-trips and path-safety tests.

Run: python -m unittest tests.test_artifact_store -v
"""
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import artifact_store


class ArtifactStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        patcher = mock.patch.dict(
            os.environ, {"LOCALAPPDATA": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def artifact_path(self, artifact_id, suffix=".txt"):
        return (self.root / "AgentMemory" / "artifacts"
                / f"{artifact_id}{suffix}")

    def test_text_roundtrip_under_localappdata(self):
        content = "large output line\n" * 400
        artifact_id = artifact_store.save_artifact(content, "search_web")

        self.assertRegex(artifact_id, r"^[0-9a-f]{32}$")
        self.assertEqual(
            self.artifact_path(artifact_id).read_text(encoding="utf-8"),
            content)
        self.assertEqual(artifact_store.read_artifact(artifact_id), content)

    def test_json_content_is_saved_as_text(self):
        payload = {"url": "https://example.com", "items": [1, 2]}
        artifact_id = artifact_store.save_artifact(payload, "web_fetch")

        self.assertEqual(
            json.loads(artifact_store.read_artifact(artifact_id)), payload)

    def test_source_metadata_is_recorded(self):
        artifact_id = artifact_store.save_artifact("body", "web")

        meta = json.loads(
            self.artifact_path(artifact_id, ".json")
            .read_text(encoding="utf-8"))
        self.assertEqual(meta["source"], "web")
        self.assertEqual(meta["chars"], 4)

    def test_dashed_uuid_reads_the_same_artifact(self):
        artifact_id = artifact_store.save_artifact("body", "agent")

        self.assertEqual(
            artifact_store.read_artifact(str(uuid.UUID(hex=artifact_id))),
            "body")

    def test_invalid_ids_are_rejected(self):
        for value in ("", "..", "../facts", "a/b", "not-a-uuid", "0" * 31):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    artifact_store.read_artifact(value)

    def test_missing_artifact_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            artifact_store.read_artifact(uuid.uuid4().hex)
