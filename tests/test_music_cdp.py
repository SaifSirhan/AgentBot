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
        self.close_calls = 0
        self.closed = False

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))

    def bring_to_front(self):
        self.brought_to_front += 1

    def close(self):
        self.close_calls += 1
        self.closed = True

    def is_closed(self):
        return self.closed


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    def locator(self, selector):
        return FakeLocator(self.page, selector)

    def get_by_text(self, text, exact=False):
        return FakeLocator(self.page, f"text={text}")

    def click(self, **kwargs):
        if self.page.fail_selector and self.page.fail_selector in self.selector:
            raise RuntimeError("DOM changed: " + self.selector)
        self.page.clicks.append(self.selector)

    def wait_for(self, **kwargs):
        if self.page.fail_selector and self.page.fail_selector in self.selector:
            raise RuntimeError("DOM changed: " + self.selector)
        self.page.waits.append(self.selector)

    def count(self):
        return self.page.counts.get(self.selector, 1)


class FakeQueuePage(FakePage):
    def __init__(self, url, fail_selector=None):
        super().__init__(url)
        self.fail_selector = fail_selector
        self.clicks = []
        self.waits = []
        self.counts = {}

    def locator(self, selector):
        return FakeLocator(self, selector)

    def get_by_text(self, text, exact=False):
        return FakeLocator(self, f"text={text}")


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
    # close_music_tab
    # ------------------------------------------------------------------
    def test_close_music_tab_closes_only_the_music_tab(self):
        other = FakePage("https://www.youtube.com/")
        music = FakePage("https://music.youtube.com/watch?v=old")
        chromium = FakeChromium(FakeBrowser([FakeContext([other, music])]))
        self.patch_cdp(chromium)

        result = agent.close_music_tab()

        self.assertIn("Closed", result)
        self.assertEqual(music.close_calls, 1)
        self.assertEqual(other.close_calls, 0)
        self.assertEqual(chromium.calls, [(agent.OPERA_CDP_ENDPOINT, 5000)])

    def test_close_music_tab_reports_when_no_music_tab_is_open(self):
        other = FakePage("https://www.youtube.com/")
        self.patch_cdp(FakeChromium(FakeBrowser([FakeContext([other])])))

        result = agent.close_music_tab()

        self.assertTrue(result.startswith("ERROR:"))
        self.assertEqual(other.close_calls, 0)

    def test_close_music_tab_reports_cdp_failure(self):
        self.patch_cdp(FakeChromium(error=RuntimeError("connection refused")))

        result = agent.close_music_tab()

        self.assertTrue(result.startswith("ERROR:"))
        self.assertIn("connection refused", result)

    def test_close_music_tab_skips_closed_pages(self):
        music = FakePage("https://music.youtube.com/")
        self.patch_cdp(FakeChromium(FakeBrowser([
            FakeContext([BrokenPage(), music])])))

        self.assertIn("Closed", agent.close_music_tab())

        self.assertEqual(music.close_calls, 1)

    def test_close_music_tab_reports_a_tab_that_refused_to_close(self):
        music = FakePage("https://music.youtube.com/")
        music.close = lambda: None
        self.patch_cdp(FakeChromium(FakeBrowser([FakeContext([music])])))

        self.assertTrue(agent.close_music_tab().startswith("ERROR:"))

    def test_router_exposes_close_music_tab_for_tab_requests(self):
        with mock.patch.object(
                agent, "_classify_unmatched_intents", return_value=[]):
            _prompt, tools = agent._route_prompt(
                "close the previous youtube music tab")
        self.assertIn("close_music_tab", [tool.name for tool in tools])

    def test_router_exposes_media_control_for_playback_commands(self):
        for request in ("pause the music", "skip this song",
                        "next track", "stop the music"):
            with self.subTest(request=request), mock.patch.object(
                    agent, "_classify_unmatched_intents", return_value=[]):
                _prompt, tools = agent._route_prompt(request)
            self.assertEqual(tools[0].name, "media_control")

    def test_close_music_tab_is_a_registered_dispatchable_tool(self):
        self.assertIn("close_music_tab", agent.VALID_TOOLS)
        with mock.patch.object(agent, "close_music_tab",
                               return_value="Closed the music.youtube.com tab.") as close:
            self.assertEqual(
                agent.execute_tool({"tool": "close_music_tab", "input": ""}),
                "Closed the music.youtube.com tab.")
        close.assert_called_once_with()

    # ------------------------------------------------------------------
    # queue_on_ytm
    # ------------------------------------------------------------------
    def test_queue_on_ytm_searches_and_clicks_add_to_queue(self):
        music = FakeQueuePage("https://music.youtube.com/")
        chromium = FakeChromium(FakeBrowser([FakeContext([music])]))
        self.patch_cdp(chromium)

        result = agent.queue_on_ytm("the strokes")

        self.assertIn("Queued", result)
        self.assertEqual(
            music.goto_calls[0][0],
            "https://music.youtube.com/search?q=the+strokes")
        self.assertEqual(music.goto_calls[0][1]["wait_until"], "domcontentloaded")
        self.assertEqual(
            music.clicks,
            ["ytmusic-menu-renderer", "text=Add to queue"])
        self.assertIn("ytmusic-responsive-list-item-renderer", music.waits)
        self.assertIn("text=Added to queue", music.waits)
        self.assertEqual(
            chromium.calls, [(agent.OPERA_CDP_ENDPOINT, 5000)])

    def test_queue_on_ytm_reports_dom_changes_as_errors(self):
        for fail_selector in ("text=Add to queue", "text=Added to queue"):
            with self.subTest(fail_selector=fail_selector):
                music = FakeQueuePage("https://music.youtube.com/",
                                      fail_selector=fail_selector)
                self.patch_cdp(FakeChromium(FakeBrowser([FakeContext([music])])))
                result = agent.queue_on_ytm("the strokes")
                self.assertTrue(result.startswith("ERROR:"), result)
                self.assertNotIn("Queued '", result)

    def test_queue_on_ytm_reports_cdp_failure(self):
        self.patch_cdp(FakeChromium(error=RuntimeError("connection refused")))
        result = agent.queue_on_ytm("the strokes")
        self.assertTrue(result.startswith("ERROR:"))
        self.assertIn("connection refused", result)

    def test_queue_on_ytm_rejects_empty_song(self):
        with mock.patch.object(
                agent, "_cdp_playwright",
                side_effect=AssertionError("CDP must not be used")):
            result = agent.queue_on_ytm("  ")
        self.assertTrue(result.startswith("ERROR:"))

    def test_queue_on_ytm_is_a_registered_dispatchable_tool(self):
        self.assertIn("queue_on_ytm", agent.VALID_TOOLS)
        with mock.patch.object(agent, "queue_on_ytm",
                               return_value="Queued 'x' on YouTube Music.") as queue:
            self.assertEqual(
                agent.execute_tool({"tool": "queue_on_ytm", "input": "x"}),
                "Queued 'x' on YouTube Music.")
        queue.assert_called_once_with("x")

    def test_router_exposes_queue_on_ytm_for_queue_requests(self):
        with mock.patch.object(
                agent, "_classify_unmatched_intents", return_value=[]):
            _prompt, tools = agent._route_prompt("queue the strokes")
        self.assertIn("queue_on_ytm", [tool.name for tool in tools])

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
        self.assertIn("Requested playback", result)
        self.assertNotIn("Playing", result)

    def test_play_on_youtube_reports_a_new_cdp_tab(self):
        with mock.patch.object(
            agent.requests, "get", return_value=self.search_response(),
        ), mock.patch.object(agent, "_play_in_music_tab", return_value="opened"):
            result = agent.play_on_youtube("the strokes")

        self.assertIn("new", result)
        self.assertIn("Requested playback", result)
        self.assertNotIn("Playing", result)

    def test_play_on_youtube_falls_back_when_cdp_is_unavailable(self):
        with mock.patch.object(
            agent.requests, "get", return_value=self.search_response(),
        ), mock.patch.object(
            agent, "_play_in_music_tab", return_value=None,
        ), mock.patch.object(
            agent, "_open_in_new_tab",
            return_value="Opened in Opera: Requested playback: x",
        ) as fallback:
            result = agent.play_on_youtube("the strokes")

        fallback.assert_called_once_with(
            "https://music.youtube.com/watch?v=abc12345678&autoplay=1",
            label="Requested playback: the strokes",
        )
        self.assertEqual(result, "Opened in Opera: Requested playback: x")

    def test_play_on_youtube_rejects_empty_query(self):
        with mock.patch.object(agent.requests, "get") as get:
            result = agent.play_on_youtube("  ")
        get.assert_not_called()
        self.assertTrue(result.startswith("ERROR:"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
