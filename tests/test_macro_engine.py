"""Macro storage, validation, and replay tests.

OS input is never sent: os_control is replaced with a recording double.
"""

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import macro_engine


class FakeClock:
    """Advance a fake clock instead of sleeping."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeOS:
    """Records calls without touching the real desktop."""

    def __init__(self):
        self.calls = []
        self.kill = False

    def kill_switch_active(self):
        return self.kill

    def click_coordinates(self, text):
        self.calls.append(("click", text))
        return f"Clicked {text}"

    def type_text(self, text):
        self.calls.append(("type", text))
        return f"Typed {text}"

    def hotkey(self, keys):
        self.calls.append(("hotkey", "+".join(keys)))
        return f"Pressed {'+'.join(keys)}."


class MacroEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(
            macro_engine.config, "memory_path",
            side_effect=lambda *parts: os.path.join(self.tmp.name, *parts))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _save(self, name, steps):
        return macro_engine.save_macro(name, steps)

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------
    def test_save_and_load_round_trip(self):
        steps = [
            {"action": "click", "x": 100, "y": 200},
            {"action": "type", "text": "hello"},
            {"action": "wait", "seconds": 1},
        ]
        out = self._save("Open Folder", steps)

        self.assertIn("Saved macro 'open_folder'", out)
        self.assertEqual(macro_engine.load_macro("Open Folder"), steps)

        path = Path(self.tmp.name) / "macros" / "open_folder.json"
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), steps)
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_missing_macro_raises(self):
        with self.assertRaisesRegex(macro_engine.MacroError, "not found"):
            macro_engine.load_macro("nope")

    def test_list_macros_is_sorted_and_ignores_other_files(self):
        self._save("beta", [{"action": "wait", "seconds": 0}])
        self._save("alpha", [{"action": "wait", "seconds": 0}])
        (Path(self.tmp.name) / "macros" / "notes.txt").write_text("x")

        self.assertEqual(macro_engine.list_macros(), ["alpha", "beta"])

    def test_name_cannot_escape_the_macros_directory(self):
        for name in ("", "   ", "../evil", "..\\evil", "a/b", "a.b", "x" * 65):
            with self.subTest(name=name):
                with self.assertRaises(macro_engine.MacroError):
                    macro_engine.normalise_name(name)
        self.assertEqual(macro_engine.normalise_name(" Stremio "), "stremio")

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def test_rejects_malformed_steps(self):
        bad = [
            "not json",
            {"action": "click", "x": 1, "y": 2},
            [],
            [{"action": "click", "x": "a", "y": 2}],
            [{"action": "click", "x": -1, "y": 2}],
            [{"action": "click", "x": 1}],
            [{"action": "type", "text": ""}],
            [{"action": "type", "text": 5}],
            [{"action": "wait", "seconds": -1}],
            [{"action": "wait", "seconds": 61}],
            [{"action": "wait", "seconds": "soon"}],
            [{"action": "scroll", "amount": 1}],
            ["click"],
        ]
        for steps in bad:
            with self.subTest(steps=steps):
                with self.assertRaises(macro_engine.MacroError):
                    macro_engine.save_macro("bad", steps)
        self.assertEqual(macro_engine.list_macros(), [])

    def test_rejects_too_many_steps_and_too_much_text(self):
        too_many = [{"action": "wait", "seconds": 0}] * (
            macro_engine.MAX_STEPS + 1)
        with self.assertRaises(macro_engine.MacroError):
            macro_engine.save_macro("big", too_many)

        too_long = "x" * (macro_engine.MAX_TYPE_CHARS + 1)
        with self.assertRaises(macro_engine.MacroError):
            macro_engine.save_macro("long", [{"action": "type", "text": too_long}])

    def test_accepts_json_text(self):
        text = '[{"action": "type", "text": "mpv"}]'
        self._save("json_text", text)
        self.assertEqual(
            macro_engine.load_macro("json_text"),
            [{"action": "type", "text": "mpv"}])

    def test_save_accepts_placeholders_and_reports_variables(self):
        steps = [
            {"action": "click", "x": 1, "y": 2},
            {"action": "type", "text": "Search {{TITLE}} now {{ QUERY }}"},
        ]
        out = self._save("search", steps)

        self.assertEqual(macro_engine.load_macro("search"), steps)
        self.assertIn("TITLE", out)
        self.assertIn("QUERY", out)
        self.assertEqual(
            macro_engine.required_variables(steps), ["QUERY", "TITLE"])

    def test_hotkey_steps_are_validated_and_normalised(self):
        steps = [{"action": "hotkey", "keys": ["CTRL", " F "]}]
        out = self._save("keys", steps)

        self.assertEqual(
            macro_engine.load_macro("keys"),
            [{"action": "hotkey", "keys": ["ctrl", "f"]}])
        self.assertIn("Saved macro 'keys'", out)

    def test_hotkey_rejects_non_string_or_empty_keys(self):
        bad = [
            [{"action": "hotkey"}],
            [{"action": "hotkey", "keys": []}],
            [{"action": "hotkey", "keys": "ctrl+f"}],
            [{"action": "hotkey", "keys": ["ctrl", 2]}],
            [{"action": "hotkey", "keys": [""]}],
            [{"action": "hotkey", "keys": [None]}],
        ]
        for steps in bad:
            with self.subTest(steps=steps):
                with self.assertRaises(macro_engine.MacroError):
                    macro_engine.save_macro("badkeys", steps)

    def test_rejects_unusable_variable_values(self):
        steps = [{"action": "type", "text": "{{TITLE}}"}]
        self._save("vars", steps)
        fake_os = FakeOS()

        for variables in ("TITLE", {"TITLE": None}, {"TITLE": ["x"]}):
            with self.subTest(variables=variables), \
                 mock.patch.dict(sys.modules, {"os_control": fake_os}):
                out = macro_engine.run_macro("vars", variables)
                self.assertTrue(out.startswith("ERROR:"), out)
        self.assertEqual(fake_os.calls, [])

    # ------------------------------------------------------------------
    # Replay
    # ------------------------------------------------------------------
    def test_run_replays_steps_in_order(self):
        clock = FakeClock()
        fake_os = FakeOS()
        steps = [
            {"action": "click", "x": 10, "y": 20},
            {"action": "type", "text": "hello"},
            {"action": "wait", "seconds": 0.5},
            {"action": "click", "x": 30, "y": 40},
        ]
        self._save("demo", steps)

        with mock.patch.dict(sys.modules, {"os_control": fake_os}), \
             mock.patch.object(macro_engine, "time", clock):
            out = macro_engine.run_macro("demo")

        self.assertEqual(fake_os.calls, [
            ("click", "10,20"),
            ("type", "hello"),
            ("click", "30,40"),
        ])
        self.assertAlmostEqual(sum(clock.sleeps), 0.5, places=6)
        self.assertTrue(all(s <= 0.2 for s in clock.sleeps), clock.sleeps)
        self.assertIn("Played macro 'demo' (4 steps)", out)

    def test_run_substitutes_variables(self):
        fake_os = FakeOS()
        self._save("sub", [
            {"action": "click", "x": 5, "y": 6},
            {"action": "type", "text": "Search {{TITLE}} in {{QUERY}}"},
            {"action": "type", "text": "again {{TITLE}}"},
        ])

        with mock.patch.dict(sys.modules, {"os_control": fake_os}), \
             mock.patch.object(macro_engine, "time", FakeClock()):
            out = macro_engine.run_macro(
                "sub", {"TITLE": "Naruto", "QUERY": "anime"})

        self.assertEqual(fake_os.calls, [
            ("click", "5,6"),
            ("type", "Search Naruto in anime"),
            ("type", "again Naruto"),
        ])
        self.assertIn("Played macro 'sub' (3 steps)", out)

    def test_missing_variables_abort_before_any_input(self):
        fake_os = FakeOS()
        self._save("needs", [
            {"action": "click", "x": 1, "y": 2},
            {"action": "type", "text": "{{TITLE}}"},
        ])

        with mock.patch.dict(sys.modules, {"os_control": fake_os}):
            out = macro_engine.run_macro("needs")

        self.assertTrue(out.startswith("ERROR:"), out)
        self.assertIn("TITLE", out)
        self.assertEqual(fake_os.calls, [])

    def test_scalar_variables_are_coerced_and_extras_ignored(self):
        fake_os = FakeOS()
        self._save("coerce", [{"action": "type", "text": "ep {{N}}"}])

        with mock.patch.dict(sys.modules, {"os_control": fake_os}), \
             mock.patch.object(macro_engine, "time", FakeClock()):
            out = macro_engine.run_macro("coerce", {"N": 42, "UNUSED": "x"})

        self.assertEqual(fake_os.calls, [("type", "ep 42")])
        self.assertIn("Played macro", out)

    def test_run_presses_hotkeys_in_order(self):
        fake_os = FakeOS()
        self._save("hot", [
            {"action": "hotkey", "keys": ["win"]},
            {"action": "type", "text": "notepad{enter}"},
            {"action": "hotkey", "keys": ["ctrl", "f"]},
        ])

        with mock.patch.dict(sys.modules, {"os_control": fake_os}), \
             mock.patch.object(macro_engine, "time", FakeClock()):
            out = macro_engine.run_macro("hot")

        self.assertEqual(fake_os.calls, [
            ("hotkey", "win"),
            ("type", "notepad{enter}"),
            ("hotkey", "ctrl+f"),
        ])
        self.assertIn("Played macro 'hot' (3 steps)", out)

    def test_hotkey_failure_stops_replay(self):
        class FailingOS(FakeOS):
            def hotkey(self, keys):
                self.calls.append(("hotkey", "+".join(keys)))
                return "ERROR: hotkey failed: bad key"

        fake_os = FailingOS()
        self._save("badhot", [
            {"action": "hotkey", "keys": ["ctrl", "nope"]},
            {"action": "type", "text": "never"},
        ])

        with mock.patch.dict(sys.modules, {"os_control": fake_os}):
            out = macro_engine.run_macro("badhot")

        self.assertTrue(out.startswith("ERROR:"), out)
        self.assertEqual(fake_os.calls, [("hotkey", "ctrl+nope")])

    def test_click_failure_stops_replay(self):
        class FailingOS(FakeOS):
            def click_coordinates(self, text):
                self.calls.append(("click", text))
                return "ABORTED: kill switch is latched"

        fake_os = FailingOS()
        self._save("stops", [
            {"action": "click", "x": 1, "y": 2},
            {"action": "type", "text": "never"},
        ])

        with mock.patch.dict(sys.modules, {"os_control": fake_os}), \
             mock.patch.object(macro_engine, "time", FakeClock()):
            out = macro_engine.run_macro("stops")

        self.assertEqual(out, "ABORTED: kill switch is latched")
        self.assertEqual(fake_os.calls, [("click", "1,2")])

    def test_kill_switch_aborts_before_a_step(self):
        fake_os = FakeOS()
        fake_os.kill = True
        self._save("guarded", [{"action": "type", "text": "never"}])

        with mock.patch.dict(sys.modules, {"os_control": fake_os}):
            out = macro_engine.run_macro("guarded")

        self.assertTrue(out.startswith("ABORTED:"), out)
        self.assertEqual(fake_os.calls, [])

    def test_kill_switch_interrupts_a_wait(self):
        fake_os = FakeOS()
        calls = {"n": 0}

        def kill_switch():
            calls["n"] += 1
            # Pass the per-step check and one wait slice, latch mid-wait.
            return calls["n"] > 2

        fake_os.kill_switch_active = kill_switch
        clock = FakeClock()
        self._save("waits", [{"action": "wait", "seconds": 10}])

        with mock.patch.dict(sys.modules, {"os_control": fake_os}), \
             mock.patch.object(macro_engine, "time", clock):
            out = macro_engine.run_macro("waits")

        self.assertTrue(out.startswith("ABORTED:"), out)
        self.assertEqual(clock.sleeps, [0.2])


class MacroToolTests(unittest.TestCase):
    """The agent wrappers keep the input format strict."""

    def setUp(self):
        import agent
        self.agent = agent

    def test_save_macro_parses_name_and_steps(self):
        with mock.patch.object(
                macro_engine, "save_macro", return_value="ok") as save:
            out = self.agent.save_macro(
                'demo| [{"action": "type", "text": "a|b"}]')

        self.assertEqual(out, "ok")
        save.assert_called_once_with(
            "demo", '[{"action": "type", "text": "a|b"}]')

    def test_save_macro_requires_separator(self):
        out = self.agent.save_macro("demo")
        self.assertTrue(out.startswith("ERROR:"), out)

    def test_save_macro_reports_errors(self):
        with mock.patch.object(
                macro_engine, "save_macro",
                side_effect=macro_engine.MacroError("bad steps")):
            out = self.agent.save_macro("demo|[]")
        self.assertIn("bad steps", out)
        self.assertTrue(out.startswith("ERROR:"), out)

    def test_play_macro_requires_a_name(self):
        out = self.agent.play_macro("  ")
        self.assertTrue(out.startswith("ERROR:"), out)

    def test_play_macro_delegates(self):
        with mock.patch.object(
                macro_engine, "run_macro", return_value="played") as run:
            out = self.agent.play_macro(" demo ")
        self.assertEqual(out, "played")
        run.assert_called_once_with("demo")

    def test_play_macro_accepts_an_inline_variables_object(self):
        with mock.patch.object(
                macro_engine, "run_macro", return_value="played") as run:
            out = self.agent.play_macro('stremio_search|{"TITLE": "Naruto"}')

        self.assertEqual(out, "played")
        run.assert_called_once_with("stremio_search", {"TITLE": "Naruto"})

    def test_play_macro_accepts_a_variables_argument(self):
        with mock.patch.object(
                macro_engine, "run_macro", return_value="played") as run:
            out = self.agent.play_macro(
                "stremio_search", variables={"TITLE": "Naruto"})

        self.assertEqual(out, "played")
        run.assert_called_once_with("stremio_search", {"TITLE": "Naruto"})

    def test_play_macro_rejects_bad_inline_json(self):
        out = self.agent.play_macro("stremio_search|{TITLE}")
        self.assertTrue(out.startswith("ERROR:"), out)

    def test_execute_tool_passes_variables_from_the_action(self):
        with mock.patch.object(
                self.agent, "play_macro", return_value="PLAYED") as play:
            out = self.agent.execute_tool({
                "tool": "play_macro",
                "input": "stremio_search",
                "variables": {"TITLE": "Naruto"},
            })

        self.assertEqual(out, "PLAYED")
        play.assert_called_once_with("stremio_search", {"TITLE": "Naruto"})

    # ------------------------------------------------------------------
    # Registry and prompt
    # ------------------------------------------------------------------
    def test_tools_are_registered_once(self):
        manual = self.agent.load_skill_tool("core_tools")
        for name in ("save_macro", "play_macro"):
            self.assertIn(name, self.agent.VALID_TOOLS, name)
            self.assertIn(name, self.agent.TOOL_DESCRIPTIONS, name)
            self.assertNotIn(name, self.agent.SAFE_GROUP_TOOLS, name)
            pattern = f"{name}(input)"
            self.assertEqual(manual.count(pattern), 1, pattern)

    def test_execute_tool_dispatches_macros(self):
        with mock.patch.object(
                self.agent, "save_macro", return_value="SAVED") as save, \
             mock.patch.object(
                self.agent, "play_macro", return_value="PLAYED") as play:
            self.assertEqual(self.agent.execute_tool(
                {"tool": "save_macro", "input": "demo|[]"}), "SAVED")
            self.assertEqual(self.agent.execute_tool(
                {"tool": "play_macro", "input": "demo"}), "PLAYED")

        save.assert_called_once_with("demo|[]")
        play.assert_called_once_with("demo", None)

    def test_group_mode_blocks_macro_replay(self):
        with mock.patch.object(self.agent, "GROUP_MODE", True):
            for name in ("save_macro", "play_macro"):
                out = self.agent.execute_tool(
                    {"tool": name, "input": "demo|[]" if name == "save_macro"
                     else "demo"})
                self.assertIn("disabled in group chats", out, name)

    def test_prompt_contains_the_self_learning_rule(self):
        flat = " ".join(self.agent.load_skill_tool("core_tools").split())
        self.assertIn(
            "For repetitive tasks (like opening a specific folder or app), "
            "first try play_macro. If it doesn't exist or fails, use OS "
            "Control with Vision to do it. Once you succeed, use save_macro "
            "to record the coordinates so you don't have to guess next time.",
            flat,
        )

    def test_prompt_requires_variables_for_placeholder_macros(self):
        flat = " ".join(self.agent.load_skill_tool("core_tools").split())
        self.assertIn(
            "If a macro contains placeholders like {{TITLE}}, you MUST pass "
            "the variables dictionary when calling play_macro. Example: "
            'play_macro("stremio_search", variables={"TITLE": "Naruto"}).',
            flat,
        )

    def test_save_macro_docs_mention_placeholders(self):
        text = self.agent.load_skill_tool("core_tools")
        start = text.index("save_macro(input)")
        end = text.index("play_macro(input)")
        self.assertIn("{{TITLE}}", text[start:end])

    def test_save_macro_docs_mention_hotkey_steps(self):
        text = self.agent.load_skill_tool("core_tools")
        start = text.index("save_macro(input)")
        end = text.index("play_macro(input)")
        self.assertIn('"hotkey"', text[start:end])

    def test_prompt_prefers_hotkeys_in_macros(self):
        flat = " ".join(self.agent.load_skill_tool("core_tools").split())
        self.assertIn(
            "When creating a macro, ALWAYS prefer using hotkeys (like win, "
            "ctrl+f, alt+tab) for opening apps or focusing search bars "
            "instead of clicking. Only use click for elements that have no "
            "keyboard shortcut, like specific search results or posters.",
            flat,
        )

    def test_prompt_stays_under_budget_with_macros(self):
        prompt = self.agent._build_plan_prompt("hello", "")
        self.assertLess(
            len(prompt) / 4, 7800,
            f"prompt is {len(prompt)} chars (~{len(prompt) / 4:.0f} tokens)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
