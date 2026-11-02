import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import self_curriculum as curriculum
from autonomous_loop import LoopResult, RecordedAction, ToolResult, Verification


class ScriptedLLM:
    def __init__(self, *, challenge="csv_summary", skill=None, loop_replies=None):
        self.challenge = challenge
        self.skill = skill or (
            "---\nname: csv_summary\n"
            "description: Summarize a CSV file inside the sandbox.\n---\n\n"
            "## Steps\n\n1. Read /workspace/sales.csv.\n"
            "2. Write /workspace/summary.json.\n"
        )
        self.loop_replies = list(loop_replies or [])
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if "CHALLENGE IDS" in prompt:
            return json.dumps({"challenge": self.challenge})
        if "Write SKILL.md" in prompt:
            return self.skill
        if self.loop_replies:
            return self.loop_replies.pop(0)
        return "DONE"


class FakeSandbox:
    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.started = self.stopped = self.reset = self.seeded = 0
        self.tool_calls = []
        self.tool_descriptions = {"sandbox_write": "Write a sandbox file."}

    def start(self):
        self.started += 1

    def seed_files(self, files):
        self.seeded += 1
        self.files = dict(files)

    def stop(self):
        self.stopped += 1

    def stop_background_processes(self):
        self.reset += 1

    def verify(self, challenge):
        if not self.verdicts:
            return Verification(True, "verified")
        return self.verdicts.pop(0)

    def execute_tool(self, tool, args, stop_event):
        self.tool_calls.append((tool, args))
        return ToolResult(True, "done")

    def authorize(self, tool, args):
        return tool in self.tool_descriptions


class FakeLoop:
    def __init__(self, results, goals):
        self.results = list(results)
        self.goals = goals

    def run(self, goal, memory=()):
        self.goals.append(goal)
        return self.results.pop(0)


class CurriculumTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.quarantine = Path(self.tmp.name) / "quarantine"

    def write_solved_entry(self, name, *, challenge=None, skill_name=None):
        entry = self.quarantine / name
        entry.mkdir(parents=True)
        if challenge is not None:
            (entry / "metadata.json").write_text(
                json.dumps({"challenge": challenge}), encoding="utf-8")
        if skill_name is not None:
            (entry / "SKILL.md").write_text(
                "---\n"
                f"name: {skill_name}\n"
                "description: A previously solved challenge.\n"
                "---\n\n## Steps\n\n1. Solved it.\n",
                encoding="utf-8")
        return entry

    def test_catalog_is_deterministic_and_has_expected_values(self):
        for challenge_id in curriculum.CATALOG:
            first = curriculum.build_challenge(challenge_id, seed=7)
            second = curriculum.build_challenge(challenge_id, seed=7)
            self.assertEqual(first, second)
            self.assertTrue(first.fixtures)
            self.assertIn("/workspace/", first.goal)
        with self.assertRaises(curriculum.CurriculumError):
            curriculum.build_challenge("unknown", seed=1)

    def test_solved_challenges_are_read_from_metadata_or_skill_name(self):
        self.write_solved_entry("run-a", challenge="local_http_service")
        self.write_solved_entry("run-b", skill_name="loopback-health-service")
        self.write_solved_entry("run-c", challenge="csv_summary")
        self.assertEqual(
            curriculum.solved_challenge_ids(self.quarantine),
            {"local_http_service", "csv_summary"})
        self.assertEqual(
            sorted(curriculum.available_challenges(self.quarantine)),
            ["log_report"])

    def test_solved_challenges_are_not_offered_or_accepted(self):
        self.write_solved_entry("run-a", challenge="csv_summary")
        llm = ScriptedLLM(challenge="csv_summary")
        sandbox = FakeSandbox([])
        with self.assertRaises(curriculum.CurriculumError):
            curriculum.run_curriculum(
                llm=llm, sandbox=sandbox,
                quarantine_root=self.quarantine, seed=1)
        self.assertEqual(sandbox.started, 0)
        prompt = llm.prompts[-1]
        self.assertNotIn("csv_summary", prompt)
        self.assertIn("log_report", prompt)
        self.assertIn("local_http_service", prompt)

    def test_explicit_solved_challenge_is_refused(self):
        self.write_solved_entry("run-a", challenge="csv_summary")
        sandbox = FakeSandbox([])
        with self.assertRaisesRegex(curriculum.CurriculumError, "already quarantined"):
            curriculum.run_curriculum(
                llm=ScriptedLLM(), sandbox=sandbox,
                quarantine_root=self.quarantine, seed=1,
                challenge_id="csv_summary")
        self.assertEqual(sandbox.started, 0)

    def test_empty_catalog_generates_harder_challenges_then_escalates(self):
        solved = set(curriculum.CATALOG)
        first = curriculum.generate_challenge(solved, seed=7)
        self.assertEqual(first.challenge_id, "json_to_csv")
        self.assertEqual(first, curriculum.generate_challenge(solved, seed=7))
        self.assertTrue(first.fixtures)
        self.assertIn("/workspace/", first.goal)

        solved.add(first.challenge_id)
        second = curriculum.generate_challenge(solved, seed=7)
        self.assertEqual(second.challenge_id, "word_frequencies")

        solved.add(second.challenge_id)
        escalation = curriculum.generate_challenge(solved, seed=7)
        self.assertEqual(escalation.challenge_id, "escalation_1")
        solved.add(escalation.challenge_id)
        harder = curriculum.generate_challenge(solved, seed=7)
        self.assertEqual(harder.challenge_id, "escalation_2")
        self.assertNotEqual(harder, escalation)
        self.assertNotEqual(harder.fixtures, escalation.fixtures)
        self.assertNotEqual(
            harder.verifier["expected"], escalation.verifier["expected"])

    def test_run_curriculum_skips_selection_when_catalog_is_solved(self):
        for challenge_id in curriculum.CATALOG:
            self.write_solved_entry(
                f"solved-{challenge_id}", challenge=challenge_id)
        llm = ScriptedLLM()
        sandbox = FakeSandbox([Verification(True, "verified")])
        result = curriculum.run_curriculum(
            llm=llm, sandbox=sandbox, quarantine_root=self.quarantine, seed=7,
            loop_factory=lambda *_: FakeLoop(
                [LoopResult("success", 1, "verified", ())], []))
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(result["challenge"], "json_to_csv")
        self.assertNotIn("CHALLENGE IDS", "\n".join(llm.prompts))
        metadata = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in self.quarantine.glob("*/metadata.json")
        ]
        self.assertIn("json_to_csv", {entry["challenge"] for entry in metadata})

    def test_csv_verifier_checks_header_and_rows(self):
        challenge = curriculum.build_challenge("json_to_csv", seed=3)
        workspace = Path(self.tmp.name) / "workspace"
        workspace.mkdir()
        sandbox = curriculum.DockerSandbox(run_dir=workspace)
        header = ",".join(challenge.verifier["header"])
        body = "".join(
            ",".join(row) + "\n" for row in challenge.verifier["rows"])
        (workspace / "orders.csv").write_text(
            header + "\n" + body, encoding="utf-8")
        self.assertTrue(sandbox.verify(challenge).achieved)
        (workspace / "orders.csv").write_text(header + "\n", encoding="utf-8")
        self.assertFalse(sandbox.verify(challenge).achieved)

    def test_invalid_challenge_selection_fails_closed(self):
        sandbox = FakeSandbox([])
        with self.assertRaises(curriculum.CurriculumError):
            curriculum.run_curriculum(
                llm=ScriptedLLM(challenge="rm_rf"), sandbox=sandbox,
                quarantine_root=self.quarantine, seed=1)
        self.assertEqual(sandbox.started, 0)
        self.assertEqual(sandbox.tool_calls, [])

    def test_failure_error_is_fed_back_and_success_quarantines_skill(self):
        llm = ScriptedLLM(skill=(
            "---\nname: csv_summary\n"
            "description: Summarize the seeded CSV in the sandbox.\n---\n\n"
            "## Steps\n\n1. Read /workspace/sales.csv.\n"
            "2. Write /workspace/summary.json with the counts.\n"
        ))
        sandbox = FakeSandbox([
            Verification(False, "Missing artifact: /workspace/summary.json"),
            Verification(True, "summary.json matches the fixture"),
        ])
        goals = []
        results = [
            LoopResult("step_limit", 3, "No progress", ()),
            LoopResult("success", 2, "Goal independently verified.", (
                RecordedAction(1, "sandbox_write", True, "Wrote summary.json"),
            )),
        ]
        result = curriculum.run_curriculum(
            llm=llm, sandbox=sandbox, quarantine_root=self.quarantine, seed=7,
            loop_factory=lambda *_: FakeLoop(results, goals))
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(len(goals), 2)
        self.assertIn("Missing artifact: /workspace/summary.json", goals[1])
        self.assertEqual(sandbox.reset, 2)
        self.assertEqual(sandbox.stopped, 1)
        drafts = list(self.quarantine.glob("*/SKILL.md"))
        self.assertEqual(len(drafts), 1)
        text = drafts[0].read_text(encoding="utf-8")
        self.assertIn("name: csv_summary", text)
        metadata = json.loads(next(self.quarantine.glob("*/metadata.json")).read_text(encoding="utf-8"))
        self.assertEqual(metadata["challenge"], "csv_summary")
        self.assertEqual(metadata["attempts"], 2)
        self.assertNotIn("summary.json matches", text)

    def test_exhausted_attempts_do_not_write_a_skill(self):
        llm = ScriptedLLM()
        sandbox = FakeSandbox([Verification(False, "still broken")] * 3)
        goals = []
        results = [LoopResult("step_limit", 3, "No progress", ())] * 3
        result = curriculum.run_curriculum(
            llm=llm, sandbox=sandbox, quarantine_root=self.quarantine, seed=7,
            max_attempts=3, loop_factory=lambda *_: FakeLoop(results, goals))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(len(goals), 3)
        self.assertEqual(len(list(self.quarantine.glob("*/SKILL.md"))), 0)
        self.assertNotIn("Write SKILL.md", "\n".join(llm.prompts))

    def test_malicious_or_malformed_skill_is_not_quarantined(self):
        bad_skills = [
            "not a skill",
            "---\nname: ../../escape\ndescription: bad\n---\n\nbody\n",
            "---\nname: unsafe\ndescription: bad\n---\n\nRun docker --privileged and use C:\\Users\\USER\n",
            "---\nname: leak\ndescription: bad\n---\n\npassword=hunter2\n",
        ]
        for skill in bad_skills:
            with self.subTest(skill=skill):
                sandbox = FakeSandbox([Verification(True, "verified")])
                result = curriculum.run_curriculum(
                    llm=ScriptedLLM(skill=skill), sandbox=sandbox,
                    quarantine_root=self.quarantine, seed=7,
                    loop_factory=lambda *_: FakeLoop(
                        [LoopResult("success", 1, "verified", ())], []))
                self.assertEqual(result["status"], "verified_skill_rejected")
                self.assertEqual(len(list(self.quarantine.glob("*/SKILL.md"))), 0)

    def test_skill_validation_accepts_a_bounded_safe_draft(self):
        data = curriculum.validate_skill_draft(
            "---\nname: csv_summary\n"
            "description: Summarize a sandbox CSV.\n---\n\n"
            "## Steps\n\n1. Read the fixture.\n")
        self.assertEqual(data["name"], "csv_summary")

    def test_container_command_has_hard_boundaries(self):
        command = curriculum.build_container_command(
            "agentbot-curriculum-test", "python:3.12-slim", Path("C:/Temp/AgentSandbox/run-1"))
        joined = " ".join(command)
        for required in ("--network none", "--read-only", "--cap-drop ALL",
                         "--security-opt no-new-privileges", "--pids-limit",
                         "--memory", "/workspace:rw", "--pull=never", "--rm",
                         "--entrypoint python"):
            self.assertIn(required, joined)
        self.assertNotIn("--privileged", joined)
        self.assertNotIn("docker.sock", joined)

    def test_invalid_image_name_is_rejected(self):
        with self.assertRaisesRegex(curriculum.CurriculumError, "Invalid container image"):
            curriculum.DockerSandbox(run_dir=Path(self.tmp.name), image="--privileged")

    def test_verifier_http_uses_isolated_python_outside_workspace(self):
        calls = []
        completed = SimpleNamespace(
            returncode=0, stdout='{"ok": true, "service": "agentbot"}', stderr="")
        sandbox = curriculum.DockerSandbox(
            run_dir=Path(self.tmp.name), image="python:3.12-slim",
            run_command=lambda args, **kwargs: calls.append(args) or completed)
        self.assertIn('"ok"', sandbox.http_get("http://127.0.0.1:8765/health"))
        args = calls[0]
        self.assertEqual(args[args.index("-w") + 1], "/tmp")
        self.assertIn("-I", args)
        self.assertIn("-P", args)

    def test_stop_always_removes_an_orphan_container(self):
        calls = []
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        sandbox = curriculum.DockerSandbox(
            run_dir=Path(self.tmp.name), image="python:3.12-slim",
            name="agentbot-curriculum-orphan",
            run_command=lambda args, **kwargs: calls.append(args) or completed)
        sandbox.stop()
        self.assertEqual(calls[0], ["docker", "rm", "-f", "agentbot-curriculum-orphan"])

    def test_path_escape_is_rejected(self):
        root = Path(self.tmp.name) / "workspace"
        root.mkdir()
        for value in ("../outside.txt", "C:\\Windows\\win.ini", "/etc/passwd", ""):
            with self.subTest(value=value), self.assertRaises(curriculum.CurriculumError):
                curriculum.resolve_workspace_path(root, value)
        self.assertEqual(curriculum.resolve_workspace_path(root, "notes/a.txt"),
                         (root / "notes/a.txt").resolve())

    def test_http_is_loopback_only(self):
        self.assertTrue(curriculum.validate_loopback_url("http://127.0.0.1:8765/health"))
        for url in ("https://example.com/", "http://localhost.evil/", "http://[::1]:80/",
                    "ftp://127.0.0.1/"):
            with self.subTest(url=url), self.assertRaises(curriculum.CurriculumError):
                curriculum.validate_loopback_url(url)

    def test_real_loop_uses_only_sandbox_tools_and_quarantines_after_verification(self):
        llm = ScriptedLLM(loop_replies=[
            '{"tool":"sandbox_write","args":{"path":"summary.json","content":"{}"}}',
            "DONE",
        ])
        sandbox = FakeSandbox([Verification(True, "verified")])
        result = curriculum.run_curriculum(
            llm=llm, sandbox=sandbox, quarantine_root=self.quarantine, seed=7)
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(sandbox.tool_calls[0][0], "sandbox_write")
        self.assertEqual(sandbox.started, 1)
        self.assertIn("Write SKILL.md", "\n".join(llm.prompts))

    def test_sandbox_is_stopped_on_start_failure(self):
        class BrokenSandbox(FakeSandbox):
            def start(self):
                self.started += 1
                raise RuntimeError("container failed to start")

        sandbox = BrokenSandbox([])
        with self.assertRaises(RuntimeError):
            curriculum.run_curriculum(
                llm=ScriptedLLM(), sandbox=sandbox,
                quarantine_root=self.quarantine, seed=7)
        self.assertEqual(sandbox.stopped, 1)


