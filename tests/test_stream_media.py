"""Tests for stream_media's URL/query routing and player launch.

yt-dlp, the player lookup, and subprocess.Popen are faked, so these tests
make no network calls and start no processes.

Run: python -m unittest tests.test_stream_media -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault(
    "AGENTBOT_MOOD_FILE",
    os.path.join(tempfile.gettempdir(), "agentbot_test_mood.json"))

import agent


class FakeYtDlp:
    """Stand-in for the yt_dlp module that records requested targets."""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.targets = []
        self.opts = None
        outer = self

        class YDL:
            def __init__(self, opts):
                outer.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, target, download=False):
                outer.targets.append(target)
                if outer.error:
                    raise outer.error
                return outer.result

        self.YoutubeDL = YDL


def video(**overrides):
    info = {
        "title": "Episode 1",
        "webpage_url": "https://example.com/watch?v=1",
        "url": "https://cdn.example.com/stream.mp4",
        "acodec": "aac",
        "vcodec": "h264",
    }
    info.update(overrides)
    return info


SPLIT_FORMATS = [
    {"url": "https://cdn.example.com/video", "vcodec": "h264", "acodec": "none"},
    {"url": "https://cdn.example.com/audio", "vcodec": "none", "acodec": "aac"},
]
MUXED_FORMAT = {"url": "https://cdn.example.com/muxed.mp4",
                "vcodec": "h264", "acodec": "aac"}
HOOK = r"--script-opts=ytdl_hook-ytdl_path=C:\yt-dlp.exe"


class StreamMediaTests(unittest.TestCase):
    def run_tool(self, query, result=None, error=None,
                 player=("mpv", r"C:\mpv.exe"), ytdlp=r"C:\yt-dlp.exe"):
        fake = FakeYtDlp(result=result, error=error)
        with mock.patch.multiple(agent, YTDLP_AVAILABLE=True, yt_dlp=fake), \
                mock.patch.object(agent, "_find_media_player",
                                  return_value=player), \
                mock.patch.object(agent, "_find_ytdlp", return_value=ytdlp), \
                mock.patch.object(agent.subprocess, "Popen") as popen, \
                mock.patch.object(agent.webbrowser, "open") as browser_open, \
                mock.patch.object(agent._cfg, "memory_path",
                                  return_value=os.path.join(tempfile.gettempdir(),
                                                            "agentbot_test_mpv.log")):
            reply = agent.stream_media(query)
        return reply, fake, popen, browser_open

    # ------------------------------------------------------------------
    # Routing: search query vs URL
    # ------------------------------------------------------------------
    def test_search_query_uses_ytsearch1(self):
        reply, fake, popen, _ = self.run_tool(
            "one piece episode 1", {"entries": [video()]})

        self.assertEqual(fake.targets, ["ytsearch1:one piece episode 1"])
        self.assertEqual(popen.call_args[0][0],
                         [r"C:\mpv.exe", HOOK,
                          "https://cdn.example.com/stream.mp4"])
        self.assertEqual(reply, "Playing in mpv: Episode 1")

    def test_url_input_is_passed_through(self):
        url = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
        reply, fake, popen, _ = self.run_tool(
            url, video(webpage_url=url, url="https://cdn.example.com/x.m3u8"))

        self.assertEqual(fake.targets, [url])
        self.assertEqual(popen.call_args[0][0],
                         [r"C:\mpv.exe", HOOK,
                          "https://cdn.example.com/x.m3u8"])

    # ------------------------------------------------------------------
    # Stream selection
    # ------------------------------------------------------------------
    def test_split_streams_hand_mpv_the_page_url(self):
        reply, _, popen, _ = self.run_tool(
            "https://example.com/watch?v=1",
            video(url=None, formats=SPLIT_FORMATS))

        self.assertEqual(popen.call_args[0][0], [
            r"C:\mpv.exe",
            HOOK,
            "https://example.com/watch?v=1",
        ])
        self.assertEqual(reply, "Playing in mpv: Episode 1")

    def test_muxed_format_is_preferred(self):
        _, _, popen, _ = self.run_tool(
            "https://example.com/watch?v=1",
            video(url=None, formats=SPLIT_FORMATS + [MUXED_FORMAT]))

        self.assertEqual(popen.call_args[0][0],
                         [r"C:\mpv.exe", HOOK, "https://cdn.example.com/muxed.mp4"])

    def test_single_format_without_codec_info_is_played_directly(self):
        _, _, popen, _ = self.run_tool(
            "https://example.com/clip.mp4",
            {"title": "Clip", "webpage_url": "https://example.com/clip.mp4",
             "url": "https://cdn.example.com/clip.mp4"})

        self.assertEqual(popen.call_args[0][0],
                         [r"C:\mpv.exe", HOOK, "https://cdn.example.com/clip.mp4"])

    # ------------------------------------------------------------------
    # Players and browser fallback
    # ------------------------------------------------------------------
    def test_no_ytdlp_omits_the_hook(self):
        reply, _, popen, _ = self.run_tool(
            "x", {"entries": [video()]}, ytdlp=None)

        self.assertEqual(popen.call_args[0][0],
                         [r"C:\mpv.exe", "https://cdn.example.com/stream.mp4"])
        self.assertEqual(reply, "Playing in mpv: Episode 1")

    def test_mpv_output_is_written_to_the_log_file(self):
        log_path = os.path.join(tempfile.gettempdir(), "agentbot_test_mpv.log")
        if os.path.exists(log_path):
            os.remove(log_path)
        fake = FakeYtDlp(result={"entries": [video()]})
        with mock.patch.multiple(agent, YTDLP_AVAILABLE=True, yt_dlp=fake), \
                mock.patch.object(agent, "_find_media_player",
                                  return_value=("mpv", r"C:\mpv.exe")), \
                mock.patch.object(agent, "_find_ytdlp",
                                  return_value=r"C:\yt-dlp.exe"), \
                mock.patch.object(agent._cfg, "memory_path",
                                  return_value=log_path), \
                mock.patch.object(agent.subprocess, "Popen") as popen:
            agent.stream_media("x")

        self.assertIs(popen.call_args.kwargs["stdout"],
                      popen.call_args.kwargs["stderr"])
        self.assertIsNot(popen.call_args.kwargs["stdout"], agent.subprocess.DEVNULL)
        with open(log_path, encoding="utf-8") as f:
            log = f.read()
        self.assertIn("ytdl_hook-ytdl_path", log)
        self.assertIn("https://cdn.example.com/stream.mp4", log)

    def test_vlc_used_when_mpv_is_absent(self):
        reply, _, popen, browser_open = self.run_tool(
            "x", {"entries": [video()]}, player=("VLC", r"C:\vlc.exe"))

        self.assertEqual(popen.call_args[0][0],
                         [r"C:\vlc.exe", "https://cdn.example.com/stream.mp4"])
        self.assertEqual(reply, "Playing in VLC: Episode 1")
        browser_open.assert_not_called()

    def test_browser_fallback_opens_stream_url(self):
        reply, _, popen, browser_open = self.run_tool(
            "x", {"entries": [video()]}, player=(None, None))

        popen.assert_not_called()
        browser_open.assert_called_once_with("https://cdn.example.com/stream.mp4")
        self.assertTrue(reply.startswith("No mpv/VLC found"))

    def test_browser_fallback_opens_page_when_streams_are_split(self):
        reply, _, _, browser_open = self.run_tool(
            "https://example.com/watch?v=1",
            video(url=None, formats=SPLIT_FORMATS), player=(None, None))

        browser_open.assert_called_once_with("https://example.com/watch?v=1")
        self.assertIn("splits video and audio", reply)

    # ------------------------------------------------------------------
    # Failures
    # ------------------------------------------------------------------
    def test_missing_ytdlp_returns_error(self):
        with mock.patch.object(agent, "YTDLP_AVAILABLE", False):
            reply = agent.stream_media("anything")

        self.assertTrue(reply.startswith("ERROR: yt-dlp is not installed"))

    def test_empty_input_returns_error(self):
        self.assertTrue(
            agent.stream_media("   ").startswith("ERROR: stream_media needs"))

    def test_search_without_results_returns_error(self):
        reply, _, popen, _ = self.run_tool("nothing", {"entries": []})

        self.assertIn("no playable results", reply)
        popen.assert_not_called()

    def test_extraction_failure_is_reported(self):
        reply, _, popen, _ = self.run_tool("x", error=RuntimeError("boom"))

        self.assertEqual(reply, "ERROR: cannot resolve 'x': boom")
        popen.assert_not_called()

    def test_player_start_failure_is_reported(self):
        fake = FakeYtDlp(result={"entries": [video()]})
        with mock.patch.multiple(agent, YTDLP_AVAILABLE=True, yt_dlp=fake), \
                mock.patch.object(agent, "_find_media_player",
                                  return_value=("mpv", r"C:\mpv.exe")), \
                mock.patch.object(agent.subprocess, "Popen",
                                  side_effect=OSError("denied")):
            reply = agent.stream_media("x")

        self.assertEqual(reply, "ERROR: cannot start mpv: denied")


class PlayerLookupTests(unittest.TestCase):
    def test_path_wins(self):
        with mock.patch.object(agent.shutil, "which", return_value=r"C:\tools\mpv.exe"):
            self.assertEqual(agent._find_mpv(), r"C:\tools\mpv.exe")

    def test_winget_link_is_used_when_not_on_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp, "Microsoft", "WinGet", "Links")
            links.mkdir(parents=True)
            (links / "mpv.exe").write_bytes(b"")
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}), \
                    mock.patch.object(agent.shutil, "which", return_value=None):
                self.assertEqual(agent._find_mpv(), str(links / "mpv.exe"))

    def test_program_files_install_is_used(self):
        installed = r"C:\Program Files\MPV Player\mpv.exe"
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": ""}), \
                mock.patch.object(agent.shutil, "which", return_value=None), \
                mock.patch.object(agent.os.path, "isfile",
                                  side_effect=lambda p: p == installed):
            self.assertEqual(agent._find_mpv(), installed)

    def test_missing_mpv_returns_none(self):
        with mock.patch.object(agent.shutil, "which", return_value=None), \
                mock.patch.object(agent.os.path, "isfile", return_value=False):
            self.assertIsNone(agent._find_mpv())

    def test_player_falls_back_from_mpv_to_vlc_to_none(self):
        with mock.patch.object(agent, "_find_mpv", return_value=r"C:\mpv.exe"):
            self.assertEqual(agent._find_media_player(), ("mpv", r"C:\mpv.exe"))

        with mock.patch.object(agent, "_find_mpv", return_value=None), \
                mock.patch.object(agent.shutil, "which",
                                  side_effect=lambda b: r"C:\vlc.exe" if b == "vlc" else None):
            self.assertEqual(agent._find_media_player(), ("VLC", r"C:\vlc.exe"))

        with mock.patch.object(agent, "_find_mpv", return_value=None), \
                mock.patch.object(agent.shutil, "which", return_value=None), \
                mock.patch.object(agent.os.path, "isfile", return_value=False):
            self.assertEqual(agent._find_media_player(), (None, None))


    def test_ytdlp_from_path(self):
        with mock.patch.object(agent.shutil, "which", return_value=r"C:\tools\yt-dlp.exe"):
            self.assertEqual(agent._find_ytdlp(), r"C:\tools\yt-dlp.exe")

    def test_ytdlp_from_interpreter_scripts_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp, "Scripts")
            scripts.mkdir()
            (scripts / "yt-dlp.exe").write_bytes(b"")
            with mock.patch.object(agent.shutil, "which", return_value=None), \
                    mock.patch.object(agent.sys, "executable",
                                      str(Path(tmp, "python.exe"))):
                self.assertEqual(agent._find_ytdlp(), str(scripts / "yt-dlp.exe"))

    def test_missing_ytdlp_returns_none(self):
        with mock.patch.object(agent.shutil, "which", return_value=None), \
                mock.patch.object(agent.sys, "executable", r"C:\nope\python.exe"), \
                mock.patch("sysconfig.get_path", return_value=""), \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": "", "APPDATA": ""}):
            self.assertIsNone(agent._find_ytdlp())


if __name__ == "__main__":
    unittest.main()
