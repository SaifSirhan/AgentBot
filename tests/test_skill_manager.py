"""Unit tests for skill_manager.py.

Run: python -m pytest tests/test_skill_manager.py -v
Or:  python tests/test_skill_manager.py
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import skill_manager as sm


def write_skill(root, name, description="A demo skill.", body="## Steps\nDo it.",
                frontmatter_extra="", raw=None):
    directory = Path(root) / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
        return path
    fm = f"name: {name}\n"
    if description is not None:
        fm += f"description: {description}\n"
    fm += frontmatter_extra
    path.write_text(f"---\n{fm}---\n\n{body}\n", encoding="utf-8")
    return path


class SkillManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skills_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "skills"
        self.legacy = self.tmp / "legacy"
        self.plugins = self.tmp / "plugins"
        for path in (self.repo, self.legacy, self.plugins):
            path.mkdir(parents=True)

    def manager(self):
        return sm.SkillManager(
            skills_dir=self.repo,
            plugins_dir=self.plugins,
            extra_skills_dirs=[self.legacy],
        )

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    def test_repo_skill_wins_over_legacy_duplicate(self):
        write_skill(self.repo, "deploy", description="repo version")
        write_skill(self.legacy, "deploy", description="legacy version")
        write_skill(self.legacy, "old-only", description="legacy only")

        mgr = self.manager()
        skills = {s.skill_id: s for s in mgr.list_skills()}

        self.assertEqual(skills["local:deploy"].description, "repo version")
        self.assertEqual(skills["local:deploy"].source, "repo")
        self.assertEqual(skills["local:old-only"].source, "legacy")
        self.assertEqual(len([s for s in skills.values() if s.name == "deploy"]), 1)

    def test_plugin_skills_are_namespaced(self):
        (self.plugins / "pack" / "skills" / "greet").mkdir(parents=True)
        write_skill(self.plugins / "pack" / "skills", "greet",
                    description="Plugin greeting")
        (self.plugins / "solo").mkdir(parents=True)
        write_skill(self.plugins, "solo", description="Single-skill plugin")

        ids = {s.skill_id for s in self.manager().list_skills()}
        self.assertEqual(ids, {"pack:greet", "solo:solo"})

    def test_invalid_names_and_broken_files_are_skipped_with_warnings(self):
        write_skill(self.repo, "good", description="fine")
        write_skill(self.repo, "123", description="starts with a digit")
        write_skill(self.repo, "Bad", description="uppercase")
        write_skill(self.repo, "no-front", raw="Just a body, no frontmatter.")
        write_skill(self.repo, "bad-yaml", raw="---\ndescription: [unclosed\n---\nbody\n")

        mgr = self.manager()
        ids = {s.skill_id for s in mgr.list_skills()}

        self.assertEqual(ids, {"local:good"})
        self.assertEqual(len(mgr.warnings), 4, mgr.warnings)

    def test_oversized_frontmatter_is_skipped(self):
        write_skill(
            self.repo, "huge",
            raw="---\n" + ("x" * (sm.FRONTMATTER_MAX_BYTES + 100)) + "\n---\nbody\n",
        )
        mgr = self.manager()
        self.assertEqual(mgr.list_skills(), [])
        self.assertTrue(any("unterminated" in w for w in mgr.warnings), mgr.warnings)

    def test_description_normalised_and_fallback_used(self):
        write_skill(
            self.repo, "multi",
            description=None, body="## Run the deploy\n\nSteps.",
            frontmatter_extra="notes: ignore me\n",
        )
        write_skill(self.repo, "wrapped", description=None,
                    raw="---\nname: wrapped\ndescription: |\n  line one\n  line two\n---\n\nbody\n")

        skills = {s.name: s for s in self.manager().list_skills()}
        self.assertEqual(skills["multi"].description, "Run the deploy")
        self.assertEqual(skills["wrapped"].description, "line one line two")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlink_escape_is_not_followed(self):
        outside = self.tmp / "outside"
        write_skill(outside, "evil", description="outside the root")
        link = self.repo / "evil"
        try:
            os.symlink(outside / "evil", link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"cannot create symlink: {exc}")

        mgr = self.manager()
        self.assertEqual(mgr.list_skills(), [])

    # ------------------------------------------------------------------
    # Catalog
    # ------------------------------------------------------------------
    def test_catalog_formats_previews_and_truncates_long_descriptions(self):
        write_skill(self.repo, "short", description="Short one")
        write_skill(self.repo, "long", description="L" * 400)

        catalog = self.manager().system_prompt()

        self.assertIn("local:short \u2014 Short one", catalog)
        long_line = next(l for l in catalog.splitlines() if "local:long" in l)
        preview = long_line.split("\u2014 ", 1)[1]
        self.assertLessEqual(len(preview), sm.CATALOG_DESCRIPTION_CHARS)
        self.assertTrue(preview.endswith("\u2026"))

    def test_catalog_entry_cap_adds_remaining_note(self):
        for i in range(sm.CATALOG_MAX_ENTRIES + 5):
            write_skill(self.repo, f"skill-{i:03d}", description=f"desc {i}")

        with mock.patch.object(sm, "CATALOG_MAX_CHARS", 100000):
            catalog = self.manager().system_prompt()

        entries = [l for l in catalog.splitlines() if l.startswith("- local:")]
        self.assertEqual(len(entries), sm.CATALOG_MAX_ENTRIES)
        self.assertIn("(+5 more", catalog)

    def test_catalog_char_cap_is_enforced(self):
        for i in range(40):
            write_skill(self.repo, f"skill-{i:03d}", description="D" * 300)

        catalog = self.manager().system_prompt()
        self.assertLessEqual(len(catalog), sm.CATALOG_MAX_CHARS)
        self.assertTrue(catalog)

    def test_catalog_empty_when_no_skills(self):
        self.assertEqual(self.manager().system_prompt(), "")

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def test_load_resolves_bare_and_namespaced_ids(self):
        write_skill(self.repo, "deploy", description="Deploy it",
                    body="## Steps\nRun the deploy command.")
        mgr = self.manager()

        for wanted in ("deploy", "local:deploy"):
            content = mgr.load_skill(wanted)
            self.assertIn("Skill: local:deploy", content)
            self.assertIn("Run the deploy command.", content)

    def test_load_resolves_unique_plugin_name(self):
        (self.plugins / "pack" / "skills" / "greet").mkdir(parents=True)
        write_skill(self.plugins / "pack" / "skills", "greet",
                    description="Greeting", body="Say hello.")
        mgr = self.manager()
        self.assertIn("Say hello.", mgr.load_skill("greet"))

    def test_ambiguous_bare_name_is_rejected(self):
        for plugin in ("alpha", "beta"):
            (self.plugins / plugin / "skills" / "shared").mkdir(parents=True)
            write_skill(self.plugins / plugin / "skills", "shared",
                        description=f"{plugin} skill")
        mgr = self.manager()

        with self.assertRaises(sm.SkillError) as ctx:
            mgr.load_skill("shared")
        self.assertIn("ambiguous", str(ctx.exception))
        self.assertIn("alpha:shared", str(ctx.exception))

    def test_unknown_and_empty_ids_are_rejected(self):
        mgr = self.manager()
        with self.assertRaisesRegex(sm.SkillError, "unknown skill"):
            mgr.load_skill("nope")
        with self.assertRaisesRegex(sm.SkillError, "required"):
            mgr.load_skill("  ")

    def test_renamed_frontmatter_is_rejected_on_load(self):
        path = write_skill(self.repo, "deploy", description="Deploy it")
        mgr = self.manager()
        path.write_text(
            "---\nname: something-else\ndescription: Deploy it\n---\n\nbody\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(sm.SkillError, "name changed"):
            mgr.load_skill("deploy")

    def test_oversized_body_is_truncated(self):
        path = write_skill(self.repo, "big", description="Big skill", body="a")
        path.write_text(
            "---\nname: big\ndescription: Big skill\n---\n\n" + ("a" * 70000),
            encoding="utf-8",
        )
        content = self.manager().load_skill("big")
        self.assertIn("[truncated:", content)
        self.assertLess(len(content), 70000)

    def test_invalid_utf8_raises(self):
        path = write_skill(self.repo, "binary", description="Binary", body="")
        path.write_bytes(
            b"---\nname: binary\ndescription: Binary\n---\n\n"
            + (b"a" * 20000) + b"\xff\xfe"
        )
        mgr = self.manager()  # metadata head stays valid
        with self.assertRaisesRegex(sm.SkillError, "not valid UTF-8"):
            mgr.load_skill("binary")

    def test_truncated_invalid_utf8_is_replaced(self):
        path = write_skill(self.repo, "binary-big", description="Binary", body="")
        path.write_bytes(
            b"---\nname: binary-big\ndescription: Binary\n---\n\n"
            + (b"a" * 20000) + b"\xff" + (b"a" * 50000)
        )
        content = self.manager().load_skill("binary-big")
        self.assertIn("[truncated:", content)
        self.assertIn("\ufffd", content)

    # ------------------------------------------------------------------
    # Reload / tool wrapper
    # ------------------------------------------------------------------
    def test_reload_sees_new_skill_and_clears_cache(self):
        mgr = self.manager()
        self.assertEqual(mgr.list_skills(), [])

        write_skill(self.repo, "fresh", description="Fresh skill")
        self.assertEqual(mgr.list_skills(), [])  # cached until reload
        mgr.reload()

        self.assertIn("local:fresh", mgr.system_prompt())
        self.assertEqual(mgr.summary(), "Skills: 1 loaded (repo 1, legacy 0, plugins 0)")

    def test_invoke_tool_shape(self):
        write_skill(self.repo, "deploy", description="Deploy it", body="Go.")
        mgr = self.manager()

        ok = mgr.invoke_tool("deploy")
        self.assertTrue(ok["ok"])
        self.assertIn("Go.", ok["content"])

        bad = mgr.invoke_tool("missing")
        self.assertFalse(bad["ok"])
        self.assertIn("unknown skill", bad["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
