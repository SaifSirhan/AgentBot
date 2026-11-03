"""Translation layer between AgentBot's text contracts and native tool calling.

The turn loop records its work as `step_log` strings ("Action: tool(input) ->
Result: output") and hands the model a flattened prompt. deepseek-flash accepts
the OpenAI-compatible shape instead: a `tools` array of JSON-Schema functions,
`message.tool_calls` on the way out, and `role: "tool"` results on the way back
in. This module converts in both directions and owns no state — the caller
passes the transcript it already has.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterable, Sequence

# Tool output goes back to the model verbatim, so one oversized observation
# (a file read, a screen dump) would be re-sent on every remaining step.
MAX_TOOL_MESSAGE_CHARS = 6000

DELEGATE_TO_QODER = "delegate_to_qoder"

_ACTION_LINE = re.compile(r"^Action: (?P<tool>[a-z][a-z0-9_]*)\((?P<input>.*)\) -> Result: (?P<result>.*)$",
                          re.DOTALL)
_DONE_LINE = "Done: "
_SYSTEM_LINE = "System: "
_CHAT_TOOL = "chat"
_BATCH_TOOL = "batch"


def _string_input_schema(description: str) -> dict:
    return {
        "type": "object",
        "properties": {"input": {"type": "string", "description": description}},
        "required": ["input"],
        "additionalProperties": False,
    }


_DELEGATE_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string", "description": "What to build"},
        "goal": {"type": "string", "description": "What proves it is done"},
    },
    "required": ["prompt"],
    "additionalProperties": False,
}


def _strict(schema: dict) -> dict:
    """Copy a registry schema and make sure it cannot accept extra keys."""
    fixed = dict(schema)
    fixed.setdefault("type", "object")
    fixed.setdefault("properties", {})
    fixed.setdefault("required", sorted(fixed["properties"]))
    fixed["additionalProperties"] = False
    return fixed


def build_specs(names: Iterable[str], registry: Any = None,
                describe: Callable[[str], str] = lambda name: "") -> list[dict]:
    """OpenAI `tools` entries for these tool names, in sorted order.

    Sorted because the array is part of the prompt prefix: a stable order lets
    the provider's prompt cache hit instead of invalidating on every route.
    A migrated tool brings its own JSON Schema from the registry; the rest take
    the single `input` string the legacy dispatcher already expects.
    """
    specs = []
    for name in sorted(set(names or ())):
        spec = registry.get(name) if registry is not None else None
        if name == DELEGATE_TO_QODER:
            parameters, description = _DELEGATE_SCHEMA, None
        elif spec is not None and getattr(spec, "input_schema", None):
            parameters, description = _strict(spec.input_schema), spec.description
        else:
            parameters, description = None, None

        text = (description or (spec.description if spec is not None else "")
                or describe(name) or name.replace("_", " "))
        parameters = parameters or _string_input_schema(text)
        specs.append({
            "type": "function",
            "function": {
                "name": name,
                "description": text[:500],
                "parameters": parameters,
            },
        })
    return specs


def actions_from_tool_calls(message: dict, *, valid_names: Iterable[str],
                            allowed: Iterable[str],
                            object_input_tools: Iterable[str] = ()) -> tuple:
    """Native `tool_calls` -> the action dicts the dispatcher already accepts.

    Returns (actions, errors). A tool name is checked against both the global
    registry and this turn's allowlist: the provider will happily echo a name
    we never advertised, so the guard belongs here and not in the prompt.
    Any error rejects the whole response — a half-executed batch is worse than
    one more turn.
    """
    calls = (message or {}).get("tool_calls") or []
    if not calls:
        return None, []

    valid = set(valid_names)
    allow = set(allowed)
    envelopes = set(object_input_tools)
    actions, errors = [], []

    for call in calls:
        function = (call or {}).get("function") or {}
        name = str(function.get("name") or "").strip()
        raw = function.get("arguments")
        if not name:
            errors.append("ERROR: a tool call arrived with no name.")
            continue
        if name not in valid:
            errors.append(f"ERROR: '{name}' is not a valid tool name.")
            continue
        if name not in allow:
            errors.append(f"ERROR: '{name}' is not in this turn's allowlist.")
            continue
        try:
            args = raw if isinstance(raw, dict) else json.loads(raw or "{}")
        except (ValueError, TypeError):
            errors.append(f"ERROR: {name} got arguments that are not JSON.")
            continue
        if not isinstance(args, dict):
            errors.append(f"ERROR: {name} got {type(args).__name__} arguments.")
            continue

        if name == DELEGATE_TO_QODER:
            actions.append({"tool": name,
                            "input": str(args.get("prompt", "")),
                            "goal": str(args.get("goal", ""))})
        elif name in envelopes:
            actions.append({"tool": name, "input": args})
        else:
            value = args.get("input", "")
            actions.append({"tool": name,
                            "input": value if isinstance(value, str) else ""})

    if errors:
        return [], errors
    return actions, []


def _truncate(text: str) -> str:
    if len(text) <= MAX_TOOL_MESSAGE_CHARS:
        return text
    return (text[:MAX_TOOL_MESSAGE_CHARS]
            + f"\n[truncated {len(text) - MAX_TOOL_MESSAGE_CHARS} chars]")


def messages_from_transcript(step_log: Sequence[str]) -> list[dict]:
    """step_log lines -> assistant/tool messages, in order.

    Ids are positional, so rebuilding from the same transcript produces the same
    conversation: the loop regenerates context on every step and a changing id
    would look like a different tool call each time.
    """
    messages: list[dict] = []
    index = 0

    for line in step_log or ():
        if not isinstance(line, str):
            continue
        if line.startswith(_SYSTEM_LINE):
            messages.append({"role": "user", "content": line})
            continue
        if line.startswith(_DONE_LINE):
            messages.append({"role": "assistant",
                             "content": line[len(_DONE_LINE):].strip()})
            continue

        match = _ACTION_LINE.match(line)
        if not match:
            continue
        tool = match.group("tool")
        result = _truncate(match.group("result"))

        if tool == _CHAT_TOOL:
            messages.append({"role": "assistant", "content": result})
            continue
        if tool == _BATCH_TOOL:
            # A batch rejection is prose about the previous step, not a call.
            messages.append({"role": "user", "content": result})
            continue

        call_id = f"call_{index}"
        index += 1
        arguments = {"input": match.group("input")}
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {
                    "name": tool,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }],
        })
        messages.append({"role": "tool", "tool_call_id": call_id,
                         "name": tool, "content": result})

    return messages


def tool_message(call_id: str, name: str, content: str) -> dict:
    """The result the provider asked for, keyed to the call that produced it."""
    return {"role": "tool", "tool_call_id": call_id, "name": name,
            "content": _truncate(content)}
