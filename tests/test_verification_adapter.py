"""Tests for the host adapter around verification_engine.

Run: python -m unittest tests.test_verification_adapter -v
"""
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from verification_engine import ERROR_REPLY


class AdapterTests(unittest.TestCase):
    def setUp(self):
        import verification_adapter as adapter
        self.adapter = adapter

    def allowed(self, executions, reply):
        engine = self.adapter.build_engine(executions)
        return self.adapter.reply_is_supported(engine, reply)

    def test_no_tools_cannot_support_mutating_claims(self):
        for reply in (
            "I translated the logs.",
            "I saved the file.",
            "Done.",
            "__Done__",
            "Translation completed.",
            "The script has been run.",
        ):
            with self.subTest(reply=reply):
                self.assertFalse(self.allowed([], reply))

    def test_successful_write_supports_save_claim(self):
        self.assertTrue(self.allowed([("write_file", "notes.txt|hi", "Wrote 12 bytes")], "I saved the file."))
        self.assertTrue(self.allowed([("write_file", "notes.txt|hi", "Wrote 12 bytes")], "Done."))

    def test_successful_write_does_not_support_translation_claim(self):
        self.assertFalse(self.allowed(
            [("write_file", "notes.txt|hi", "Wrote 12 bytes")], "I translated the logs."))

    def test_successful_execution_supports_run_not_translation(self):
        executions = [("execute_python", "print(1)", "hello")]
        self.assertTrue(self.allowed(executions, "I ran the script."))
        self.assertTrue(self.allowed(executions, "The script has been run."))
        self.assertFalse(self.allowed(executions, "I translated the logs."))

    def test_failed_or_empty_tool_output_is_not_evidence(self):
        for output in ("ERROR: access denied", "", "   \n", None):
            with self.subTest(output=output):
                self.assertFalse(self.allowed(
                    [("write_file", "notes.txt|hi", output)], "I saved the file."))

    def test_non_mutating_claims_are_out_of_scope(self):
        self.assertTrue(self.allowed([("read_file", "notes.txt", "contents")], "I read the file."))
        self.assertTrue(self.allowed([], "I read the file."))
        self.assertFalse(self.allowed([("read_file", "notes.txt", "contents")], "Done."))

    def test_out_of_scope_claim_cannot_smuggle_a_generic_done(self):
        self.assertFalse(self.allowed([], "I read it. Done."))
        self.assertTrue(self.allowed([("write_file", "notes.txt|hi", "ok")], "I read it. Done."))

    def test_specific_claim_borrows_only_matching_evidence(self):
        executions = [("send_telegram_message", "me|hi", "Message sent")]
        self.assertTrue(self.allowed(executions, "I sent the message."))
        self.assertFalse(self.allowed(executions, "I deleted the message."))

    def test_unknown_tool_name_cannot_supply_evidence(self):
        self.assertFalse(self.allowed([("search_web", "query", "results")], "I published the report."))

    def test_underscore_emphasis_is_detected(self):
        self.assertFalse(self.allowed([], "I __saved__ it."))

    def test_conditional_tool_evidence_matches_the_operation(self):
        self.assertTrue(self.allowed(
            [("skill_manage", "create|demo|body", "Created skill")], "I created the skill."))
        self.assertFalse(self.allowed(
            [("skill_manage", "delete|demo|", "Deleted skill")], "I created the skill."))
        self.assertTrue(self.allowed(
            [("skill_manage", "delete|demo|", "Deleted skill")], "I deleted the skill."))
        self.assertTrue(self.allowed(
            [("set_name", "Bender", "Renamed to Bender")], "I renamed you."))
        self.assertTrue(self.allowed(
            [("todo", "remove: 0", "Removed")], "I removed the todo."))
        self.assertFalse(self.allowed(
            [("todo", "list", "1. buy milk")], "I removed the todo."))
        self.assertTrue(self.allowed(
            [("media_control", "stop", "Stopped")], "I stopped the music."))
        self.assertFalse(self.allowed(
            [("media_control", "next", "Skipped")], "I stopped the music."))
        self.assertTrue(self.allowed(
            [("watch_folder", "downloads", "Watching")], "I started watching it."))

    def test_engine_fault_fails_open(self):
        with mock.patch.object(self.adapter.traceback, "print_exc"):
            return self._assert_fault_opens()

    def _assert_fault_opens(self):
        class BrokenEngine:
            def filter_reply(self, reply):
                raise RuntimeError("boom")

            def claimed_actions(self, reply):
                raise RuntimeError("boom")

        self.assertTrue(self.adapter.reply_is_supported(BrokenEngine(), "I saved it."))

    def test_rejection_message_is_the_requested_text(self):
        self.assertEqual(
            self.adapter.RETRY_MESSAGE,
            "ERROR: Your claim was rejected by the verification engine because you "
            "did not actually run the tool. Try again.",
        )
        self.assertEqual(self.adapter.ERROR_REPLY, ERROR_REPLY)


if __name__ == "__main__":
    unittest.main()