class DockerCommandTests(unittest.TestCase):
    def test_start_python_requires_an_existing_python_file(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            sandbox = curriculum.DockerSandbox(
                run_dir=Path(directory), image="python:3.12-slim",
                run_command=lambda args, **kwargs: calls.append(args))
            with self.assertRaisesRegex(curriculum.CurriculumError, "must exist"):
                sandbox.start_python("missing.py")
            (Path(directory) / "notes.txt").write_text("not python", encoding="utf-8")
            with self.assertRaisesRegex(curriculum.CurriculumError, "must exist"):
                sandbox.start_python("notes.txt")
            self.assertEqual(calls, [])

    def test_image_check_does_not_pull(self):
        calls = []
        completed = SimpleNamespace(returncode=0, stdout="sha256:local", stderr="")
        sandbox = curriculum.DockerSandbox(
            run_dir=Path("C:/Temp/AgentSandbox/run-1"),
            image="python:3.12-slim",
            run_command=lambda args, **kwargs: calls.append(args) or completed)
        self.assertTrue(sandbox.image_available())
        self.assertFalse(any("pull" in call for call in calls))

    def test_start_refuses_missing_image_before_container_start(self):
        calls = []
        completed = SimpleNamespace(returncode=1, stdout="", stderr="No such image")
        sandbox = curriculum.DockerSandbox(
            run_dir=Path("C:/Temp/AgentSandbox/run-1"),
            image="python:3.12-slim",
            run_command=lambda args, **kwargs: calls.append(args) or completed)
        with self.assertRaisesRegex(curriculum.CurriculumError, "not available locally"):
            sandbox.start()
        self.assertFalse(any("run" in call for call in calls))


if __name__ == "__main__":
    unittest.main()
