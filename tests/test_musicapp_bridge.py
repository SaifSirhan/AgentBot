"""Kestrel's client and tool for the MusicAppDesktop local bridge.

No MusicAppDesktop process is needed. Client tests use a real loopback HTTP
server so headers, paths and payloads are exercised end to end.

Run: python -m unittest tests.test_musicapp_bridge -v
"""
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent
import musicapp_bridge
import native_tools
import prompt_catalog
from tool_registry import InputValidationError


class FakeBridge:
    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body if body is not None else {
            "ok": True,
            "result": {"ok": True},
        }
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                outer.requests.append({
                    "path": self.path,
                    "headers": dict(self.headers),
                    "json": json.loads(raw),
                })
                payload = json.dumps(outer.body).encode("utf-8")
                self.send_response(outer.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def port(self):
        return self.server.server_address[1]


def write_credentials(path, port, token="unit-test-token-123"):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"port": port, "token": token}, handle)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.credentials = os.path.join(self.temp.name, "kestrel-bridge.json")

    def test_posts_the_action_with_a_bearer_token(self):
        with FakeBridge() as bridge:
            write_credentials(self.credentials, bridge.port)
            result = musicapp_bridge.call(
                "next", path=self.credentials, timeout=2)

            self.assertEqual(result, {"ok": True})
            request = bridge.requests[0]
            self.assertEqual(request["path"], "/v1/action")
            self.assertEqual(
                request["headers"]["Authorization"],
                "Bearer unit-test-token-123")
            self.assertEqual(request["json"], {"action": "next"})

    def test_forwards_params_beside_the_action(self):
        with FakeBridge() as bridge:
            write_credentials(self.credentials, bridge.port)
            musicapp_bridge.call(
                "seek", {"seconds": 42}, path=self.credentials, timeout=2)

            self.assertEqual(
                bridge.requests[0]["json"],
                {"action": "seek", "seconds": 42},
            )

    def test_missing_credentials_are_unavailable(self):
        with self.assertRaises(musicapp_bridge.MusicAppUnavailable):
            musicapp_bridge.call("next", path=self.credentials)

    def test_malformed_credentials_are_unavailable(self):
        with open(self.credentials, "w", encoding="utf-8") as handle:
            handle.write("{}")
        with self.assertRaises(musicapp_bridge.MusicAppUnavailable):
            musicapp_bridge.call("next", path=self.credentials)

    def test_http_errors_are_reported_without_the_token(self):
        with FakeBridge(
            status=400,
            body={"ok": False, "error": "unknown action 'explode'"},
        ) as bridge:
            write_credentials(self.credentials, bridge.port)
            with self.assertRaises(musicapp_bridge.MusicAppError) as caught:
                musicapp_bridge.call(
                    "next", path=self.credentials, timeout=2)

            self.assertEqual(caught.exception.status, 400)
            self.assertIn("unknown action", str(caught.exception))
            self.assertNotIn("unit-test-token-123", str(caught.exception))

    def test_a_closed_port_is_unavailable(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        write_credentials(self.credentials, port)

        with self.assertRaises(musicapp_bridge.MusicAppUnavailable):
            musicapp_bridge.call("next", path=self.credentials, timeout=1)

    def test_unknown_actions_never_reach_the_network(self):
        with mock.patch.object(musicapp_bridge, "load_credentials") as loader:
            with self.assertRaises(musicapp_bridge.MusicAppError):
                musicapp_bridge.call("explode", path=self.credentials)
            loader.assert_not_called()

    def test_the_token_path_can_be_overridden(self):
        old = os.environ.get(musicapp_bridge.TOKEN_FILE_ENV)
        try:
            os.environ[musicapp_bridge.TOKEN_FILE_ENV] = self.credentials
            self.assertEqual(
                musicapp_bridge.token_path(), self.credentials)
        finally:
            if old is None:
                os.environ.pop(musicapp_bridge.TOKEN_FILE_ENV, None)
            else:
                os.environ[musicapp_bridge.TOKEN_FILE_ENV] = old


class DescribeTests(unittest.TestCase):
    def test_state_reports_the_song_volume_and_queue(self):
        text = musicapp_bridge.describe("state", {
            "song": {
                "title": "Song C",
                "artist": "Artist C",
                "album": "Album C",
                "videoId": "ccccccccccc",
                "duration": 201,
                "elapsed": 12,
                "paused": False,
            },
            "player": {
                "playerState": 1,
                "currentTime": 12,
                "duration": 201,
                "volume": 55,
                "muted": False,
                "url": "https://music.youtube.com/watch?v=ccccccccccc",
                "shuffle": False,
                "fullscreen": False,
                "repeat": "NONE",
            },
            "queue": {"count": 2, "currentIndex": 0, "items": []},
        })

        self.assertIn('playing "Song C"', text)
        self.assertIn("Artist C", text)
        self.assertIn("0:12/3:21", text)
        self.assertIn("volume 55%", text)
        self.assertIn("2 queued", text)

    def test_state_without_a_song_says_so(self):
        text = musicapp_bridge.describe("state", {"song": None})
        self.assertIn("nothing is loaded", text.lower())

    def test_search_lists_matches(self):
        text = musicapp_bridge.describe("search", {
            "items": [{
                "title": "Song C",
                "artist": "Artist C",
                "album": "Album C",
                "duration": "3:21",
                "videoId": "ccccccccccc",
            }],
        })

        self.assertIn("Song C", text)
        self.assertIn("Artist C", text)
        self.assertIn("ccccccccccc", text)

    def test_empty_search_is_honest(self):
        text = musicapp_bridge.describe("search", {"items": []})
        self.assertIn("no matches", text.lower())

    def test_queue_lists_items_and_marks_the_current_one(self):
        text = musicapp_bridge.describe("queue_list", {
            "count": 2,
            "currentIndex": 0,
            "items": [
                {"title": "One", "artist": "Artist One", "selected": True},
                {"title": "Two", "artist": "Artist Two", "selected": False},
            ],
        })

        self.assertIn("1. One", text)
        self.assertIn("2. Two", text)

    def test_transport_actions_answer_plainly(self):
        self.assertIn("paused", musicapp_bridge.describe("pause", {"ok": True}))
        self.assertIn("skipped", musicapp_bridge.describe("next", {"ok": True}))


class ToolTests(unittest.TestCase):
    def test_the_tool_describes_a_successful_call(self):
        with mock.patch.object(agent.musicapp_bridge, "call",
                               return_value={"ok": True}) as call:
            result = agent.musicapp_control({"action": "pause"})

        call.assert_called_once_with("pause", {})
        self.assertIn("paused", result)

    def test_the_tool_forwards_the_nested_params(self):
        with mock.patch.object(agent.musicapp_bridge, "call",
                               return_value={"ok": True}) as call:
            agent.musicapp_control(
                {"action": "seek", "params": {"seconds": 30}})

        call.assert_called_once_with("seek", {"seconds": 30})

    def test_an_unknown_action_is_rejected_without_a_call(self):
        with mock.patch.object(agent.musicapp_bridge, "call") as call:
            result = agent.musicapp_control({"action": "explode"})

        call.assert_not_called()
        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("state", result)

    def test_a_missing_bridge_gets_an_honest_error(self):
        with mock.patch.object(
            agent.musicapp_bridge, "call",
            side_effect=musicapp_bridge.MusicAppUnavailable("down"),
        ):
            result = agent.musicapp_control({"action": "play"})

        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("not running", result)

    def test_a_rejected_action_is_reported(self):
        with mock.patch.object(
            agent.musicapp_bridge, "call",
            side_effect=musicapp_bridge.MusicAppError(400, "bad seconds"),
        ):
            result = agent.musicapp_control(
                {"action": "seek", "params": {"seconds": "soon"}})

        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("bad seconds", result)


class SchemaTests(unittest.TestCase):
    def test_the_tool_is_advertised_everywhere_it_must_be(self):
        self.assertIn("musicapp_control", agent.VALID_TOOLS)
        self.assertIn("musicapp_control", agent.OBJECT_INPUT_TOOLS)
        self.assertIn("musicapp_control", prompt_catalog.MUSIC_TOOLS)
        self.assertIn("musicapp_control", prompt_catalog.DESCRIPTIONS)
        self.assertIn("musicapp_control", prompt_catalog.EXTRA_KEYWORDS)
        self.assertIsNotNone(agent.TOOL_REGISTRY.get("musicapp_control"))

    def test_the_schema_accepts_every_documented_action(self):
        spec = agent.TOOL_REGISTRY.get("musicapp_control")
        for action in sorted(musicapp_bridge.ACTIONS):
            with self.subTest(action=action):
                spec.validate({"action": action})

    def test_the_schema_requires_an_action_and_object_params(self):
        spec = agent.TOOL_REGISTRY.get("musicapp_control")
        spec.validate({"action": "seek", "params": {"seconds": 30}})
        for value in ({}, [], {"action": "seek", "params": 30}):
            with self.subTest(value=value):
                with self.assertRaises(InputValidationError):
                    spec.validate(value)

    def test_native_callers_get_action_and_params(self):
        specs = native_tools.build_specs(["musicapp_control"],
                                         agent.TOOL_REGISTRY)
        parameters = specs[0]["function"]["parameters"]
        self.assertEqual(parameters["type"], "object")
        self.assertEqual(parameters["properties"]["action"]["type"], "string")
        self.assertEqual(parameters["properties"]["params"]["type"], "object")
        self.assertEqual(parameters["required"], ["action"])
        self.assertIs(parameters["additionalProperties"], False)


if __name__ == "__main__":
    unittest.main()
