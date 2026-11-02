"""Skill discovery and loading for AgentBot.

A skill is a directory containing SKILL.md with YAML frontmatter:

    ---
    name: clock-out-reminder
    description: Remind the user to clock out at the end of the work day.
    ---
    ## Steps
    ...

Two local roots are scanned:

* the repo ``skills/`` directory (versioned with the project), and
* the legacy ``%LOCALAPPDATA%\\AgentMemory\\skills`` directory used by
  ``agent.skill_manage``.

Repo skills win when both roots define the same name. Plugin skills live under
``plugins/<plugin>/skills/<skill>/SKILL.md`` (or ``plugins/<plugin>/SKILL.md``)
and are addressed as ``<plugin>:<skill>``.

Only skill metadata is read when scanning. The body is read on demand by
``load_skill`` so the system prompt stays small (progressive disclosure).
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import yaml

# A skill id must start with a letter. Hyphens and underscores are allowed as
# separators, so both "clock-out-reminder" and legacy "my_skill" names work.
NAME_RE = re.compile(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*\Z")

FRONTMATTER_MAX_BYTES = 16 * 1024
MAX_SKILL_BODY_BYTES = 64 * 1024

# Catalog limits keep the system prompt bounded no matter how many skills or
# how verbose their descriptions become.
CATALOG_DESCRIPTION_CHARS = 160
CATALOG_MAX_ENTRIES = 50
CATALOG_MAX_CHARS = 2000

SKILL_FILENAME = "SKILL.md"

LOCAL_NAMESPACE = "local"


class SkillError(Exception):
    """A skill could not be discovered or loaded."""


@dataclass(frozen=True)
class Skill:
    skill_id: str          # "local:name" or "plugin:name"
    name: str
    description: str
    path: Path             # path to SKILL.md
    source: str            # "repo", "legacy", or "plugin:<plugin>"
    plugin: Optional[str] = None


def _is_within(path: Path, root: Path) -> bool:
    """True when `path` resolves inside `root` (also blocks symlink escapes)."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        return False


