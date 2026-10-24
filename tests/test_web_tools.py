"""Tests for DeepSeek Search/Fetch tools and their legacy fallbacks.

Run: python -m pytest tests/test_web_tools.py -v
Or:  python tests/test_web_tools.py
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent

SEARCH_URL = "https://api.deepseek.com/v1/tools/search"
FETCH_URL = "https://api.deepseek.com/v1/tools/fetch"
TAVILY_URL = "https://api.tavily.com/search"


class FakeResponse:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self._payload = {} if payload is None else payload
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        return self._payload


class SearchTests(unittest.TestCase):
    def test_deepseek_search_formats_results(self):
        payload = {"results": [
            {"title": "Nvidia news", "url": "https://example.com/n",
             "summary": "New GPU announced.", "snippet": "ignored",
             "content": "<html>ignored</html>"},
            {"title": "Other", "url": "https://example.com/o",
             "summary": "", "snippet": "Snippet used", "content": ""},
            {"title": "", "url": "https://example.com/missing-title"},
        ]}
        with mock.patch.object(agent, "DEEPSEEK_API_KEY", "test-key"), \
             mock.patch.object(agent.requests, "post",
                               return_value=FakeResponse(200, payload)) as post:
            out = agent.search_web("nvidia")

        self.assertTrue(out.startswith("Real search results"), out)
        self.assertIn("Nvidia news -> https://example.com/n", out)
        self.assertIn("New GPU announced.", out)
        self.assertIn("Other -> https://example.com/o", out)
        self.assertIn("Snippet used", out)
        self.assertNotIn("<html>", out)
        self.assertNotIn("missing-title", out)
        self.assertEqual(agent.last_search_result, out)

        args, kwargs = post.call_args
        self.assertEqual(args[0], SEARCH_URL)
        self.assertEqual(kwargs["json"],
                         {"query": "nvidia", "count": 5, "need_content": True})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")

    def test_query_is_capped_at_api_limit(self):
        with mock.patch.object(agent, "DEEPSEEK_API_KEY", "test-key"), \
             mock.patch.object(agent.requests, "post",
                               return_value=FakeResponse(200, {"results": [
                                   {"title": "T", "url": "https://e.example",
                                    "summary": "S"}]})) as post:
            agent.search_web("q" * 300)

        self.assertEqual(len(post.call_args.kwargs["json"]["query"]), 128)

    def test_falls_back_to_legacy_on_api_error(self):
        legacy = {"results": [{"title": "Legacy", "url": "https://legacy.example",
                               "content": "Old backend"}]}
        with mock.patch.object(agent, "DEEPSEEK_API_KEY", "test-key"), \
             mock.patch.object(agent.requests, "post", side_effect=[
                 FakeResponse(500, text="deepseek down"),
                 FakeResponse(200, legacy),
             ]) as post:
            out = agent.search_web("nvidia")

        self.assertIn("Legacy -> https://legacy.example", out)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].args[0], SEARCH_URL)
        self.assertEqual(post.call_args_list[1].args[0], TAVILY_URL)

    def test_empty_results_fall_back(self):
        legacy = {"results": [{"title": "Legacy", "url": "https://legacy.example",
                               "content": "Found"}]}
        with mock.patch.object(agent, "DEEPSEEK_API_KEY", "test-key"), \
             mock.patch.object(agent.requests, "post", side_effect=[
                 FakeResponse(200, {"results": []}),
                 FakeResponse(200, legacy),
             ]):
            out = agent.search_web("nvidia")
        self.assertIn("Legacy -> https://legacy.example", out)

    def test_missing_key_skips_deepseek(self):
        legacy = {"results": [{"title": "Legacy", "url": "https://legacy.example",
                               "content": "Found"}]}
        with mock.patch.object(agent, "DEEPSEEK_API_KEY", ""), \
             mock.patch.object(agent.requests, "post",
                               return_value=FakeResponse(200, legacy)) as post:
            out = agent.search_web("nvidia")

        self.assertIn("Legacy -> https://legacy.example", out)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[0], TAVILY_URL)

    def test_both_backends_failing_returns_one_error(self):
        with mock.patch.object(agent, "DEEPSEEK_API_KEY", "test-key"), \
             mock.patch.object(agent.requests, "post", side_effect=[
                 FakeResponse(500, text="deepseek down"),
                 FakeResponse(429, text="tavily limited"),
             ]):
            out = agent.search_web("nvidia")

        self.assertTrue(out.startswith("ERROR:"), out)
        self.assertIn("DeepSeek", out)
        self.assertIn("Tavily", out)
        self.assertEqual(agent.show_last_result(), out)


class FetchTests(unittest.TestCase):
    def test_deepseek_fetch_returns_markdown(self):
        payload = {"title": "Example article",
                   "content": "# Heading\n\nArticle **body**."}
        with mock.patch.object(agent, "DEEPSEEK_API_KEY", "test-key"), \
             mock.patch.object(agent.requests, "post",
                               return_value=FakeResponse(200, payload)) as post:
            out = agent.web_fetch("example.com/article")

        self.assertIn("[Fetched: Example article]", out)
        self.assertIn("https://example.com/article", out)
        self.assertIn("# Heading", out)
        self.assertIn("**body**", out)
        args, kwargs = post.call_args
        self.assertEqual(args[0], FETCH_URL)
        self.assertEqual(kwargs["json"], {"url": "https://example.com/article"})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")

    def test_fetch_truncates_large_markdown(self):
        payload = {"title": "Big", "content": "x" * (agent.MAX_FETCH_CHARS + 500)}
        with mock.patch.object(agent, "DEEPSEEK_API_KEY", "test-key"), \
             mock.patch.object(agent.requests, "post",
                               return_value=FakeResponse(200, payload)):
            out = agent.web_fetch("https://example.com/big")

        self.assertIn("[... truncated]", out)
        self.assertLess(len(out), agent.MAX_FETCH_CHARS + 200)

    def test_fetch_falls_back_to_local_extraction(self):
        with mock.patch.object(agent, "DEEPSEEK_API_KEY", "test-key"), \
             mock.patch.object(agent.requests, "post",
                               side_effect=agent.requests.exceptions.Timeout("slow")), \
             mock.patch.object(agent, "_fetch_clean_legacy",
                               return_value="[Clean text from https://example.com]\n\nLocal") as legacy:
            out = agent.web_fetch("https://example.com")

        self.assertIn("Local", out)
        self.assertIn("used local extraction", out)
        legacy.assert_called_once_with("https://example.com")

    def test_missing_key_uses_local_extraction(self):
        with mock.patch.object(agent, "DEEPSEEK_API_KEY", ""), \
             mock.patch.object(agent.requests, "post") as post, \
             mock.patch.object(agent, "_fetch_clean_legacy",
                               return_value="[Clean text from x]\n\nLocal"):
            out = agent.web_fetch("https://example.com")

        self.assertIn("Local", out)
        post.assert_not_called()

    def test_rejects_non_http_urls(self):
        with mock.patch.object(agent.requests, "post") as post:
            out = agent.web_fetch("file:///C:/secret.txt")
        self.assertTrue(out.startswith("ERROR:"), out)
        post.assert_not_called()

    def test_both_fetch_backends_failing_returns_error(self):
        with mock.patch.object(agent, "DEEPSEEK_API_KEY", "test-key"), \
             mock.patch.object(agent.requests, "post",
                               return_value=FakeResponse(500, text="deepseek down")), \
             mock.patch.object(agent, "_fetch_clean_legacy",
                               return_value="ERROR: no readable text"):
            out = agent.web_fetch("https://example.com")

        self.assertTrue(out.startswith("ERROR:"), out)
        self.assertIn("DeepSeek", out)
        self.assertIn("no readable text", out)

    def test_empty_markdown_falls_back(self):
        with mock.patch.object(agent, "DEEPSEEK_API_KEY", "test-key"), \
             mock.patch.object(agent.requests, "post",
                               return_value=FakeResponse(200, {"title": "T", "content": ""})), \
             mock.patch.object(agent, "_fetch_clean_legacy",
                               return_value="[Clean text from x]\n\nLocal"):
            out = agent.web_fetch("https://example.com")
        self.assertIn("Local", out)


class RegistryTests(unittest.TestCase):
    def test_web_fetch_replaces_fetch_clean(self):
        self.assertIn("web_fetch", agent.VALID_TOOLS)
        self.assertNotIn("fetch_clean", agent.VALID_TOOLS)
        self.assertEqual(agent.TOOL_DESCRIPTIONS.count("web_fetch(input)"), 1)
        self.assertNotIn("fetch_clean(input)", agent.TOOL_DESCRIPTIONS)

    def test_execute_tool_dispatches(self):
        with mock.patch.object(agent, "web_fetch", return_value="FETCHED") as fetch, \
             mock.patch.object(agent, "search_web", return_value="SEARCHED") as search:
            self.assertEqual(agent.execute_tool(
                {"tool": "web_fetch", "input": "https://example.com"}), "FETCHED")
            self.assertEqual(agent.execute_tool(
                {"tool": "search_web", "input": "nvidia"}), "SEARCHED")
        fetch.assert_called_once_with("https://example.com")
        search.assert_called_once_with("nvidia")

    def test_group_mode_blocks_web_fetch(self):
        self.assertNotIn("web_fetch", agent.SAFE_GROUP_TOOLS)
        with mock.patch.object(agent, "GROUP_MODE", True):
            out = agent.execute_tool(
                {"tool": "web_fetch", "input": "https://example.com"})
        self.assertIn("disabled in group chats", out)

    def test_prompt_budget(self):
        prompt = agent._build_plan_prompt("hello", "")
        self.assertLess(len(prompt) / 4, 7800,
                        f"prompt is {len(prompt)} chars")


if __name__ == "__main__":
    unittest.main(verbosity=2)
