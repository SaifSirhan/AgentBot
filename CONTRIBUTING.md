# Contributing to AgentBot

Thanks for your interest! AgentBot is a **personal project**, so contributions
are welcome but kept lightweight. This guide sets expectations so we can work
together smoothly.

## Reporting bugs

Open an issue using the **Bug report** template and include:

- What happened vs. what you expected.
- Steps to reproduce.
- Your environment (Windows version, Python version).
- Relevant log output — **never paste API keys, session tokens, or personal file
  paths with your username.**

## Suggesting features

Use the **Feature request** template. Describe the problem you're solving and
how you imagine it working. Keep in mind this is a personal assistant, so scope
stays deliberately small — not every suggestion will be adopted.

## Pull requests

1. Fork the repo and create a branch from `main`.
2. Keep changes focused — one logical change per PR.
3. Follow the existing style (see below).
4. Make sure the app still launches: `python -c "import agent"` and run the GUI.
5. Fill in the PR template checklist.

## Coding style

- **PEP 8**, 4-space indentation (no tabs).
- Match the surrounding code's conventions and naming.
- Prefer small, surgical changes over large rewrites.
- Don't add new third-party dependencies without discussing it in an issue first.
- Don't rename existing tools or functions casually — tool names are referenced
  in the LLM's tool-description prompt, and a rename can break tool selection.

## Tests

Automated tests are **work in progress**. For now, verify changes manually:

```bash
python -c "import agent; print(agent.list_brains())"
python agent_gui.py
```

Please describe how you tested your change in the PR.

## Ground rules

- No secrets in the repo, ever. `.gitignore` already excludes config, session,
  and credential files — don't force-add them.
- Be kind and patient. This is a hobby project maintained in spare time.
