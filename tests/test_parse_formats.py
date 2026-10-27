"""Regression tests for non-JSON tool-call output after the prompt slim-down.

DeepSeek started emitting raw function calls (load_skill("local:stremio")) and
hallucinated tool-result XML (<tool_result name="open_app">) instead of JSON.
parse_action must recover the former and reject the latter.

Run: python -m pytest tests/test_parse_formats.py -v
Or without pytest: python tests/test_parse_formats.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import agent


def test_raw_load_skill_double_quotes():
    action, error = agent.parse_action('load_skill("local:stremio")')
    assert error is None
    assert action == {"tool": "load_skill", "input": "local:stremio"}


def test_raw_load_skill_single_quotes():
    action, error = agent.parse_action("load_skill('local:stremio')")
    assert error is None
    assert action == {"tool": "load_skill", "input": "local:stremio"}


def test_raw_load_skill_with_whitespace_and_escaped_quotes():
    action, error = agent.parse_action(
        'I will run load_skill( \\"local:stremio\\" ) now.')
    assert error is None
    assert action == {"tool": "load_skill", "input": "local:stremio"}


def test_hallucinated_tool_result_is_a_parse_failure():
    raw = ('<tool_result name="open_app">\n{"ok": true}\n</tool_result>')
    action, error = agent.parse_action(raw)
    assert action is None
    assert error and "tool_result" in error


def test_hallucinated_tool_result_wins_over_valid_json():
    raw = ('{"tool": "open_app", "input": "notepad"}\n'
           '<tool_result name="open_app">{"ok": true}</tool_result>')
    action, error = agent.parse_action(raw)
    assert action is None
    assert error


def test_valid_json_still_parses():
    action, error = agent.parse_action(
        '{"tool": "load_skill", "input": "local:stremio"}')
    assert error is None
    assert action == {"tool": "load_skill", "input": "local:stremio"}


def test_prompt_index_states_the_json_contract():
    instruction = (
        'To call a tool, you MUST output a valid JSON block: '
        '{"tool": "tool_name", "input": "tool_input"}. NEVER output raw code, '
        'XML, or function calls like tool("arg").')
    assert instruction in agent.TOOL_DESCRIPTIONS


def _run_standalone():
    tests = [
        test_raw_load_skill_double_quotes,
        test_raw_load_skill_single_quotes,
        test_raw_load_skill_with_whitespace_and_escaped_quotes,
        test_hallucinated_tool_result_is_a_parse_failure,
        test_hallucinated_tool_result_wins_over_valid_json,
        test_valid_json_still_parses,
        test_prompt_index_states_the_json_contract,
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
