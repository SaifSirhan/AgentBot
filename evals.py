"""
Selection-only evaluation harness for AgentBot.

Records every agent turn as a structured trace. Runs suites in selection-only
mode: the LLM is asked what tool it would call, the parsed action is compared
against the expected tool, and nothing is executed.

Not an LLM-facing tool. Exposed via GUI slash commands only.
"""
from __future__ import annotations
import hashlib, json, os, re, time, uuid
from datetime import datetime
from pathlib import Path

EVALS_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "AgentMemory" / "evals"
TRACES_DIR = EVALS_DIR / "traces"
SUITES_DIR = EVALS_DIR / "suites"
RUNS_DIR = EVALS_DIR / "runs"
for _d in (TRACES_DIR, SUITES_DIR, RUNS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ---------- redaction ----------
_SECRET_PATTERNS = [
    re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"),         # long tokens/keys
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),         # openai-style
    re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}\b"),     # google
    re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),        # github
]


def _redact(text) -> str:
    if not isinstance(text, str):
        return text
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


# ---------- prompt hash ----------
def _prompt_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


# ---------- token estimate ----------
def _est_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


# ---------- trace recording ----------
def record_trace(
    user_request: str,
    tool_calls: list,
    final_reply: str,
    duration_ms: int,
    prompt_text: str,
    provider: str = None,
    failure_reason: str = None,
    parse_ok: bool = True,
    empty_reply: bool = False,
    source: str = "cli",
    commit: str = "",
) -> str:
    tid = uuid.uuid4().hex[:8]
    ts = datetime.now().isoformat(timespec="seconds")
    trace = {
        "id": tid,
        "timestamp": ts,
        "source": source,
        "user_request": _redact(user_request),
        "tool_calls": [{"tool": c.get("tool"), "input": _redact(str(c.get("input", "")))}
                       for c in tool_calls],
        "final_reply": _redact(final_reply),
        "duration_ms": duration_ms,
        "provider": provider,
        "failure_reason": failure_reason,
        "prompt_hash": _prompt_hash(prompt_text),
        "parse_ok": parse_ok,
        "empty_reply": empty_reply,
        "est_tokens_in": _est_tokens(prompt_text),
        "est_tokens_out": _est_tokens(final_reply),
        "tokens_estimated": True,
        "user_feedback": None,
        "selection_correct": None,
        "execution_correct": None,
        "goal_completed": None,
        "response_accurate": None,
        "commit": commit,
    }
    try:
        (TRACES_DIR / f"{ts.replace(':', '-')}_{tid}.json").write_text(
            json.dumps(trace, indent=2), encoding="utf-8"
        )
    except Exception:
        pass  # a trace write failure must never break a turn
    return tid


def label_trace(trace_id: str, field: str = "user_feedback", value=None) -> str:
    """
    field: user_feedback | selection_correct | execution_correct | goal_completed | response_accurate
    value: "good"/"bad" for user_feedback; true/false for the others.
    """
    for p in TRACES_DIR.iterdir():
        if trace_id in p.name:
            try:
                t = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return f"ERROR: could not parse {p.name}"
            if field == "user_feedback":
                if value not in ("good", "bad"):
                    return "ERROR: user_feedback must be 'good' or 'bad'."
                t["user_feedback"] = value
            elif field in ("selection_correct", "execution_correct",
                           "goal_completed", "response_accurate"):
                t[field] = bool(value)
            else:
                return f"ERROR: unknown field '{field}'."
            p.write_text(json.dumps(t, indent=2), encoding="utf-8")
            return f"Updated {trace_id}: {field}={value}"
    return f"ERROR: trace {trace_id} not found."


def list_traces(limit: int = 20) -> list:
    files = sorted(TRACES_DIR.glob("*.json"), reverse=True)[:limit]
    out = []
    for p in files:
        try:
            t = json.loads(p.read_text(encoding="utf-8"))
            out.append({
                "id": t["id"],
                "timestamp": t["timestamp"],
                "request": t["user_request"][:60],
                "provider": t.get("provider"),
                "parse_ok": t.get("parse_ok"),
                "empty_reply": t.get("empty_reply"),
                "failure_reason": t.get("failure_reason"),
                "feedback": t.get("user_feedback"),
            })
        except Exception:
            continue
    return out


# ---------- suites ----------
def load_suite(name: str) -> dict:
    p = SUITES_DIR / f"{name}.json"
    if not p.exists():
        return {"name": name, "mode": "selection_only", "cases": []}
    return json.loads(p.read_text(encoding="utf-8"))


def save_suite(name: str, suite: dict) -> None:
    (SUITES_DIR / f"{name}.json").write_text(json.dumps(suite, indent=2), encoding="utf-8")