def _normalise_description(value) -> str:
    """Frontmatter descriptions are single-line text in the catalog."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SkillError("description must be a string")
    return " ".join(value.split())


def _split_frontmatter(text: str) -> Tuple[str, str]:
    """Return (yaml_text, body) or raise SkillError when the header is absent."""
    stripped = text.lstrip("\ufeff")
    if not stripped.startswith("---"):
        raise SkillError("missing YAML frontmatter")
    end = stripped.find("\n---", 3)
    if end == -1:
        raise SkillError("unterminated YAML frontmatter")
    body = stripped[end + 4:]
    if body.startswith("\n"):
        body = body[1:]
    return stripped[3:end], body


def _fallback_description(body: str, name: str) -> str:
    """Use the first body heading/line when frontmatter omits a description."""
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return _normalise_description(line)[:CATALOG_DESCRIPTION_CHARS]
    return name.replace("-", " ").replace("_", " ")


def _preview(description: str) -> str:
    if len(description) <= CATALOG_DESCRIPTION_CHARS:
        return description
    return description[:CATALOG_DESCRIPTION_CHARS - 1].rstrip() + "\u2026"


class SkillManager:
    """Scans skill roots and loads skill bodies on demand.

    The manager is thread-safe; the GUI reloads it from the Qt thread while
    agent turns may read the catalog from a worker thread.
    """

    def __init__(
        self,
        skills_dir: str | os.PathLike = "skills",
        plugins_dir: str | os.PathLike = "plugins",
        extra_skills_dirs: Iterable[str | os.PathLike] = (),
    ) -> None:
        self._repo_root = Path(skills_dir)
        self._plugins_root = Path(plugins_dir)
        self._extra_roots = [Path(p) for p in extra_skills_dirs]
        self._lock = threading.RLock()
        self._skills: Dict[str, Skill] = {}
        self._warnings: List[str] = []
        self._catalog_cache: Optional[str] = None
        self._scanned = False

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    def scan(self) -> List[Skill]:
        """Rebuild the catalog from disk. Invalid skills are skipped with a
        warning so one broken file cannot hide every other skill."""
        with self._lock:
            skills: Dict[str, Skill] = {}
            warnings: List[str] = []

            self._scan_skills_root(
                self._repo_root, "repo", skills, warnings, allow_duplicates=False
            )
            for root in self._extra_roots:
                self._scan_skills_root(
                    root, "legacy", skills, warnings, allow_duplicates=True
                )
            self._scan_plugins(skills, warnings)

            self._skills = skills
            self._warnings = warnings
            self._catalog_cache = None
            self._scanned = True
            return self.list_skills()

    def reload(self) -> List[Skill]:
        return self.scan()

    def _scan_skills_root(
        self,
        root: Path,
        source: str,
        skills: Dict[str, Skill],
        warnings: List[str],
        allow_duplicates: bool,
    ) -> None:
        if not root.is_dir():
            return
        try:
            entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
        except OSError as exc:
            warnings.append(f"{root}: cannot list ({exc})")
            return

        for entry in entries:
            if entry.name.startswith(".") or not entry.is_dir():
                continue
            if not NAME_RE.fullmatch(entry.name):
                warnings.append(f"{root.name}/{entry.name}: invalid skill name")
                continue
            skill_file = entry / SKILL_FILENAME
            if not skill_file.is_file() or not _is_within(skill_file, root):
                continue
            skill = self._read_metadata(
                skill_file, entry.name, f"{LOCAL_NAMESPACE}:{entry.name}",
                source=source, warnings=warnings,
            )
            if skill is None:
                continue
            if skill.name in {s.name for s in skills.values() if s.plugin is None}:
                if not allow_duplicates:
                    warnings.append(
                        f"{source}/{entry.name}: duplicate of an earlier skill"
                    )
                continue
            skills[skill.skill_id] = skill

    def _scan_plugins(self, skills: Dict[str, Skill], warnings: List[str]) -> None:
        root = self._plugins_root
        if not root.is_dir():
            return
        try:
            plugins = sorted(root.iterdir(), key=lambda p: p.name.lower())
        except OSError as exc:
            warnings.append(f"{root}: cannot list ({exc})")
            return

        for plugin_dir in plugins:
            if plugin_dir.name.startswith(".") or not plugin_dir.is_dir():
                continue
            if not NAME_RE.fullmatch(plugin_dir.name):
                warnings.append(f"plugins/{plugin_dir.name}: invalid plugin name")
                continue

            single = plugin_dir / SKILL_FILENAME
            if single.is_file() and _is_within(single, root):
                skill = self._read_metadata(
                    single, plugin_dir.name,
                    f"{plugin_dir.name}:{plugin_dir.name}",
                    source=f"plugin:{plugin_dir.name}",
                    warnings=warnings, plugin=plugin_dir.name,
                )
                if skill is not None:
                    skills.setdefault(skill.skill_id, skill)

            skills_root = plugin_dir / "skills"
            if not skills_root.is_dir():
                continue
            try:
                entries = sorted(skills_root.iterdir(), key=lambda p: p.name.lower())
            except OSError as exc:
                warnings.append(f"{skills_root}: cannot list ({exc})")
                continue
            for entry in entries:
                if entry.name.startswith(".") or not entry.is_dir():
                    continue
                if not NAME_RE.fullmatch(entry.name):
                    warnings.append(
                        f"plugins/{plugin_dir.name}/skills/{entry.name}: "
                        "invalid skill name"
                    )
                    continue
                skill_file = entry / SKILL_FILENAME
                if not skill_file.is_file() or not _is_within(skill_file, root):
                    continue
                skill = self._read_metadata(
                    skill_file, entry.name,
                    f"{plugin_dir.name}:{entry.name}",
                    source=f"plugin:{plugin_dir.name}",
                    warnings=warnings, plugin=plugin_dir.name,
                )
                if skill is not None:
                    skills.setdefault(skill.skill_id, skill)

    def _read_metadata(
        self,
        path: Path,
        name: str,
        skill_id: str,
        source: str,
        warnings: List[str],
        plugin: Optional[str] = None,
    ) -> Optional[Skill]:
        try:
            with open(path, "rb") as handle:
                head = handle.read(FRONTMATTER_MAX_BYTES)
            text = head.decode("utf-8-sig")
            yaml_text, body_head = _split_frontmatter(text)
            meta = yaml.safe_load(yaml_text) or {}
            if not isinstance(meta, dict):
                raise SkillError("frontmatter is not a mapping")
            description = _normalise_description(meta.get("description"))
            if not description:
                description = _fallback_description(body_head, name)
            return Skill(
                skill_id=skill_id,
                name=name,
                description=description,
                path=path,
                source=source,
                plugin=plugin,
            )
        except (OSError, UnicodeDecodeError, yaml.YAMLError, SkillError) as exc:
            warnings.append(f"{skill_id}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Catalog
    # ------------------------------------------------------------------
    def list_skills(self) -> List[Skill]:
        with self._lock:
            if not self._scanned:
                self.scan()
            return [self._skills[key] for key in sorted(self._skills)]

    def get(self, skill_id: str) -> Skill:
        with self._lock:
            if not self._scanned:
                self.scan()
            return self._resolve_locked(skill_id)

    @property
    def warnings(self) -> List[str]:
        with self._lock:
            return list(self._warnings)

    def summary(self) -> str:
        skills = self.list_skills()
        repo = sum(1 for s in skills if s.source == "repo")
        legacy = sum(1 for s in skills if s.source == "legacy")
        plugins = len(skills) - repo - legacy
        return (f"Skills: {len(skills)} loaded "
                f"(repo {repo}, legacy {legacy}, plugins {plugins})")

    def system_prompt(self) -> str:
        """Compact catalog for the agent system prompt, or "" when empty."""
        with self._lock:
            if not self._scanned:
                self.scan()
            if self._catalog_cache is not None:
                return self._catalog_cache

            skills = [self._skills[key] for key in sorted(self._skills)]
            if not skills:
                self._catalog_cache = ""
                return self._catalog_cache

            header = (
                "AVAILABLE SKILLS (use load_skill to read a matching one):\n"
            )
            lines: List[str] = []
            used = len(header)
            shown = 0
            for skill in skills:
                if shown >= CATALOG_MAX_ENTRIES:
                    break
                line = f"- {skill.skill_id} \u2014 {_preview(skill.description)}\n"
                if used + len(line) > CATALOG_MAX_CHARS:
                    break
                lines.append(line)
                used += len(line)
                shown += 1

            remaining = len(skills) - shown
            if remaining:
                line = f"- (+{remaining} more; load by id from skills/)\n"
                if used + len(line) <= CATALOG_MAX_CHARS:
                    lines.append(line)

            if not lines:
                self._catalog_cache = ""
            else:
                self._catalog_cache = header + "".join(lines)
            return self._catalog_cache

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def _resolve_locked(self, raw_id: str) -> Skill:
        wanted = (raw_id or "").strip()
        if not wanted:
            raise SkillError("skill id required")
        if len(wanted) > 128:
            raise SkillError("skill id is too long")
        if wanted in self._skills:
            return self._skills[wanted]

        local_id = f"{LOCAL_NAMESPACE}:{wanted}"
        if local_id in self._skills:
            return self._skills[local_id]

        matches = [s for s in self._skills.values() if s.name == wanted]
        if len(matches) == 1:
            return matches[0]
        if matches:
            ids = ", ".join(sorted(s.skill_id for s in matches))
            raise SkillError(f"ambiguous skill '{wanted}' — use one of: {ids}")
        raise SkillError(
            f"unknown skill '{wanted}' (available: "
            f"{', '.join(sorted(self._skills)) or 'none'})"
        )

    def load_skill(self, raw_id: str) -> str:
        """Return the body of a skill, with its id as a short header.

        The file is re-read on every call so edits show up without a restart.
        Metadata is revalidated and oversized bodies are truncated.
        """
        skill = self.get(raw_id)

        with self._lock:
            try:
                with open(skill.path, "rb") as handle:
                    data = handle.read(MAX_SKILL_BODY_BYTES + 1)
            except OSError as exc:
                raise SkillError(f"cannot read {skill.skill_id}: {exc}") from exc

        truncated = len(data) > MAX_SKILL_BODY_BYTES
        if truncated:
            data = data[:MAX_SKILL_BODY_BYTES]

        try:
            text = data.decode("utf-8-sig", errors="replace" if truncated else "strict")
            yaml_text, body = _split_frontmatter(text)
            meta = yaml.safe_load(yaml_text) or {}
            if not isinstance(meta, dict):
                raise SkillError("frontmatter is not a mapping")
            meta_name = _normalise_description(meta.get("name"))
            if meta_name and meta_name != skill.name:
                raise SkillError(
                    f"{skill.skill_id}: frontmatter name changed to '{meta_name}'"
                )
            description = _normalise_description(meta.get("description"))
            if not description:
                description = _fallback_description(body, skill.name)
        except UnicodeDecodeError as exc:
            raise SkillError(f"{skill.skill_id}: not valid UTF-8") from exc
        except yaml.YAMLError as exc:
            raise SkillError(f"{skill.skill_id}: bad frontmatter ({exc})") from exc
        except SkillError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise SkillError(f"{skill.skill_id}: {exc}") from exc

        body = body.strip()
        if truncated:
            body += (
                f"\n\n[truncated: showing the first {MAX_SKILL_BODY_BYTES} "
                "bytes of this skill]"
            )

        return f"Skill: {skill.skill_id}\nDescription: {description}\n\n{body}"

    def invoke_tool(self, input_text: str) -> dict:
        """Tool-shaped wrapper: {'ok': True, ...} or {'ok': False, 'error': ...}."""
        try:
            content = self.load_skill(input_text)
        except SkillError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "skill": (input_text or "").strip(), "content": content}
