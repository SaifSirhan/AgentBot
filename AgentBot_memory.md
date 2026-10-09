# AgentBot — session handoff

Context note so a new chat can pick up where this one left off.

Repo: `C:\Users\USER\AgentBot` (branch `main`).
Interpreter: use the pyenv 3.11.9 python; the `.venv` is stale.

---

## Task completed (2026-10-09)

Disable the natural-language fast-path in `agent_gui.py` so every message goes
through the LLM instead of the regex-based "send X to Y" router.

Commit: **`ec75273`** — "Disable natural-language fast-path regex, route all through LLM"
(1 file changed, 9 insertions, 7 deletions)

### Why
The regex fast-path in `_try_nl_fastpath` misfired on free-form text such as
`send Cock and balls group|a crying gif`, routing it wrongly and confusing the user.
Removing the short-circuit sends the raw text to the LLM, which decides intent.

### What changed in `agent_gui.py`

**CHANGE 1 — `send()`, ~line 2308.** The fast-path block was commented out
(kept in place, not deleted) with a short why comment:

```python
        # Natural-language fast-path disabled: regex routing misfired on
        # free-form text (e.g. "send X to Y"), so every message goes to the LLM.
        # if not self.attachments and self._try_nl_fastpath(user_input):
        #     self._clear_entry()
        #     try:
        #         self.entry.configure(height=30)
        #     except Exception:
        #         pass
        #     return
```

**CHANGE 2 — methods kept.** `_try_nl_fastpath` (line 1676) and `_dispatch_tg`
(line 1717) are untouched. Deliberately retained; just no longer called from `send()`.

**CHANGE 3 — caller check.** Only two occurrences of `_try_nl_fastpath` exist in
the file: the definition (1676) and the now-commented call site (2310). A repo-wide
`*.py` grep found no other references. No other callers.

### Verification done
- `ast.parse('agent_gui.py')` → syntax OK.
- `git diff` confirmed only the one block changed.
- Committed only `agent_gui.py`. Left untouched uncommitted work as-is:
  `config.py` (modified), `chroma/` (untracked), `test_scrub.txt` (untracked).

### NOT done — runtime tests still outstanding
The four behavioral tests could not be run in-session:

1. **It's a Tk GUI.** Needs a live mainloop + LLM provider credentials to observe
   the 2–5 s latency and the `telegram_user_send` call.
2. **The Telegram tests would send real messages.** `telegram_user.py` uses a
   logged-in *user* session (MTProto/Telethon, not a bot) — testing "send hello to
   John" and "send Cock and balls group|..." sends real messages to real people and
   cannot be un-sent. Do not run these unattended.

To finish: launch AgentBot and check manually:
- `send hello to John` → now takes 2–5 s (LLM), LLM calls `telegram_user_send` with the right contact.
- `send Cock and balls group|a crying gif` → no regex trigger; LLM sees full text.
- `what's the weather` → still works (never used the fast-path).

### Caveats to keep in mind
- **`_dispatch_tg` is now dead code** — nothing calls it while the fast-path is
  commented out. Harmless; it's retained on purpose.
- **The misfire was a routing bug, not a regex bug.** Commenting out the fast-path
  removes the symptom, but the LLM now owns intent-routing for *all* free-form text
  (including the "send X to Y" case the regex used to handle instantly). If the LLM
  mishandles `Cock and balls group|a crying gif`, fix it in the tool/prompt layer —
  not by restoring the regex.

---

## Project conventions that still apply (from prior sessions)

- After any edit: `python -c "import ast; ast.parse(open('file.py', encoding='utf-8').read()); print('ok')"`.
- Python silently keeps the *last* duplicate `def` — grep for duplicate definitions
  after adding/upgrading a function.
- Test tools directly (`python -c "import agent; print(agent.<tool>('...'))"`) instead
  of launching the GUI. `import agent_gui` works headless.
- Instantiating the real `AgentGUI` fires real startup side effects (auto-index,
  scheduler, tray, global hotkey) — monkeypatch those to no-ops before constructing.
- Never round-trip config through `save_config(load_config())` — it bakes env-var
  secrets into `config.json` in plaintext. Edit the raw JSON surgically.
- Keep secrets (`agent_user_session.session`, `credentials.json`, `APIs.txt`) out of
  git. Scan staged files before committing.