def run_suite(suite_name: str, force_provider: str = None) -> dict:
    """Run a suite in selection-only mode. Does not execute any tool."""
    suite = load_suite(suite_name)
    results = []
    for case in suite["cases"]:
        t0 = time.time()
        try:
            preview = _preview_first_action(
                case["request"],
                history=[],
                force_provider=force_provider,
            )
        except Exception as e:
            preview = {
                "action": None, "provider": None, "parse_ok": False,
                "empty_reply": False, "raw": "", "error": str(e),
            }
        actual_tool = (preview.get("action") or {}).get("tool")
        actual_input = (preview.get("action") or {}).get("input", "") or ""
        # An errored or empty preview proves nothing about selection — the
        # provider never answered. It must not be able to pass, even a
        # null-expected case (where action=None would otherwise look like
        # "correctly chose no tool").
        errored = bool(preview.get("empty_reply")) or preview.get("error") is not None
        passed = (not errored) and _case_passed(case, actual_tool, actual_input)
        results.append({
            "case_id": case["id"],
            "passed": passed,
            "errored": errored,
            "actual_tool": actual_tool,
            "expected_tool": case.get("expected_tool"),
            "provider": preview.get("provider"),
            "parse_ok": preview.get("parse_ok"),
            "empty_reply": preview.get("empty_reply"),
            "duration_ms": int((time.time() - t0) * 1000),
        })
    run = {
        "run_id": uuid.uuid4().hex[:8],
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "suite": suite_name,
        "mode": suite.get("mode", "selection_only"),
        "force_provider": force_provider,
        "commit": _current_commit(),
        "results": results,
        "errors": sum(1 for r in results if r.get("errored")),
        "pass_rate": sum(1 for r in results if r["passed"]) / max(len(results), 1),
    }
    (RUNS_DIR / f"{run['timestamp'].replace(':', '-')}_{run['run_id']}.json").write_text(
        json.dumps(run, indent=2), encoding="utf-8"
    )
    return run


def diff_runs(run_a: str, run_b: str) -> str:
    a = _find_run(run_a)
    b = _find_run(run_b)
    if not a or not b:
        return "ERROR: one or both runs not found."
    a_by = {r["case_id"]: r for r in a["results"]}
    b_by = {r["case_id"]: r for r in b["results"]}
    lines = []
    for cid in sorted(set(a_by) | set(b_by)):
        pa = a_by.get(cid, {}).get("passed")
        pb = b_by.get(cid, {}).get("passed")
        if pa != pb:
            lines.append(f"{cid}: {pa} -> {pb}")
    if not lines:
        return "No regressions."
    return "Changes:\n" + "\n".join(lines)


def _find_run(run_id: str):
    for p in RUNS_DIR.glob("*.json"):
        if run_id in p.name:
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
    return None


def _case_passed(case: dict, actual_tool, actual_input: str) -> bool:
    expected = case.get("expected_tool")
    forbidden = case.get("forbidden_tools", []) or []
    if actual_tool in forbidden:
        return False
    # In this codebase parse_action ALWAYS yields a tool: a plain-text reply
    # falls back to {"tool": "chat"} (and the loop may end on "done"). For a
    # case that expects NO tool, those two count as "no tool chosen". A bare
    # None is also accepted for callers that pass one.
    if expected is None:
        return actual_tool is None or actual_tool in ("chat", "done")
    if actual_tool != expected:
        return False
    sub = case.get("expected_input_contains")
    if sub and sub not in (actual_input or ""):
        return False
    return True


def _current_commit() -> str:
    try:
        import subprocess
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).parent),
            text=True,
        ).strip()
    except Exception:
        return ""


# ---------- selection-only preview ----------
def _preview_first_action(user_request: str, history: list, force_provider: str = None) -> dict:
    """Ask the LLM what it would do, without executing.

    Reuses the SAME prompt assembly as run_agent_turn step 1 (which goes
    through get_valid_action -> ask_ai_for_plan -> _build_plan_prompt), so the
    selection test measures exactly what a real turn would see. Nothing here
    executes a tool.

    Returns {"action": {...}|None, "provider": str|None, "parse_ok": bool,
             "empty_reply": bool, "raw": str}.
    """
    import agent

    context = "[selection-only preview]"
    prompt_text = agent._build_plan_prompt(user_request, context)
    raw = agent._call_llm(prompt_text, force_json=False, max_tokens=512,
                          force_provider=force_provider)
    provider = getattr(agent, "_LAST_PROVIDER", None)
    empty = not raw or not raw.strip() or str(raw).startswith("Error:")

    action = None
    parse_ok = False
    if not empty:
        try:
            # parse_action returns (action, error); a plain-text reply falls
            # back to {"tool": "chat"}, which is a valid selection.
            parsed, err = agent.parse_action(raw)
            if isinstance(parsed, dict):
                action = parsed
                parse_ok = True
        except Exception:
            parse_ok = False

    return {
        "action": action,
        "provider": provider,
        "parse_ok": parse_ok,
        "empty_reply": empty,
        "raw": raw or "",
    }
