"""Tests for play_on_youtube's CDP tab reuse, with a fake Playwright browser.

No real Opera connection is made and nothing is navigated.

Run: python -m unittest tests.test_music_cdp -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent


class FakePage:
    def __init__(self, url):
        self.url = url
        self.goto_calls = []
        self.brought_to_front = 0

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))

    def bring_to_front(self):
        self.brought_to_front += 1


class BrokenPage:
    @property
    def url(self):
        raise RuntimeError("page closed")


class FakeContext:
    def __init__(self, pages=()):
        self.pages = list(pages)
        self.created = []

    def new_page(self):
        page = FakePage("about:blank")
        self.pages.append(page)
        self.created.append(page)
        return page


class FakeBrowser:
    def __init__(self, contexts):
        self.contexts = list(contexts)
        self.new_contexts = []

    def new_context(self):
        context = FakeContext()
        self.new_contexts.append(context)
        return context


class FakeChromium:
    def __init__(self, browser=None, error=None):
        self.browser = browser
        self.error = error
        self.calls = []

    def connect_over_cdp(self, endpoint, timeout=None):
        self.calls.append((endpoint, timeout))
        if self.error is not None:
            raise self.error
        return self.browser


class FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class MusicCdpTests(unittest.TestCase):
    def patch_cdp(self, chromium):
        playwright = FakePlaywright(chromium)
        patcher = mock.patch.object(
            agent, "_cdp_playwright", lambda: (lambda: playwright))
        patcher.start()
        self.addCleanup(patcher.stop)

    def play_url(self, video_id="newvideo123"):
        return f"https://music.youtube.com/watch?v={video_id}&autoplay=1"

    def test_reuses_existing_music_tab(self):
        other = FakePage("https://www.youtube.com/")
        music = FakePage("https://music.youtube.com/watch?v=old&autoplay=1")
        context = FakeContext([other, music])
        chromium = FakeChromium(FakeBrowser([context]))
        self.patch_cdp(chromium)

        url = self.play_url()
        self.assertEqual(agent._play_in_music_tab(url), "reused")

        self.assertEqual(music.goto_calls[0][0], url)
        self.assertEqual(music.goto_calls[0][1]["wait_until"], "domcontentloaded")
        self.assertEqual(music.brought_to_front, 1)
        self.assertEqual(other.goto_calls, [])
        self.assertEqual(context.created, [])
        self.assertEqual(
            chromium.calls, [(agent.OPERA_CDP_ENDPOINT, 5000)])

    def test_opens_new_tab_when_no_music_tab_exists(self):
        other = FakePage("https://www.youtube.com/")
        context = FakeContext([other])
        self.patch_cdp(FakeChromium(FakeBrowser([context])))

        url = self.play_url()
        self.assertEqual(agent._play_in_music_tab(url), "opened")

        self.assertEqual(len(context.created), 1)
        created = context.created[0]
        self.assertEqual(created.goto_calls[0][0], url)
        self.assertEqual(created.brought_to_front, 1)
        self.assertEqual(other.goto_calls, [])

    def test_creates_context_when_none_exists(self):
        browser = FakeBrowser([])
        self.patch_cdp(FakeChromium(browser))

        url = self.play_url()
        self.assertEqual(agent._play_in_music_tab(url), "opened")

        self.assertEqual(len(browser.new_contexts), 1)
        created = browser.new_contexts[0].created[0]
        self.assertEqual(created.goto_calls[0][0], url)

    def test_closed_page_is_skipped(self):
        music = FakePage("https://music.youtube.com/")
        context = FakeContext([BrokenPage(), music])
        self.patch_cdp(FakeChromium(FakeBrowser([context])))

        self.assertEqual(agent._play_in_music_tab(self.play_url()), "reused")
        self.assertEqual(len(music.goto_calls), 1)

    def test_connection_failure_returns_none(self):
        chromium = FakeChromium(error=RuntimeError("connection refused"))
        self.patch_cdp(chromium)

        self.assertIsNone(agent._play_in_music_tab(self.play_url()))
        self.assertEqual(len(chromium.calls), 1)

    def test_goto_failure_returns_none(self):
        page = FakePage("https://music.youtube.com/")

        def boom(url, **kwargs):
            raise RuntimeError("navigation failed")

        page.goto = boom
        context = FakeContext([page])
        self.patch_cdp(FakeChromium(FakeBrowser([context])))

        self.assertIsNone(agent._play_in_music_tab(self.play_url()))

    # ------------------------------------------------------------------
    # play_on_youtube integration
    # ------------------------------------------------------------------
    def search_response(self):
        return mock.Mock(status_code=200, text='{"videoId":"abc12345678"}')

    def test_play_on_youtube_uses_existing_tab(self):
        with mock.patch.object(
            agent.requests, "get", return_value=self.search_response(),
        ), mock.patch.object(
            agent, "_play_in_music_tab", return_value="reused",
        ) as play, mock.patch.object(
            agent, "_open_in_new_tab",
        ) as fallback:
            result = agent.play_on_youtube("the strokes")

        play.assert_called_once_with(
            "https://music.youtube.com/watch?v=abc12345678&autoplay=1")
        fallback.assert_not_called()
        self.assertIn("existing", result)

    def test_play_on_youtube_reports_a_new_cdp_tab(self):
        with mock.patch.object(
            agent.requests, "get", return_value=self.search_response(),
        ), mock.patch.object(agent, "_play_in_music_tab", return_value="opened"):
            result = agent.play_on_youtube("the strokes")

        self.assertIn("new", result)

    def test_play_on_youtube_falls_back_when_cdp_is_unavailable(self):
        with mock.patch.object(
            agent.requests, "get", return_value=self.search_response(),
        ), mock.patch.object(
            agent, "_play_in_music_tab", return_value=None,
        ), mock.patch.object(
            agent, "_open_in_new_tab", return_value="Opened in Opera: Playing: x",
        ) as fallback:
            result = agent.play_on_youtube("the strokes")

        fallback.assert_called_once_with(
            "https://music.youtube.com/watch?v=abc12345678&autoplay=1",
            label="Playing: the strokes",
        )
        self.assertEqual(result, "Opened in Opera: Playing: x")

    def test_play_on_youtube_rejects_empty_query(self):
        with mock.patch.object(agent.requests, "get") as get:
            result = agent.play_on_youtube("  ")
        get.assert_not_called()
        self.assertTrue(result.startswith("ERROR:"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
