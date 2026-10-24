# Plugins

Optional skill packs live here. Each plugin is a directory whose name is the
namespace used in skill ids:

```
plugins/
  my-plugin/
    skills/
      tidy-downloads/
        SKILL.md
```

Skills are addressed as `my-plugin:tidy-downloads`. A plugin may instead ship
one skill at `plugins/my-plugin/SKILL.md`, which is addressed as
`my-plugin:my-plugin`.

Plugin discovery is repo-only for now. Only `SKILL.md` frontmatter is read at
scan time; the body is loaded on demand with the `load_skill` tool.
