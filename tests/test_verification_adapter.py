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

    def test_structured_tool_failure_is_not_evidence(self):
        failure = ('{"ok": false, "retryable": false, "error": '
                   '{"code": "TEXT_NOT_FOUND", "message": "missing"}}')
        success = ('{"ok": true, "action": "replace_text", '
                   '"filepath": "report.docx", "changes": 1}')
        self.assertFalse(self.allowed(
            [("edit_docx_safe", '{"filepath": "report.docx"}', failure)],
            "I saved the report."))
        self.assertTrue(self.allowed(
            [("edit_docx_safe", '{"filepath": "report.docx"}', success)],
            "I saved the report."))

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

    def test_engine_fault_fails_closed_and_interruption_propagates(self):
        for error in (RuntimeError("boom"), OSError("boom"), InterruptedError("stop")):
            engine = mock.Mock()
            engine.filter_reply.side_effect = error
            with mock.patch.object(self.adapter.traceback, "print_exc"):
                if isinstance(error, InterruptedError):
                    with self.assertRaises(InterruptedError):
                        self.adapter.reply_is_supported(engine, "I saved it.")
                else:
                    self.assertFalse(self.adapter.reply_is_supported(engine, "I saved it."))

    def test_shared_event_cancels_build_and_reply(self):
        from threading import Event
        stop = Event()
        engine = self.adapter.build_engine([], stop_event=stop)
        stop.set()
        with self.assertRaises(InterruptedError):
            self.adapter.build_engine([], stop_event=stop)
        with self.assertRaises(InterruptedError):
            self.adapter.reply_is_supported(engine, "Hello")
        with self.assertRaises(InterruptedError):
            self.adapter.reply_is_supported(mock.Mock(), "Hello", stop_event=stop)

    def test_engine_event_cancellation_wins_over_filter_fault(self):
        from threading import Event
        stop = Event()
        engine = self.adapter.build_engine([], stop_event=stop)
        def fail(reply):
            stop.set()
            raise OSError("cancelled filter")
        with mock.patch.object(engine, "filter_reply", side_effect=fail), \
                mock.patch.object(self.adapter.traceback, "print_exc"):
            with self.assertRaises(InterruptedError):
                self.adapter.reply_is_supported(engine, "Hello")

    def test_cancel_between_replayed_executions(self):
        from threading import Event
        stop = Event()
        def executions():
            yield ("write_file", "one", "ok")
            stop.set()
            yield ("write_file", "two", "ok")
        with self.assertRaises(InterruptedError):
            self.adapter.build_engine(executions(), stop_event=stop)

    def test_engine_propagates_interruptions_without_success_receipt(self):
        from verification_engine import VerificationEngine, ToolSpec, ToolResult
        from threading import Event
        for phase in ("run", "verify", "set_event"):
            stop = Event()
            def interrupt(*args):
                raise InterruptedError("stop")
            def verify(*args):
                stop.set()
                return True
            engine = VerificationEngine({"write": ToolSpec(
                interrupt if phase == "run" else lambda args: ToolResult(True, "ok"),
                {"save": verify if phase == "set_event" else interrupt},
            )}, stop_event=stop)
            with self.assertRaises(InterruptedError):
                engine.execute_tool("write", {})
            self.assertEqual(engine.executions, ())

    def test_rejection_message_is_the_requested_text(self):
        self.assertEqual(
            self.adapter.RETRY_MESSAGE,
            "ERROR: Your claim was rejected by the verification engine because you "
            "did not actually run the tool. Try again.",
        )
        self.assertEqual(self.adapter.ERROR_REPLY, ERROR_REPLY)


if __name__ == "__main__":
    unittest.main()
