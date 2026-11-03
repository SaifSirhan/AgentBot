"""Read-only tools are evidence, and reported speech is not a claim.

Two live failures, one commit:

1. A search-only turn has no mutating tool, so `bool(evidenced)` was False and
   any completion-phrased reply ("Done — I searched the web") was blocked.
2. A grounded news reply said a third party "published" a paper. The engine
   matches bare past-tense verbs with no subject check, so the agent was
   blamed for DeepSeek's action.

Both are fixed in the adapter, which is where AgentBot's turn log meets the
engine. The engine's mutation guards stay exactly as strict for first-person
claims.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import verification_adapter as va  # noqa: E402

SEARCH_OUTPUT = ("Real search results (use these EXACT URLs):\n"
                 "DeepSeek Drops Enhanced Coding Agent -> https://example.com/1\n"
                 "  The DeepSeek Harness team published a paper on Super-agent.")

NEWS_REPLY = ('DeepSeek\'s been busy. On Oct 31 their "DeepSeek Harness" team '
              'published a paper on a new Super-agent architecture. Done and '
              'verified.')


def _supported(executions, reply):
    return va.reply_is_supported(va.build_engine(executions), reply)


class ReadOnlyEvidenceTests(unittest.TestCase):
    def test_a_successful_search_evidences_a_search_claim(self):
        self.assertTrue(_supported(
            [("search_web", "deepseek news", SEARCH_OUTPUT)],
            "Done — I searched the web and found the announcement."))

    def test_read_only_evidence_is_recorded_as_a_read_action(self):
        engine = va.build_engine([("search_web", "q", SEARCH_OUTPUT)])
        evidenced = set()
        for execution in engine.executions:
            if execution.success and execution.has_output:
                evidenced.update(execution.verified_actions)
        self.assertIn(va.READ_ACTION, evidenced)

    def test_registry_read_only_tools_are_included(self):
        # news is registered read_only; rag_search is a legacy name.
        self.assertTrue(va.tool_is_read_only("news"))
        self.assertTrue(va.tool_is_read_only("rag_search"))

    def test_a_mutating_tool_is_not_read_only(self):
        self.assertFalse(va.tool_is_read_only("write_file"))
        self.assertFalse(va.tool_is_read_only("telegram_user_send"))

    def test_a_failed_search_supports_nothing(self):
        self.assertFalse(_supported(
            [("search_web", "deepseek news", "ERROR: DeepSeek HTTP 503")],
            "Done — I searched the web and found the announcement."))

    def test_read_only_evidence_does_not_support_a_first_person_mutation(self):
        """The whole point of the guard: a search is not a send."""
        self.assertFalse(_supported(
            [("search_web", "q", SEARCH_OUTPUT)],
            "Done — I sent the email to the client."))

    def test_unregistered_unknown_tool_still_supplies_no_evidence(self):
        engine = va.build_engine([("some_legacy_tool", "q", "output")])
        self.assertEqual(list(engine.executions), [])


class ReportedSpeechTests(unittest.TestCase):
    def test_a_third_party_action_in_the_source_is_not_the_agents_claim(self):
        self.assertTrue(_supported(
            [("search_web", "deepseek news", SEARCH_OUTPUT)], NEWS_REPLY))

    def test_the_same_verb_in_first_person_is_still_blocked(self):
        """"published" appears in the search results, but "I published" is the
        agent claiming an action no tool performed."""
        self.assertFalse(_supported(
            [("search_web", "q", SEARCH_OUTPUT)],
            "Done — I published the site to production."))

    def test_reported_action_without_a_source_is_still_blocked(self):
        """No read-only output containing the verb, so no exemption."""
        self.assertFalse(_supported(
            [("search_web", "q", "Real search results:\nExample -> https://x")],
            NEWS_REPLY))

    def test_reportage_exemption_needs_a_successful_read_only_tool(self):
        self.assertFalse(_supported([], NEWS_REPLY))


if __name__ == "__main__":
    unittest.main(verbosity=2)
