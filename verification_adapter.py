"""Host adapter that connects AgentBot's turn log to VerificationEngine.

The engine verifies recorded executions, so this adapter replays the turn's
successful tool outputs as evidence. Mutating tools evidence their own action;
read-only tools evidence `READ_ACTION`, which is what a reply like "I searched
the web" actually claims. A tool that only looked at something still proves
nothing about a mutation.
"""
from __future__ import annotations

import json
import re
import traceback
from collections.abc import Iterable
from threading import Event

from verification_engine import (
    ERROR_REPLY,  # noqa: F401  re-export: agent.py, tests/test_provider_stop.py, tests/test_verification_turn.py read verification_adapter.ERROR_REPLY
    ToolResult,
    ToolSpec,
    VerificationEngine,
    _ACTION_FORMS,
    _COMPLETION,
    _FIRST_PERSON,
    _check_cancelled,
)

RETRY_MESSAGE = (
    "ERROR: Your claim was rejected by the verification engine because you "
    "did not actually run the tool. Try again."
)
MAX_RETRIES = 1

ENFORCED_ACTIONS = frozenset({
    "translate",
    "save", "create", "edit", "delete", "move", "copy", "send", "publish",
    "run", "schedule", "install", "download", "upload", "deploy", "convert",
    "rename", "compress", "extract", "restart", "stop", "backup", "restore",
    "connect", "configure", "enable", "disable", "apply", "format", "clean",
})

# The action a successful observation evidences. Deliberately absent from
# ENFORCED_ACTIONS: it satisfies "I searched / I read / I checked" and can
# never satisfy a mutation claim.
READ_ACTION = "read"

# Names the registry has not migrated yet. Registered tools report their own
# effect_class, so this list only covers the legacy dispatcher.
LEGACY_READ_ONLY_TOOLS = frozenset({
    "rag_search", "web_fetch", "map_site", "crawl_site", "crawl_managed",
    "describe_image", "describe_images", "analyze_screenshot", "ocr_image",
    "extract_text", "scrutinize_text", "read_screen", "describe_screen",
    "get_pixel_color", "clipboard_read", "show_last_result", "inspect_docx",
    "list_symbols", "repo_map", "list_dir", "system_status",
    "check_battery_health", "weather", "air_quality", "news", "search_web",
    "serp_search", "read_file", "find_files", "current_time", "read_artifact",
    "uia_dump_tree", "uia_get_text", "watch_folder_list",
})


def tool_is_read_only(tool: str) -> bool:
    """True when the tool only observes. The registry answers for itself."""
    try:
        import agent
        spec = agent.TOOL_REGISTRY.get(tool)
    except Exception:
        spec = None
    if spec is not None:
        return getattr(spec, "effect_class", None) == "read_only"
    return tool in LEGACY_READ_ONLY_TOOLS


def _first_person_claim(action: str, reply: str) -> bool:
    """Whether the reply says the *agent* performed this action.

    The engine matches bare past tense ("published") with no subject check, so
    a sentence reporting someone else's action reads as the agent's claim.
    Mutation verbs keep their own guard; this only tells "DeepSeek published a
    paper" apart from "I published the site".
    """
    forms = _ACTION_FORMS.get(action)
    if not forms:
        return True
    base, past = forms
    pattern = re.compile(
        rf"{_FIRST_PERSON}[^.!?]{{0,40}}\b(?:{base}|{past})\b", re.IGNORECASE)
    return bool(pattern.search(reply))


def _reported_actions(reply: str, enforced, sources) -> set:
    """Enforced verbs that a successful observation's own text reports.

    Exempting these cannot weaken a real claim: it requires a read-only tool
    that ran, returned the verb in its output, and a reply that never says the
    agent did it.
    """
    if not sources:
        return set()
    reported = set()
    for action in enforced:
        forms = _ACTION_FORMS.get(action)
        if not forms:
            continue
        base, past = forms
        if _first_person_claim(action, reply):
            continue
        verb = re.compile(rf"\b(?:{base}|{past})\b", re.IGNORECASE)
        if any(verb.search(source) for source in sources):
            reported.add(action)
    return reported


def _post_to_social(tool_input):
    action = tool_input.split("|", 1)[0].strip().lower()
    return {"publish"} if action == "post" else set()


def _telegram_delete(tool_input):
    parts = tool_input.split("|")
    confirm = parts[-1].strip().lower() if len(parts) > 1 else ""
    return {"delete"} if confirm in ("confirm", "all") else set()


def _schedule_task(tool_input):
    action = tool_input.split("|", 1)[0].strip().lower()
    return set() if action in ("list", "remove") else {"schedule"}


def _whitelist(tool_input):
    action = tool_input.split("|", 1)[0].strip().lower()
    return {"delete"} if action == "remove" else set()


def _skill_manage(tool_input):
    action = tool_input.split("|", 1)[0].strip().lower()
    return {"create": {"create"}, "update": {"edit"}, "delete": {"delete"}}.get(action, set())


def _todo(tool_input):
    action = tool_input.split(":", 1)[0].strip().lower()
    return {"delete"} if action in ("remove", "clear") else set()


def _media_control(tool_input):
    return {"stop"} if tool_input.strip().lower() == "stop" else set()


