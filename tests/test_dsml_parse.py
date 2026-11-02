"""Regression tests for leaked DeepSeek DSML tool-call markup.

When the prompt is near capacity, DeepSeek sometimes emits its raw DSML tags
instead of the JSON tool call the prompt asks for. parse_action must recover
the call instead of echoing the markup back to the user as chat.

Run: python -m pytest tests/test_dsml_parse.py -v
Or without pytest: python tests/test_dsml_parse.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import agent


# Exact leaked reply captured from the 2026-10-27T14-50-52 eval trace
# (provider: deepseek, load_skill for local:stremio).
RAW_DSML_LEAK = (
    '<｜｜DSML｜｜ calls>\n'
    '<｜｜DSML｜｜ invoke name="load_skill">\n'
    '<｜｜DSML｜｜ parameter name="input" string="true">local:stremio</｜｜DSML｜｜ parameter>\n'
    '</｜｜DSML｜｜ invoke>\n'
    '</｜｜DSML｜｜ calls>'
)

# Canonical single-bar spelling, multi-line input (as used by execute_python).
RAW_DSML_MULTILINE = (
    '<｜DSML｜tool_calls>\n'
    '<｜DSML｜invoke name="execute_python">\n'
    '<｜DSML｜parameter name="input" string="true">print("a")\nprint("b")</｜DSML｜parameter>\n'
    '</｜DSML｜invoke>\n'
    '</｜DSML｜tool_calls>'
)


def test_dsml_leak_parses_exact_string():
    """The exact leaked string must parse as a load_skill call."""
    action, error = agent.parse_action(RAW_DSML_LEAK)
    assert error is None
    assert action == {"tool": "load_skill", "input": "local:stremio"}


def test_dsml_leak_multiline_input():
    """Single-bar DSML tokens must parse, and multi-line input must survive."""
    action, error = agent.parse_action(RAW_DSML_MULTILINE)
    assert error is None
    assert action == {"tool": "execute_python",
                      "input": 'print("a")\nprint("b")'}


def test_dsml_unknown_tool_rejected():
    """A DSML call naming an unknown tool must not bypass VALID_TOOLS."""
    action, error = agent.parse_action(
        RAW_DSML_LEAK.replace("load_skill", "definitely_not_a_tool"))
    assert action is None
    assert error


def _run_standalone():
    tests = [
        test_dsml_leak_parses_exact_string,
        test_dsml_leak_multiline_input,
        test_dsml_unknown_tool_rejected,
    ]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS: {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL: {fn.__name__}: {e}")
        except Exception as e:
            failures += 1
            print(f"ERROR: {fn.__name__}: {e!r}")
    print(f"{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_standalone() else 0)
