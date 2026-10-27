"""Tests for context compaction. No LLM calls: _ask_llm is always patched.

Run: python -m unittest tests.test_context_compactor -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import context_compactor as cc


def messages(count, size=400):
    return [f"User: message {i} " + "x" * size for i in range(count)]


class CompactionTests(unittest.TestCase):
    def test_estimate_tokens(self):
        self.assertEqual(cc.estimate_tokens(["a" * 40]), 11)

    def test_under_budget_is_untouched(self):
        history = ["User: hi", "Agent: hello"]
        with mock.patch.object(cc, "_ask_llm") as ask:
            out = cc.compact_history(history, budget=1000)

        self.assertIs(out, history)
        self.assertEqual(history, ["User: hi", "Agent: hello"])
        ask.assert_not_called()

    def test_oldest_are_summarized_and_recent_kept_verbatim(self):
        history = messages(15)
        original_tail = history[-cc.KEEP_RECENT:]
        with mock.patch.object(cc, "_ask_llm", return_value="Brief text"):
            out = cc.compact_history(history, budget=100)

        self.assertIs(out, history)
        self.assertEqual(history[0], "[SYSTEM SUMMARY] Brief text")
        self.assertEqual(history[1:], original_tail)
        self.assertEqual(len(history), cc.KEEP_RECENT + 1)

    def test_existing_summary_is_merged(self):
        history = ["[SYSTEM SUMMARY] old facts"] + messages(8)
        captured = {}

        def fake(prompt):
            captured["prompt"] = prompt
            return "new brief"

        with mock.patch.object(cc, "_ask_llm", side_effect=fake):
            cc.compact_history(history, budget=10)

        self.assertEqual(history[0], "[SYSTEM SUMMARY] new brief")
        self.assertIn("Previous summary:\nold facts", captured["prompt"])
        self.assertEqual(len(history), 6)

    def test_orders_wait_when_too_few_old_messages(self):
        history = messages(7)
        with mock.patch.object(cc, "_ask_llm") as ask:
            cc.compact_history(history, budget=10)

        ask.assert_not_called()
        self.assertEqual(len(history), 7)

    def test_llm_failure_keeps_every_message(self):
        history = messages(15)
        with mock.patch.object(cc, "_ask_llm", return_value=None):
            cc.compact_history(history, budget=10)

        self.assertEqual(len(history), 15)
        self.assertFalse(str(history[0]).startswith(cc.SUMMARY_PREFIX))

    def test_summary_prefix_is_not_doubled(self):
        history = messages(15)
        with mock.patch.object(cc, "_ask_llm",
                               return_value="[SYSTEM SUMMARY] already"):
            cc.compact_history(history, budget=10)

        self.assertEqual(history[0], "[SYSTEM SUMMARY] already")

    def test_prompt_carries_the_full_transcript_and_path_rules(self):
        path = r"C:\Users\USER\Downloads\Log Intern.docx"
        history = [f"User: edit {path}"] + messages(14)
        captured = {}

        def fake(prompt):
            captured["prompt"] = prompt
            return "brief"

        with mock.patch.object(cc, "_ask_llm", side_effect=fake):
            cc.compact_history(history, budget=10)

        self.assertIn(path, captured["prompt"])
        self.assertIn("file path", captured["prompt"])
        self.assertEqual(captured["prompt"].count(path), 1)

    def test_confirmed_paths_survive_a_lossy_summary(self):
        path = r"C:\Users\USER\Downloads\Log Intern.docx"
        history = [f"User: please edit {path}"] + messages(14)

        with mock.patch.object(cc, "_ask_llm", return_value="brief without paths"):
            cc.compact_history(history, budget=10)

        self.assertIn("Confirmed paths:", history[0])
        self.assertIn(path, history[0])

    def test_path_extraction_strips_punctuation_and_dedupes(self):
        text = r"Use C:\temp\a.docx, then C:\temp\a.docx."

        self.assertEqual(cc._confirmed_paths([text]), [r"C:\temp\a.docx"])

    def test_history_window_keeps_the_summary_in_view(self):
        history = ["[SYSTEM SUMMARY] s"] + [f"m{i}" for i in range(30)]

        window = cc.history_window(history, limit=20)

        self.assertEqual(len(window), 20)
        self.assertEqual(window[0], "[SYSTEM SUMMARY] s")
        self.assertEqual(window[-1], "m29")

    def test_history_window_without_summary_takes_the_tail(self):
        history = [f"m{i}" for i in range(30)]

        window = cc.history_window(history, limit=20)

        self.assertEqual(window, history[-20:])


class SummarizerRoutingTests(unittest.TestCase):
    def test_fast_provider_is_preferred(self):
        import agent

        with mock.patch("config.get",
                        side_effect=lambda k, d="": "key" if k == "GROQ_API_KEY" else ""), \
                mock.patch.object(agent, "_call_llm", return_value="brief") as call:
            out = cc._ask_llm("prompt")

        self.assertEqual(out, "brief")
        self.assertEqual(call.call_args.kwargs.get("force_provider"), "groq")

    def test_falls_back_to_the_normal_chain(self):
        import agent

        with mock.patch("config.get",
                        side_effect=lambda k, d="": "key" if k == "GROQ_API_KEY" else ""), \
                mock.patch.object(agent, "_call_llm",
                                  side_effect=["Error: nope", "brief"]) as call:
            out = cc._ask_llm("prompt")

        self.assertEqual(out, "brief")
        self.assertEqual(call.call_count, 2)
        self.assertNotIn("force_provider", call.call_args.kwargs)


class TurnIntegrationTests(unittest.TestCase):
    def test_turn_compacts_before_planning_and_sees_the_summary(self):
        import agent

        history = messages(15)
        contexts = []

        def fake_planner(next_input, context):
            contexts.append(context)
            return {"tool": "chat", "input": "ok"}, None

        with mock.patch.object(cc, "_ask_llm", return_value="BRIEF"), \
                mock.patch.object(agent, "get_valid_action", side_effect=fake_planner), \
                mock.patch.object(agent, "execute_tool", return_value="ok"), \
                mock.patch("rag_tool.search_documents", return_value=""):
            agent._run_agent_turn_impl("hello", history)

        self.assertEqual(history[0], "[SYSTEM SUMMARY] BRIEF")
        self.assertEqual(len(history), cc.KEEP_RECENT + 1)
        self.assertIn("[SYSTEM SUMMARY] BRIEF", contexts[0])


if __name__ == "__main__":
    unittest.main()
