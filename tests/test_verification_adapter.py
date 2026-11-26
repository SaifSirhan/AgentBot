"""Tests for the host adapter around verification_engine.

Run: python -m unittest tests.test_verification_adapter -v
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import python_sandbox
from verification_engine import ERROR_REPLY


class AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox_temp = tempfile.TemporaryDirectory()
        roots = Path(cls.sandbox_temp.name) / "roots"
        roots.mkdir()
        cls.sandbox_env = mock.patch.dict(os.environ, {
            "LOCALAPPDATA": str(Path(cls.sandbox_temp.name) / "local"),
            "PYTHON_SANDBOX_ROOTS": str(roots),
        })
        cls.sandbox_env.start()
        _, _, sandbox_state = python_sandbox._profile_identity()
        state_digest = hashlib.sha256(sandbox_state.encode("utf-8")).hexdigest()[:12]
        profile_name = f"Kestrel.PythonSandbox.Tests.VerificationAdapter.{state_digest}"
        cls.identity_patch = mock.patch.object(
            python_sandbox, "_profile_identity",
            return_value=(profile_name, "S-1-test", sandbox_state))
        cls.identity_patch.start()
        import config
        cls.config_patch = mock.patch.object(
            config, "CONFIG_PATH", str(Path(cls.sandbox_temp.name) / "config.json"))
        cls.memory_patch = mock.patch.object(
            config, "memory_dir", return_value=str(Path(cls.sandbox_temp.name) / "memory"))
        cls.config_patch.start()
        cls.memory_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.memory_patch.stop()
        cls.config_patch.stop()
        cls.identity_patch.stop()
        cls.sandbox_env.stop()
        cls.sandbox_temp.cleanup()

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

    def test_read_only_tools_evidence_read_and_completion_claims(self):
        self.assertTrue(self.allowed([("read_file", "notes.txt", "contents")], "I read the file."))
        self.assertTrue(self.allowed([], "I read the file."))
        # A successful observation proves the turn did something, so "Done."
        # after a real read is a report, not a fabricated completion.
        self.assertTrue(self.allowed([("read_file", "notes.txt", "contents")], "Done."))
        # With nothing executed at all, a bare completion is still a claim.
        self.assertFalse(self.allowed([], "Done."))

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
        with tempfile.TemporaryDirectory(prefix="docx_receipt_") as directory:
            path = Path(directory) / "report.docx"
            path.write_bytes(b"report")
            saved = path.stat()
            success = json.dumps({
                "ok": True,
                "action": "replace_text",
                "filepath": str(path),
                "changes": 1,
                "receipt": {
                    "path": str(path),
                    "size": saved.st_size,
                    "mtime_ns": saved.st_mtime_ns,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                },
            })
            tool_input = json.dumps({"filepath": str(path)})
            self.assertFalse(self.allowed(
                [("edit_docx_safe", tool_input, failure)],
                "I saved the report."))
            self.assertTrue(self.allowed(
                [("edit_docx_safe", tool_input, success)],
                "I saved the report."))

    def test_docx_success_needs_a_receipt_that_matches_the_file(self):
        with tempfile.TemporaryDirectory(prefix="docx_receipt_") as directory:
            path = Path(directory) / "report.docx"
            path.write_bytes(b"report")
            stat_result = path.stat()
            receipt = {
                "path": str(path),
                "size": stat_result.st_size,
                "mtime_ns": stat_result.st_mtime_ns,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            tool_input = json.dumps({"filepath": str(path)})
            success = json.dumps({
                "ok": True,
                "action": "replace_text",
                "filepath": str(path),
                "changes": 1,
                "receipt": receipt,
            })
            no_receipt = json.dumps({
                "ok": True,
                "action": "replace_text",
                "filepath": str(path),
                "changes": 1,
            })
            executions = [("edit_docx_safe", tool_input, success)]

            self.assertFalse(self.allowed(
                [("edit_docx_safe", tool_input, no_receipt)],
                "I saved the report."))
            self.assertTrue(self.allowed(executions, "I saved the report."))

            path.write_bytes(b"changed after the receipt")
            self.assertFalse(self.allowed(executions, "I saved the report."))

    def test_python_file_edit_supports_edit_claim_with_matching_receipt(self):
        import agent

        with tempfile.TemporaryDirectory(prefix="python_receipt_") as directory, \
                mock.patch.dict(os.environ, {"PYTHON_SANDBOX_ROOTS": directory}):
            path = Path(directory) / "report.txt"
            path.write_text("before", encoding="utf-8")
            code = (
                "from pathlib import Path\n"
                f"Path({str(path)!r}).write_text('after', encoding='utf-8')"
            )
            output = agent.execute_python(code)

            self.assertEqual(path.read_text(encoding="utf-8"), "after")
            self.assertTrue(self.allowed(
                [("execute_python", code, output)], "I edited the file."))

    def test_python_receipt_binds_explicit_file_targets(self):
        import agent

        with tempfile.TemporaryDirectory(prefix="python_receipt_") as directory, \
                mock.patch.dict(os.environ, {"PYTHON_SANDBOX_ROOTS": directory}):
            path = Path(directory) / "report.txt"
            path.write_text("before", encoding="utf-8")
            code = (
                "from pathlib import Path\n"
                f"Path({str(path)!r}).write_text('after', encoding='utf-8')"
            )
            output = agent.execute_python(code)
            executions = [("execute_python", code, output)]

            self.assertTrue(self.allowed(executions, "I edited report.txt."))
            self.assertTrue(self.allowed(executions, "I edited the file."))
            self.assertFalse(self.allowed(executions, "I edited other.txt."))
            self.assertFalse(self.allowed(
                executions, "I edited report.txt and notes.txt."))

    def test_python_after_state_failure_invalidates_the_receipt(self):
        with tempfile.TemporaryDirectory(prefix="python_receipt_") as directory:
            path = Path(directory) / "report.txt"
            path.write_text("before", encoding="utf-8")
            before = {os.path.normcase(str(path)): self.adapter.snapshot_python_file(str(path))}
            with mock.patch.object(self.adapter, "_python_file_state",
                                   side_effect=OSError("cannot snapshot")):
                output = self.adapter.append_python_receipts("ok", before)
            self.assertTrue(output.startswith("ERROR:"))
            self.assertNotIn(self.adapter._PYTHON_RECEIPT_MARKER, output)

    def test_python_receipt_size_limit_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="python_receipt_") as directory:
            path = Path(directory) / "report.txt"
            path.write_text("before", encoding="utf-8")
            before = {os.path.normcase(str(path)): self.adapter.snapshot_python_file(str(path))}
            path.write_text("after", encoding="utf-8")
            with mock.patch.object(self.adapter, "_PYTHON_MAX_RECEIPT_BYTES",
                                   128, create=True):
                output = self.adapter.append_python_receipts("ok", before)
            self.assertTrue(output.startswith("ERROR:"))
            self.assertIn("size limit", output)
            self.assertNotIn(self.adapter._PYTHON_RECEIPT_MARKER, output)

    def test_python_receipt_parser_enforces_size_limit(self):
        with tempfile.TemporaryDirectory(prefix="python_receipt_") as directory:
            path = Path(directory) / "report.txt"
            path.write_text("before", encoding="utf-8")
            before = {os.path.normcase(str(path)): self.adapter.snapshot_python_file(str(path))}
            path.write_text("after", encoding="utf-8")
            output = self.adapter.append_python_receipts("ok", before)
            self.assertTrue(self.adapter.verified_python_actions(output))
            with mock.patch.object(self.adapter, "_PYTHON_MAX_RECEIPT_BYTES",
                                   128, create=True):
                self.assertEqual(self.adapter.verified_python_actions(output), frozenset())

    @unittest.skipUnless(os.name == "nt", "Windows junction identity test")
    def test_python_receipt_rejects_reparse_substitution_with_same_content(self):
        with tempfile.TemporaryDirectory(prefix="python_receipt_") as directory:
            base = Path(directory)
            first = base / "first"
            second = base / "second"
            first.mkdir()
            second.mkdir()
            first_file = first / "report.txt"
            second_file = second / "report.txt"
            first_file.write_bytes(b"before")
            second_file.write_bytes(b"before")
            junction = base / "linked"
            created = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(first)],
                capture_output=True, text=True, check=False)
            self.assertEqual(created.returncode, 0, created.stderr)
            linked_file = junction / "report.txt"
            key = os.path.normcase(str(linked_file))
            before = self.adapter.snapshot_python_file(str(linked_file))

            first_file.write_bytes(b"after")
            output = self.adapter.append_python_receipts("ok", {key: before})
            self.assertTrue(self.adapter.verified_python_actions(output))

            second_file.write_bytes(b"after")
            saved = first_file.stat()
            os.utime(second_file, ns=(saved.st_atime_ns, saved.st_mtime_ns))
            os.rmdir(junction)
            created = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(second)],
                capture_output=True, text=True, check=False)
            self.assertEqual(created.returncode, 0, created.stderr)

            self.assertEqual(self.adapter.verified_python_actions(output), frozenset())

    def assert_computed_path_receipt(self, filename, expression):
        import agent

        with tempfile.TemporaryDirectory(prefix="python_receipt_") as directory, \
                mock.patch.dict(os.environ, {"PYTHON_SANDBOX_ROOTS": directory}):
            path = Path(directory) / filename
            path.write_text("before", encoding="utf-8")
            code = (
                "from pathlib import Path\n"
                f"directory = Path({directory!r})\n"
                f"name = {filename!r}\n"
                "prefix = 'report'\n"
                f"{expression}\n"
                "target.write_text('after', encoding='utf-8')"
            )
            output = agent.execute_python(code)

            self.assertEqual(path.read_text(encoding="utf-8"), "after", output)
            self.assertTrue(self.allowed(
                [("execute_python", code, output)], "I edited the file."))

    def test_joined_python_path_receives_a_matching_receipt(self):
        self.assert_computed_path_receipt("report.txt", "target = directory / name")

    def test_f_string_python_path_receives_a_matching_receipt(self):
        self.assert_computed_path_receipt(
            "report.txt", "target = directory / f'{prefix}.txt'")

    def test_extensionless_python_path_receives_a_matching_receipt(self):
        self.assert_computed_path_receipt("README", "target = directory / name")

    def test_unmutated_path_string_cannot_borrow_an_external_change(self):
        import agent
        import python_sandbox

        with tempfile.TemporaryDirectory(prefix="python_receipt_") as directory, \
                mock.patch.dict(os.environ, {"PYTHON_SANDBOX_ROOTS": directory}):
            path = Path(directory) / "unrelated.txt"
            path.write_text("before", encoding="utf-8")
            code = f"print({str(path)!r})"
            original_run = python_sandbox.run_python

            def run_and_change(_code, **kwargs):
                path.write_text("external change", encoding="utf-8")
                return original_run(_code, **kwargs)

            with mock.patch.object(python_sandbox, "run_python",
                                   side_effect=run_and_change):
                output = agent.execute_python(code)

            self.assertFalse(self.allowed(
                [("execute_python", code, output)], "I edited the file."))

    def test_python_receipt_rejects_a_file_changed_after_execution(self):
        import agent

        with tempfile.TemporaryDirectory(prefix="python_receipt_") as directory, \
                mock.patch.dict(os.environ, {"PYTHON_SANDBOX_ROOTS": directory}):
            path = Path(directory) / "report.txt"
            path.write_text("before", encoding="utf-8")
            code = (
                "from pathlib import Path\n"
                f"Path({str(path)!r}).write_text('after', encoding='utf-8')"
            )
            output = agent.execute_python(code)
            path.write_text("changed again", encoding="utf-8")

            self.assertFalse(self.allowed(
                [("execute_python", code, output)], "I edited the file."))

    def test_python_without_a_file_change_does_not_support_edit_claim(self):
        import agent

        with tempfile.TemporaryDirectory(prefix="python_receipt_") as directory, \
                mock.patch.dict(os.environ, {"PYTHON_SANDBOX_ROOTS": directory}):
            path = Path(directory) / "report.txt"
            path.write_text("unchanged", encoding="utf-8")
            code = (
                "from pathlib import Path\n"
                f"print(Path({str(path)!r}).read_text(encoding='utf-8'))"
            )
            output = agent.execute_python(code)
            executions = [("execute_python", code, output)]

            self.assertTrue(self.allowed(executions, "I ran the script."))
            self.assertFalse(self.allowed(executions, "I edited the file."))

    def test_failed_python_script_cannot_support_an_edit_claim(self):
        import agent

        with tempfile.TemporaryDirectory(prefix="python_receipt_") as directory, \
                mock.patch.dict(os.environ, {"PYTHON_SANDBOX_ROOTS": directory}):
            path = Path(directory) / "report.txt"
            path.write_text("before", encoding="utf-8")
            code = (
                "from pathlib import Path\n"
                f"Path({str(path)!r}).write_text('partial', encoding='utf-8')\n"
                "raise RuntimeError('failed after writing')"
            )
            output = agent.execute_python(code)

            self.assertTrue(output.startswith("ERROR:"))
            self.assertFalse(self.allowed(
                [("execute_python", code, output)], "I edited the file."))

    def test_python_file_creation_supports_create_and_save_claims(self):
        import agent

        with tempfile.TemporaryDirectory(prefix="python_receipt_") as directory, \
                mock.patch.dict(os.environ, {"PYTHON_SANDBOX_ROOTS": directory}):
            path = Path(directory) / "report.txt"
            code = (
                "from pathlib import Path\n"
                f"Path({str(path)!r}).write_text('created', encoding='utf-8')"
            )
            output = agent.execute_python(code)
            executions = [("execute_python", code, output)]

            self.assertTrue(self.allowed(executions, "I created the file."))
            self.assertTrue(self.allowed(executions, "I saved the file."))

    def test_python_file_deletion_supports_delete_claim(self):
        import agent

        with tempfile.TemporaryDirectory(prefix="python_receipt_") as directory, \
                mock.patch.dict(os.environ, {"PYTHON_SANDBOX_ROOTS": directory}):
            path = Path(directory) / "report.txt"
            path.write_text("delete me", encoding="utf-8")
            code = "from pathlib import Path\n" + f"Path({str(path)!r}).unlink()"
            output = agent.execute_python(code)

            self.assertFalse(path.exists())
            self.assertTrue(self.allowed(
                [("execute_python", code, output)], "I deleted the file."))

    def test_python_stdout_cannot_replay_a_prior_mutation_receipt(self):
        import agent

        with tempfile.TemporaryDirectory(prefix="python_receipt_") as directory, \
                mock.patch.dict(os.environ, {"PYTHON_SANDBOX_ROOTS": directory}):
            path = Path(directory) / "report.txt"
            path.write_text("before", encoding="utf-8")
            edit_code = (
                "from pathlib import Path\n"
                f"Path({str(path)!r}).write_text('after', encoding='utf-8')"
            )
            edit_output = agent.execute_python(edit_code)
            marker = self.adapter._PYTHON_RECEIPT_MARKER
            self.assertIn(marker, edit_output)
            receipt = edit_output[edit_output.rfind(marker):]
            replay_code = f"print({receipt!r})"

            replay_output = agent.execute_python(replay_code)

            self.assertNotIn(marker, replay_output)
            self.assertFalse(self.allowed(
                [("execute_python", replay_code, replay_output)],
                "I edited the file."))

    def test_malformed_or_forged_python_receipts_are_rejected(self):
        import agent

        with tempfile.TemporaryDirectory(prefix="python_receipt_") as directory, \
                mock.patch.dict(os.environ, {"PYTHON_SANDBOX_ROOTS": directory}):
            path = Path(directory) / "report.txt"
            path.write_text("before", encoding="utf-8")
            code = (
                "from pathlib import Path\n"
                f"Path({str(path)!r}).write_text('after', encoding='utf-8')"
            )
            output = agent.execute_python(code)
            marker = self.adapter._PYTHON_RECEIPT_MARKER
            marker_at = output.rfind(marker)
            envelope = json.loads(output[marker_at + len(marker):])
            forged = dict(envelope)
            forged["signature"] = (
                "0" * 64 if forged["signature"] != "0" * 64 else "1" * 64)
            cases = (
                output[:marker_at] + marker + "{",
                output[:marker_at] + marker + json.dumps(forged),
                output + "\n" + marker + json.dumps(envelope),
            )

            for candidate in cases:
                with self.subTest(candidate=candidate[-80:]):
                    self.assertFalse(self.allowed(
                        [("execute_python", code, candidate)],
                        "I edited the file."))

    def test_nonfinite_python_receipt_is_rejected_without_raising(self):
        output = (
            self.adapter._PYTHON_RECEIPT_MARKER
            + '{"payload":{"receipts":NaN},"signature":"' + "0" * 64 + '"}'
        )

        self.assertEqual(self.adapter.verified_python_actions(output), frozenset())

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