TOOL_EVIDENCE = {
    "write_file": frozenset({"save", "create"}),
    "patch_file": frozenset({"edit"}),
    "make_folder": frozenset({"create"}),
    "move_files": frozenset({"move"}),
    "edit_document": frozenset({"edit", "save"}),
    "edit_docx_safe": frozenset({"edit", "save"}),
    "post_to_social": _post_to_social,
    "send_telegram_message": frozenset({"send"}),
    "telegram_user_send": frozenset({"send"}),
    "telegram_user_edit": frozenset({"edit"}),
    "telegram_user_delete": _telegram_delete,
    "send_image_telegram": frozenset({"send"}),
    "send_gif": frozenset({"send"}),
    "reply_with_gif": frozenset({"send"}),
    "send_maps_url": frozenset({"send"}),
    "send_maps_list": frozenset({"send"}),
    "generate_image": frozenset({"create"}),
    "save_screenshot": frozenset({"save", "create"}),
    "save_macro": frozenset({"save"}),
    "play_macro": frozenset({"run"}),
    "run_recipe": frozenset({"run"}),
    "run_command": frozenset({"run"}),
    "execute_python": frozenset({"run"}),
    "set_reminder": frozenset({"schedule"}),
    "set_reminder_at": frozenset({"schedule"}),
    "schedule_task": _schedule_task,
    "remember": frozenset({"save"}),
    "forget": frozenset({"delete"}),
    "whitelist": _whitelist,
    "skill_manage": _skill_manage,
    "todo": _todo,
    "set_name": frozenset({"edit", "rename"}),
    "media_control": _media_control,
    "watch_folder": frozenset({"start"}),
    "stop_watching": frozenset({"stop"}),
    "play_on_youtube": frozenset({"start"}),
    "stream_media": frozenset({"start"}),
    "quarantine_file": frozenset({"move"}),
    "restart_pc": frozenset({"restart"}),
    "shutdown_pc": frozenset({"stop"}),
    "sleep_pc": frozenset({"stop"}),
    "lock_pc": frozenset({"stop"}),
    "deep_research": frozenset({"save", "create"}),
}


def _actions_for(tool, tool_input):
    entry = TOOL_EVIDENCE.get(tool)
    if entry is None:
        if tool_is_read_only(tool):
            return frozenset({READ_ACTION})
        return frozenset()
    if callable(entry):
        return frozenset(entry(tool_input))
    return entry


def _text(output):
    if output is None:
        return ""
    return output if isinstance(output, str) else str(output)


def _source_text(execution) -> str:
    """The recorded output as plain text, for verb matching.

    The engine stores it JSON-encoded, so a bare `execution.output` does not
    exist — reading the wrong attribute here would raise inside
    reply_is_supported's catch-all and silently block every reply.
    """
    raw = getattr(execution, "output_json", None)
    if raw is None:
        return ""
    try:
        return _text(json.loads(raw))
    except (TypeError, ValueError):
        return _text(raw)


def _verified(_arguments, result):
    return (
        result.success is True
        and isinstance(result.output, str)
        and bool(result.output.strip())
    )


def _tool_succeeded(tool, text):
    if tool == "edit_docx_safe":
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return False
        return isinstance(payload, dict) and payload.get("ok") is True
    return bool(text.strip()) and not text.lstrip().lower().startswith("error:")


def build_engine(executions: Iterable[tuple[str, object, object]],
                 stop_event: Event | None = None) -> VerificationEngine:
    _check_cancelled(stop_event)
    tools = {}
    pending = []
    for index, (tool, tool_input, output) in enumerate(executions):
        _check_cancelled(stop_event)
        actions = _actions_for(tool, _text(tool_input))
        if not actions:
            continue
        text = _text(output)
        success = _tool_succeeded(tool, text)
        key = f"{tool}:{index}"
        tools[key] = ToolSpec(
            run=(lambda _arguments, _text=text, _success=success: ToolResult(
                _success,
                _text if _success else None,
                None if _success else "tool reported an error",
            )),
            verifiers={action: _verified for action in actions},
        )
        pending.append((key, _text(tool_input)))
    engine = VerificationEngine(tools, stop_event=stop_event)
    for key, tool_input in pending:
        _check_cancelled(stop_event)
        engine.execute_tool(key, {"tool_input": tool_input})
    _check_cancelled(stop_event)
    return engine


def reply_is_supported(engine: VerificationEngine, reply: str,
                       stop_event: Event | None = None) -> bool:
    """True when the reply may be sent; faults fail closed, cancellation propagates."""
    def check():
        _check_cancelled(stop_event)
        engine.check_cancelled()

    try:
        check()
        if not isinstance(reply, str):
            return False
        filtered = engine.filter_reply(reply)
        check()
        if filtered == reply:
            return True
        claimed = engine.claimed_actions(reply)
        evidenced = set()
        sources = []
        for execution in engine.executions:
            check()
            if execution.success and execution.has_output:
                evidenced.update(execution.verified_actions)
                if READ_ACTION in execution.verified_actions:
                    sources.append(_source_text(execution))
        enforced = claimed & ENFORCED_ACTIONS
        check()
        # A verb that the observed source itself reports, in a sentence that
        # never says the agent did it, is quotation — not a claim to prove.
        unmet = enforced - evidenced - _reported_actions(reply, enforced, sources)
        check()
        if _COMPLETION.search(VerificationEngine._normalize(reply)):
            return bool(evidenced) and not unmet
        return not unmet
    except InterruptedError:
        raise
    except Exception:
        # Cancellation wins even when a callback reports a different failure.
        try:
            check()
        except InterruptedError:
            raise
        except Exception:
            pass
        traceback.print_exc()
        return False
