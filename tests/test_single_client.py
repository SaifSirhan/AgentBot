"""Single-client DeepSeek: no provider chain, no failover, no second keys.

AgentBot talks to exactly one brain — deepseek-flash at api.deepseek.com,
which serves text, vision and search/fetch natively. These tests pin the
removal so a provider cannot creep back in.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import agent  # noqa: E402
import config  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REMOVED_PROVIDERS = (
    "meta", "groq", "gemini", "openrouter", "cerebras", "mistral",
    "cloudflare", "cohere", "huggingface", "ollama", "openai",
)
REMOVED_KEYS = (
    "META_AI_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY",
    "CEREBRAS_API_KEY", "MISTRAL_API_KEY", "CLOUDFLARE_API_KEY",
    "CLOUDFLARE_ACCOUNT_ID", "COHERE_API_KEY", "HUGGINGFACE_API_KEY",
    "OPENAI_API_KEY",
)


def _deepseek_reply(content="ok", finish="stop", tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": finish}],
        "usage": {"total_tokens": 1},
    }


class ProviderChainTests(unittest.TestCase):
    def setUp(self):
        # Cooldown state is module-global: an earlier test that simulated a
        # 5xx would make the admission gate skip deepseek here.
        saved = dict(agent._brain_state)
        agent._brain_state.clear()
        self.addCleanup(lambda: (agent._brain_state.clear(),
                                  agent._brain_state.update(saved)))

    def test_no_provider_functions_survive_on_agent(self):
        for name in REMOVED_PROVIDERS:
            with self.subTest(provider=name):
                self.assertFalse(hasattr(agent, f"_call_{name}"),
                                 f"agent._call_{name} must be deleted")

    def test_dispatch_has_no_branch_for_removed_providers(self):
        for name in REMOVED_PROVIDERS:
            with self.subTest(provider=name):
                self.assertEqual(
                    agent._dispatch_call(name, "p", False, None),
                    f"Error: unknown brain '{name}'")

    def test_brain_priority_is_deepseek_only(self):
        self.assertEqual(list(agent.BRAIN_PRIORITY), ["deepseek"])

    def test_model_constant_is_deepseek_flash(self):
        self.assertEqual(agent.DEEPSEEK_MODEL, "deepseek-flash")
        self.assertEqual(agent.MODEL, "deepseek-flash")

    def test_removed_provider_attributes_are_gone_from_agent(self):
        for name in REMOVED_PROVIDERS:
            with self.subTest(provider=name):
                self.assertFalse(hasattr(agent, f"{name.upper()}_MODEL"),
                                 f"agent.{name.upper()}_MODEL must be deleted")

    def test_no_provider_key_reads_survive_on_agent(self):
        for key in REMOVED_KEYS:
            with self.subTest(key=key):
                self.assertFalse(hasattr(agent, key), f"agent.{key} must be deleted")

    def test_config_defaults_drop_provider_keys_and_priority(self):
        for key in REMOVED_KEYS + ("AGENT_BRAIN_PRIORITY", "OLLAMA_URL",
                                   "OLLAMA_MODEL"):
            with self.subTest(key=key):
                self.assertNotIn(key, config.DEFAULTS)

    def test_config_defaults_keep_deepseek(self):
        self.assertIn("DEEPSEEK_API_KEY", config.DEFAULTS)

    def test_force_provider_cannot_select_a_second_client(self):
        """The kwarg stays for existing callers; every value routes to DeepSeek."""
        with mock.patch.object(agent, "DEEPSEEK_API_KEY", "k"), \
             mock.patch.object(agent.requests, "post",
                               return_value=mock.Mock(status_code=200,
                                                      json=lambda: _deepseek_reply("hi"))) as post:
            self.assertEqual(agent._call_llm("p", force_provider="groq"), "hi")
        self.assertEqual(post.call_args[0][0],
                         "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(agent._LAST_PROVIDER, "deepseek")


class CompactorClientTests(unittest.TestCase):
    def test_compactor_no_longer_pins_a_fast_provider(self):
        import context_compactor
        with mock.patch.object(agent, "_call_llm",
                               return_value="summary") as call:
            self.assertEqual(context_compactor._ask_llm("prompt"), "summary")
        self.assertEqual(call.call_count, 1)
        self.assertNotIn("force_provider", call.call_args.kwargs)


class TelegramServiceClientTests(unittest.TestCase):
    def test_telegram_service_supports_only_deepseek(self):
        import telegram_service
        self.assertEqual(set(telegram_service.SUPPORTED_LLM_PROVIDERS),
                         {"deepseek"})
        self.assertEqual(tuple(telegram_service.DEFAULT_LLM_PRIORITY),
                         ("deepseek",))

    def test_telegram_service_has_no_gemini_or_ollama_branch(self):
        import telegram_service
        for name in ("_call_gemini_messages", "_build_gemini_payload",
                     "_call_ollama_messages", "GEMINI_MODEL"):
            with self.subTest(symbol=name):
                self.assertFalse(hasattr(telegram_service, name),
                                 f"telegram_service.{name} must be deleted")

    def test_telegram_fails_gracefully_when_deepseek_fails(self):
        """No secondary provider: an error string, not an exception."""
        import telegram_service
        with mock.patch.object(telegram_service, "_load_raw_config",
                               return_value={"DEEPSEEK_API_KEY": "k"}), \
             mock.patch.object(telegram_service.requests, "post",
                               return_value=mock.Mock(
                                   status_code=503, text="down")):
            reply = telegram_service.call_isolated_llm(
                [{"role": "user", "content": "summarize this"}])
        self.assertTrue(str(reply).startswith("Error:"), reply)
        self.assertIn("DeepSeek", str(reply))

    def test_local_only_refuses_the_cloud_client(self):
        """TG_DOC_LOCAL_ONLY is a promise document text never leaves the
        machine. With no local model it must refuse, not summarize remotely."""
        import telegram_service
        reply = telegram_service.call_isolated_llm(
            [{"role": "user", "content": "private document text"}],
            local_only=True)
        self.assertTrue(str(reply).startswith("Error:"), reply)

    def test_local_only_priority_is_rejected_at_config_time(self):
        import telegram_service
        with self.assertRaises(telegram_service.TelegramDocConfigError):
            telegram_service._parse_priority(None, True)


class SearchClientTests(unittest.TestCase):
    """DeepSeek search/fetch is the only web-retrieval path."""

    def test_serp_search_tool_is_gone(self):
        self.assertNotIn("serp_search", agent.VALID_TOOLS)

    def test_serp_search_is_not_dispatched(self):
        result = agent.execute_tool({"tool": "serp_search", "input": "q"})
        self.assertTrue(str(result).startswith("ERROR:"), result)

    def test_router_never_pins_a_second_search_backend(self):
        source = open(os.path.join(ROOT, "prompt_router.py"),
                      encoding="utf-8").read()
        self.assertNotIn("serp_search", source)

    def test_serpapi_tool_module_is_not_shipped(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, "serpapi_tool.py")))

    def test_config_has_no_serpapi_key(self):
        self.assertNotIn("SERPAPI_KEY", config.DEFAULTS)
        source = open(os.path.join(ROOT, "agent_gui_qt.py"),
                      encoding="utf-8").read()
        self.assertNotIn("SERPAPI_KEY", source)

    def test_crawl_keys_stay_because_crawl_managed_uses_them(self):
        """Not inert: crawl_managed.py reads both to scrape via Crawlbase or
        Firecrawl. Purging them would break a live tool, so they stay."""
        self.assertIn("CRAWLBASE_KEY", config.DEFAULTS)
        self.assertIn("FIRECRAWL_KEY", config.DEFAULTS)
        import crawl_managed
        self.assertTrue(callable(crawl_managed.crawl_managed))


class GuiSettingsClientTests(unittest.TestCase):
    def test_gui_settings_expose_no_removed_provider_keys(self):
        source = open(os.path.join(ROOT, "agent_gui_qt.py"),
                      encoding="utf-8").read()
        for key in REMOVED_KEYS + ("AGENT_BRAIN_PRIORITY",):
            with self.subTest(key=key):
                self.assertNotIn(key, source)

    def test_gui_settings_still_expose_deepseek(self):
        source = open(os.path.join(ROOT, "agent_gui_qt.py"),
                      encoding="utf-8").read()
        self.assertIn('"DEEPSEEK_API_KEY"', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
