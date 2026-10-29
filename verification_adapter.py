"""Host adapter that connects AgentBot's turn log to VerificationEngine.

The engine verifies recorded executions, so this adapter replays the turn's
successful tool outputs as evidence. Only mutating tools can supply evidence;
read/search/analyze claims stay on the existing heuristic guards.
"""
from __future__ import annotations

import json
import traceback
from collections.abc import Iterable
from threading import Event

from verification_engine import (
    ERROR_REPLY,
    ToolResult,
    ToolSpec,
    VerificationEngine,
    _COMPLETION,
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
        return frozenset()
    if callable(entry):
        return frozenset(entry(tool_input))
    return entry


def _text(output):
    if output is None:
        return ""
    return output if isinstance(output, str) else str(output)


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
        for execution in engine.executions:
            check()
            if execution.success and execution.has_output:
                evidenced.update(execution.verified_actions)
        enforced = claimed & ENFORCED_ACTIONS
        check()
        if _COMPLETION.search(VerificationEngine._normalize(reply)):
            return bool(evidenced) and enforced <= evidenced
        return enforced <= evidenced
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
