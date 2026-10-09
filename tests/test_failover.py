"""
Regression tests for provider failover and empty-reply handling.

Run: python -m pytest tests/test_failover.py -v
Or without pytest: python tests/test_failover.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import agent


def test_empty_response_treated_as_failure():
    """A provider returning '' must not be treated as success."""
    orig_dispatch = agent._dispatch_call
    seen = []

    def fake_dispatch(name, prompt, force_json, max_tokens):
        seen.append(name)
        # First provider in the chain returns an empty reply; the next succeeds.
        if len(seen) == 1:
            return ""
        return "ok from next provider"

    agent._dispatch_call = fake_dispatch
    try:
        result = agent._call_llm("test")
        assert result != "", "empty reply was treated as success"
        assert result == "ok from next provider"
        assert len(seen) >= 2, "failover did not continue past the empty reply"
        assert agent._LAST_FAILURE_REASON is None  # reset on the successful call
    finally:
        agent._dispatch_call = orig_dispatch


def test_whitespace_response_treated_as_failure():
    """A provider returning '   ' must not be treated as success."""
    orig_dispatch = agent._dispatch_call
    seen = []

    def fake_dispatch(name, prompt, force_json, max_tokens):
        seen.append(name)
        if len(seen) == 1:
            return "   \n\t"
        return "ok from next provider"

    agent._dispatch_call = fake_dispatch
    try:
        result = agent._call_llm("test")
        assert result.strip() != "", "whitespace reply was treated as success"
        assert result == "ok from next provider"
        assert len(seen) >= 2, "failover did not continue past the whitespace reply"
    finally:
        agent._dispatch_call = orig_dispatch


def test_empty_response_records_failure_reason():
    """When every provider returns empty, failure reason must be 'empty'."""
    orig_dispatch = agent._dispatch_call

    def fake_dispatch(name, prompt, force_json, max_tokens):
        return ""

    agent._dispatch_call = fake_dispatch
    try:
        result = agent._call_llm("test")
        assert result.startswith("Error:")
        assert agent._LAST_FAILURE_REASON == "empty"
    finally:
        agent._dispatch_call = orig_dispatch


def test_force_provider_walks_only_one():
    """force_provider must restrict the walk to that single provider."""
    orig_dispatch = agent._dispatch_call
    seen = []

    def fake_dispatch(name, prompt, force_json, max_tokens):
        seen.append(name)
        return "ok"

    agent._dispatch_call = fake_dispatch
    try:
        result = agent._call_llm("test", force_provider="groq")
        assert result == "ok"
        assert seen == ["groq"], f"expected only groq, saw {seen}"
        assert agent._LAST_PROVIDER == "groq"
    finally:
        agent._dispatch_call = orig_dispatch


def test_malformed_json_action_handled():
    """parse_action on garbage must not raise. It returns (action, error)."""
    result = agent.parse_action("this is not json and not an action")
    # Real contract: parse_action returns a (action, error) tuple and falls
    # back to a chat action on plain text rather than raising.
    assert isinstance(result, tuple) and len(result) == 2
    action, error = result
    assert action is None or isinstance(action, dict)


def test_unknown_tool_name_rejected():
    """execute_tool with a tool not in VALID_TOOLS must return an error string, not raise."""
    result = agent.execute_tool({"tool": "definitely_not_a_real_tool", "input": ""})
    assert isinstance(result, str)
    assert "error" in result.lower() or "unknown" in result.lower()


def _run_standalone():
    tests = [
        test_empty_response_treated_as_failure,
        test_whitespace_response_treated_as_failure,
        test_empty_response_records_failure_reason,
        test_force_provider_walks_only_one,
        test_malformed_json_action_handled,
        test_unknown_tool_name_rejected,
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
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(1 if _run_standalone() else 0)
