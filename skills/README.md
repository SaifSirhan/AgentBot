# Repo skills

Versioned skills live here, one directory per skill:

```
skills/
  clock-out-reminder/
    SKILL.md
```

`SKILL.md` starts with YAML frontmatter:

```markdown
---
name: clock-out-reminder
description: Remind the user to clock out at the end of the work day.
---

## When to use
...
```

Name rules: lowercase letters, digits, hyphens, and underscores; the first
character must be a letter. The directory name is the skill name.

Discovery also scans the legacy `%LOCALAPPDATA%\AgentMemory\skills` folder
(written by `skill_manage`). A skill in this repo overrides a legacy skill
with the same name, so committed procedures always win.

Skill bodies are loaded on demand with the `load_skill` tool; only the id and
a 160-character description preview appear in the system prompt.
