# Testing

## The command that actually runs the suite

```bash
python -m unittest discover -s tests -t tests -p "test_*.py"
```

Run it from the repository root.

## Interpreter — this is the part that trips people up

Use the **pyenv Python 3.11.9** interpreter (`python` on a normal shell here).
That is the environment that has the runtime dependencies the tests import:
`requests`, `telethon`, `openai`, `pyautogui`, `sounddevice`, `PySide6`.

**Do not use `.venv` for tests.** `.venv` has `pytest` installed but is missing
most of the runtime dependencies above, so the suite collects nothing and the
run looks like a pass while executing zero tests. If you see `0 tests` or
`35 errors during collection`, you are on the wrong interpreter — not on a
broken test suite.

## Why `-t tests` is required

`tests/` has no `__init__.py` — the file exists in zero commits across the whole
history — so `tests` is not an importable package. The default discovery
invocation fails outright:

```bash
python -m unittest discover -s tests -t . -p "test_*.py"
# ImportError: Start directory is not importable: '...\AgentBot\tests'
```

Passing `-t tests` makes `tests` the top-level directory, so each test module is
imported by its own filename. Every test module adds the repository root to
`sys.path` itself (e.g. `tests/test_parallel_batch.py:12`), which is how the
project modules under test become importable.

## Expected result

| | |
|---|---|
| Tests | **1425** |
| Duration | ~65 s |
| Outcome | `Ran 1425 tests in ...` then `OK`, exit code 0 |

The voice/wake/TTS modules open real microphones and audio devices, so the run
prints non-fatal lines such as `[voice] mic open failed (1/2): device busy` and
`[wake] wake flow failed: mic busy`. Those are the tests exercising failure
paths; judge the run only by the final `Ran N tests` / `OK` / `FAILED` summary.

One known flake: `tests/test_uia_control.py:109`
(`LiveWindowTests.test_uia_type_writes_text_that_can_be_read_back`) types into a
live window, so under load it can drop or interleave keystrokes and fail with
`'hello from UIA' not found in 'hello from UIcAan'`, or the keystrokes can land
nowhere and fail with `'hello from UIA' not found in ''` while `uia_type` still
reports success. Both are timing/focus artifacts of the real desktop, not code
regressions. Re-running does not always clear it — if it fails repeatedly,
confirm against a clean checkout (`git worktree add ../head HEAD`) before
treating it as a regression.

## `pytest tests` is not supported here

Running pytest fails at import time rather than reporting real results:

```
ModuleNotFoundError: No module named 'requests'
Interrupted: 35 errors during collection !
```

That is 0 tests executed, not 35 failures. **Do not add pytest configuration
(`pytest.ini`, `pyproject.toml` markers, a `conftest.py`) to "fix" this** — the
suite is unittest-based, and the underlying issue is the interpreter's missing
dependencies, not test discovery.

## Running a single module

```bash
python -m unittest tests.test_parallel_batch -v      # as the module docstring suggests
python -m unittest discover -s tests -t tests -p "test_prompt_router.py"
```
